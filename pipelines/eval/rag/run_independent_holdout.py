"""Guarded, resumeless execution of the sealed RAG holdout.

Validation is offline by default. Real execution requires an independently
approved review record plus an explicit 32-request budget acknowledgement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipelines.eval.rag.validate_independent_holdout import validate_holdout
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGError, RAGProviderError
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService


SYSTEM_VERSION = "rag-hybrid-v3"
DEFAULT_BINDING = Path(__file__).with_name("independent-holdout-v2.1-binding.json")
MAX_EMBEDDING_REQUESTS = 16
MAX_GENERATION_REQUESTS = 16
MINIMUM_INTERVAL_SECONDS = 2.0


class HoldoutCaseError(RuntimeError):
    def __init__(self, stage: str, cause: RAGError) -> None:
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _persist(path: Path, artifact: dict[str, Any]) -> None:
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    pending.replace(path)


def _bound_artifact(binding_path: Path, name: str) -> Path:
    value = name.strip()
    if not value or Path(value).is_absolute() or Path(value).name != value:
        raise ValueError("evaluation binding artifact must be a local filename")
    return binding_path.resolve().parent / value


def load_evaluation_binding(
    binding_path: Path = DEFAULT_BINDING,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, Path]:
    raw = binding_path.read_bytes()
    binding = json.loads(raw)
    if binding.get("schema_version") != 1:
        raise ValueError("unsupported evaluation binding schema")
    if binding.get("system_version") != SYSTEM_VERSION:
        raise ValueError("evaluation binding targets a different system version")

    holdout_path = _bound_artifact(binding_path, str(binding["holdout_artifact"]))
    rubric_path = _bound_artifact(binding_path, str(binding["rubric_artifact"]))
    output_path = _bound_artifact(binding_path, str(binding["result_artifact"]))
    validation = validate_holdout(
        holdout_path,
        rubric_path,
        expected_evaluation_version=str(binding["evaluation_version"]),
        expected_target_system_version=SYSTEM_VERSION,
        expected_sha256=str(binding["holdout_sha256"]),
        expected_rubric_sha256=str(binding["rubric_sha256"]),
    )
    counts = binding.get("case_counts")
    if counts != {
        "positive": validation["positive"],
        "negative": validation["negative"],
        "total": validation["positive"] + validation["negative"],
    }:
        raise ValueError("evaluation binding case counts differ from the holdout")
    budget = binding.get("external_request_budget")
    if budget != {
        "embedding_requests_maximum": MAX_EMBEDDING_REQUESTS,
        "generation_requests_maximum": MAX_GENERATION_REQUESTS,
        "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
        "automatic_retries": 0,
    }:
        raise ValueError("evaluation binding has an unexpected request budget")
    validation["binding_sha256"] = hashlib.sha256(raw).hexdigest()
    return binding, validation, holdout_path, rubric_path, output_path


def validate_review_record(path: Path, validation: dict[str, Any]) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    required_checks = {
        "references_match_corpus",
        "ambiguity_reviewed",
        "duplicates_reviewed",
        "prior_use_reviewed",
        "rubric_is_material_and_complete",
    }
    checks = record.get("checks")
    if record.get("target_system_version") != SYSTEM_VERSION:
        raise ValueError("review record targets a different system version")
    if record.get("evaluation_version") != validation["evaluation_version"]:
        raise ValueError("review record targets a different evaluation version")
    if record.get("holdout_sha256") != validation["sha256"]:
        raise ValueError("review record has a different holdout hash")
    if record.get("rubric_sha256") != validation["rubric_sha256"]:
        raise ValueError("review record has a different rubric hash")
    if record.get("approved") is not True:
        raise ValueError("holdout review is not approved")
    reviewer = record.get("reviewer")
    if not isinstance(reviewer, str) or len(reviewer.strip()) < 3:
        raise ValueError("holdout review has no attributable reviewer")
    if reviewer.strip().casefold() in {"pending", "unknown", "anonymous"}:
        raise ValueError("holdout review has no attributable reviewer")
    reviewed_at = record.get("reviewed_at")
    if not isinstance(reviewed_at, str):
        raise ValueError("holdout review has no review timestamp")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("holdout review timestamp is invalid") from exc
    if parsed_reviewed_at.tzinfo is None:
        raise ValueError("holdout review timestamp must include a timezone")
    approval_source = record.get("approval_source")
    if not isinstance(approval_source, str) or not approval_source.strip():
        raise ValueError("holdout review has no attributable approval source")
    independent = record.get("reviewer_independent_from_implementation")
    if not isinstance(independent, bool):
        raise ValueError("reviewer independence must be explicitly declared")
    review_type = record.get("review_type")
    limitations = record.get("limitations")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) and item.strip() for item in limitations
    ):
        raise ValueError("holdout review limitations are incomplete")
    if independent:
        if review_type != "strictly_independent":
            raise ValueError("independent review must use the strict review label")
        basis = record.get("independence_basis")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("independent review has no declared basis")
    else:
        if review_type != "prospective_with_independence_limitations":
            raise ValueError("limited review must use the prospective label")
        if record.get("independence_limitation_acknowledged") is not True:
            raise ValueError("limited review must acknowledge its independence limitation")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise ValueError("review record checks are incomplete")
    return record


def calculate_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [case for case in cases if case.get("status") == "completed"]
    positive = [case for case in completed if case["split"] == "positive"]
    negative = [case for case in completed if case["split"] == "negative"]

    def rate(items: list[dict[str, Any]], field: str) -> float | None:
        if not items:
            return None
        return sum(bool(item[field]) for item in items) / len(items)

    return {
        "completed_cases": len(completed),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "recall_at_3": rate(positive, "raw_top3_hit"),
        "complete_raw_top3_reference_coverage_rate": rate(
            positive, "raw_top3_complete_coverage"
        ),
        "mean_raw_top3_reference_coverage": (
            sum(float(case["raw_top3_reference_coverage"]) for case in positive)
            / len(positive)
            if positive
            else None
        ),
        "mean_top3_precision": (
            sum(float(case["top3_precision"]) for case in positive) / len(positive)
            if positive
            else None
        ),
        "semantic_primary_expected_recovery": rate(
            positive, "semantic_primary_hit"
        ),
        "lexical_primary_expected_recovery": rate(positive, "lexical_primary_hit"),
        "primary_expected_recovery": rate(positive, "primary_hit"),
        "complementary_expected_recovery": rate(positive, "complementary_hit"),
        "combined_selected_recovery": rate(positive, "selected_hit"),
        "complete_selected_reference_coverage_rate": rate(
            positive, "selected_complete_coverage"
        ),
        "mean_selected_reference_coverage": (
            sum(float(case["selected_reference_coverage"]) for case in positive)
            / len(positive)
            if positive
            else None
        ),
        "negative_abstention_rate": rate(negative, "final_abstention"),
        "negative_false_positives": sum(
            not bool(case["final_abstention"]) for case in negative
        ),
        "commercial_fidelity": {
            "status": "pending_independent_adjudication",
            "automatic_score": None,
        },
    }


def _case_result(
    *,
    case: dict[str, Any],
    split: str,
    service: RAGService,
) -> dict[str, Any]:
    question = str(case["question"])
    expected = {
        (str(item["source_document"]), str(item["section"]))
        for item in case["expected"]
    }
    started = time.perf_counter()
    retrieval_started = time.perf_counter()
    try:
        trace = service.retrieve_with_trace(question, k=3, min_score=0.57)
    except RAGError as exc:
        raise HoldoutCaseError("retrieval", exc) from exc
    retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
    raw_pairs = {
        (candidate.chunk.source_document, candidate.chunk.section)
        for candidate in trace.candidates
    }
    primary_pairs = {
        (candidate.chunk.source_document, candidate.chunk.section)
        for candidate in trace.candidates
        if candidate.retrieval_kind == "primary"
    }
    semantic_primary_pairs = {
        (candidate.chunk.source_document, candidate.chunk.section)
        for candidate in trace.candidates
        if candidate.retrieval_kind == "primary"
        and candidate.evidence_basis == "semantic"
    }
    lexical_primary_pairs = {
        (candidate.chunk.source_document, candidate.chunk.section)
        for candidate in trace.candidates
        if candidate.retrieval_kind == "primary"
        and candidate.evidence_basis == "lexical"
    }
    complementary_pairs = {
        (candidate.chunk.source_document, candidate.chunk.section)
        for candidate in trace.candidates
        if candidate.retrieval_kind == "complementary"
    }
    selected_pairs = primary_pairs | complementary_pairs
    expected_count = len(expected)

    def reference_count(pairs: set[tuple[str, str]]) -> int | None:
        return len(expected & pairs) if split == "positive" else None

    def reference_coverage(pairs: set[tuple[str, str]]) -> float | None:
        if split != "positive":
            return None
        return len(expected & pairs) / expected_count if expected_count else 0.0

    generation_ms: float | None = None
    if trace.selected:
        generation_started = time.perf_counter()
        try:
            answer = service.generate_answer(question, trace.selected)
        except RAGError as exc:
            raise HoldoutCaseError("generation", exc) from exc
        generation_ms = (time.perf_counter() - generation_started) * 1000
    else:
        answer = INSUFFICIENT_INFORMATION_ANSWER
    final_abstention = answer == INSUFFICIENT_INFORMATION_ANSWER

    return {
        "id": str(case["id"]),
        "split": split,
        "status": "completed",
        "question": question,
        "expected": list(case["expected"]),
        "candidates": [
            {
                "rank": rank,
                "source_document": candidate.chunk.source_document,
                "section": candidate.chunk.section,
                "score": candidate.score,
                "retrieval_kind": candidate.retrieval_kind,
                "evidence_basis": candidate.evidence_basis,
            }
            for rank, candidate in enumerate(trace.candidates, start=1)
        ],
        "expected_reference_count": expected_count if split == "positive" else 0,
        "raw_top3_hit": bool(expected & raw_pairs) if split == "positive" else None,
        "raw_top3_reference_hits": reference_count(raw_pairs),
        "raw_top3_reference_coverage": reference_coverage(raw_pairs),
        "raw_top3_complete_coverage": (
            expected <= raw_pairs if split == "positive" else None
        ),
        "top3_relevant_count": (
            len(expected & raw_pairs) if split == "positive" else None
        ),
        "top3_precision": (
            len(expected & raw_pairs) / len(trace.candidates)
            if split == "positive" and trace.candidates
            else (0.0 if split == "positive" else None)
        ),
        "semantic_primary_hit": (
            bool(expected & semantic_primary_pairs) if split == "positive" else None
        ),
        "semantic_primary_reference_hits": reference_count(semantic_primary_pairs),
        "lexical_primary_hit": (
            bool(expected & lexical_primary_pairs) if split == "positive" else None
        ),
        "lexical_primary_reference_hits": reference_count(lexical_primary_pairs),
        "primary_hit": bool(expected & primary_pairs) if split == "positive" else None,
        "complementary_hit": (
            bool(expected & complementary_pairs) if split == "positive" else None
        ),
        "complementary_reference_hits": reference_count(complementary_pairs),
        "selected_hit": (
            bool(expected & selected_pairs) if split == "positive" else None
        ),
        "selected_reference_hits": reference_count(selected_pairs),
        "selected_reference_coverage": reference_coverage(selected_pairs),
        "selected_complete_coverage": (
            expected <= selected_pairs if split == "positive" else None
        ),
        "final_abstention": final_abstention,
        "answer": answer,
        "retrieval_ms": round(retrieval_ms, 3),
        "generation_ms": round(generation_ms, 3) if generation_ms is not None else None,
        "total_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def execute(
    binding_path: Path,
    review_path: Path,
    output: Path | None = None,
) -> int:
    binding, validation, holdout_path, rubric_path, default_output = (
        load_evaluation_binding(binding_path)
    )
    review = validate_review_record(review_path, validation)
    output = default_output if output is None else output
    if output.exists():
        raise ValueError("refusing to overwrite an existing evaluation result")

    settings = load_settings()
    if settings.min_score != 0.57:
        raise ValueError("RAG_MIN_SCORE must remain 0.57 for this evaluation")
    if settings.expected_vector_size != 1024:
        raise ValueError("RAG_VECTOR_SIZE must remain 1024 for this evaluation")
    if settings.min_request_interval_seconds < MINIMUM_INTERVAL_SECONDS:
        raise ValueError("RAG_REQUEST_INTERVAL_SECONDS must be at least 2")

    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    cases = [
        *(("positive", case) for case in holdout["positive"]),
        *(("negative", case) for case in holdout["negative"]),
    ]
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "system_version": SYSTEM_VERSION,
        "evaluation_version": holdout["evaluation_version"],
        "evaluation_binding": binding_path.name,
        "evaluation_binding_sha256": validation["binding_sha256"],
        "holdout_sha256": validation["sha256"],
        "rubric_sha256": validation["rubric_sha256"],
        "independent_review": {
            "reviewed_at": review.get("reviewed_at"),
            "reviewer": review.get("reviewer"),
            "approval_source": review.get("approval_source"),
            "review_type": review.get("review_type"),
            "reviewer_independent_from_implementation": review.get(
                "reviewer_independent_from_implementation"
            ),
            "limitations": review.get("limitations", []),
        },
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "budget": {
            "embedding_requests_maximum": MAX_EMBEDDING_REQUESTS,
            "generation_requests_maximum": MAX_GENERATION_REQUESTS,
            "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
            "automatic_retries": 0,
        },
        "cases": [],
        "metrics": {},
        "usage": {},
        "fidelity_rubric": rubric,
    }
    _persist(output, artifact)

    gateway = FourGeeksHTTPClient(settings)
    qdrant = QdrantHTTPVectorStore(
        settings.qdrant_url,
        timeout=settings.request_timeout_seconds,
    )
    service = RAGService(
        gateway,
        gateway,
        qdrant,
        default_min_score=settings.min_score,
        expected_vector_size=settings.expected_vector_size,
    )
    exit_code = 0
    try:
        for split, case in cases:
            try:
                result = _case_result(case=case, split=split, service=service)
                artifact["cases"].append(result)
            except HoldoutCaseError as case_error:
                exc = case_error.cause
                error: dict[str, Any] = {
                    "id": str(case["id"]),
                    "split": split,
                    "status": "technical_error",
                    "stage": case_error.stage,
                    "error_category": (
                        exc.category if isinstance(exc, RAGProviderError) else "provider_error"
                    ),
                    "http_status": (
                        exc.status_code if isinstance(exc, RAGProviderError) else None
                    ),
                }
                artifact["cases"].append(error)
                artifact["status"] = "interrupted_on_technical_error"
                exit_code = 1
            artifact["embedding_requests"] = gateway.embedding_request_count
            artifact["generation_requests"] = gateway.generation_request_count
            artifact["usage"] = {
                "embedding": gateway.embedding_usage,
                "generation": gateway.generation_usage,
            }
            artifact["metrics"] = calculate_metrics(artifact["cases"])
            _persist(output, artifact)
            if exit_code:
                break
            if gateway.embedding_request_count > MAX_EMBEDDING_REQUESTS:
                raise RuntimeError("embedding request budget exceeded")
            if gateway.generation_request_count > MAX_GENERATION_REQUESTS:
                raise RuntimeError("generation request budget exceeded")
    finally:
        gateway.close()
        qdrant.close()

    artifact["finished_at"] = _now()
    if not exit_code:
        artifact["status"] = "completed_pending_fidelity_adjudication"
    _persist(output, artifact)
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "completed_cases": artifact["metrics"].get("completed_cases"),
                "embedding_requests": artifact.get("embedding_requests", 0),
                "generation_requests": artifact.get("generation_requests", 0),
                "output": str(output),
            },
            separators=(",", ":"),
        )
    )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--review-record", type=Path)
    parser.add_argument("--approved-budget", type=int, choices=(32,))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not args.execute:
        _, validation, _, _, output = load_evaluation_binding(args.binding)
        validation["output"] = str(output)
        print(json.dumps(validation, separators=(",", ":")))
        return 0
    if args.review_record is None or args.approved_budget != 32:
        parser.error(
            "real execution requires --review-record and --approved-budget 32"
        )
    try:
        return execute(args.binding, args.review_record, args.output)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": type(exc).__name__},
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
