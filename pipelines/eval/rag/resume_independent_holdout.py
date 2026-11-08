"""Guarded continuation of the immutable v2.1 evaluation attempt.

Offline preflight is the default. Live continuation requires a new, attributable
authorization record and cannot overwrite either the parent or resume artifact.
The evaluated RAG implementation is imported unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pipelines.eval.rag.run_independent_holdout import (
    DEFAULT_BINDING,
    MINIMUM_INTERVAL_SECONDS,
    SYSTEM_VERSION,
    HoldoutCaseError,
    _case_result,
    _now,
    _persist,
    calculate_metrics,
    load_evaluation_binding,
    validate_review_record,
)
from pipelines.eval.rag.evaluation_checkpoints import (
    CheckpointStore,
    build_checkpoint_binding,
    deserialize_trace,
    serialize_trace,
    validate_freeze_manifest,
)
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGError, RAGProviderError, RetrievalResult, RetrievalTrace
from services.rag.service import RAGService


ROOT = Path(__file__).resolve().parent
DEFAULT_PARENT = ROOT / "independent-holdout-v2.1-results.json"
DEFAULT_REVIEW = ROOT / "independent-holdout-v2.1-review-approved.json"
DEFAULT_OUTPUT = ROOT / "independent-holdout-v2.1-resume-01-results.json"
DEFAULT_CHECKPOINT_DIRECTORY = ROOT / "independent-holdout-v2.1-checkpoints"
FREEZE_MANIFEST = ROOT / "rag-hybrid-v3-freeze-manifest.json"
EXPECTED_PARENT_SHA256 = (
    "10367faa8fb5835f2a2dc09fa4a65b3caf5e3d878c8b3c48e9d3a6d49fa841b0"
)
MAX_NEW_EMBEDDING_REQUESTS = 14
MAX_NEW_GENERATION_REQUESTS = 14
MAX_NEW_EXTERNAL_REQUESTS = 28


def ensure_output_available(path: Path) -> None:
    if path.exists() or path.with_suffix(path.suffix + ".pending").exists():
        raise ValueError("refusing to overwrite an existing resume result")


class _CheckpointedEmbeddingProvider:
    def __init__(
        self,
        delegate: FourGeeksHTTPClient,
        store: CheckpointStore,
        *,
        case_id: str,
        split: str,
        question: str,
        attempt_number: int,
        model: str,
    ) -> None:
        self.delegate = delegate
        self.store = store
        self.case_id = case_id
        self.split = split
        self.question = question
        self.attempt_number = attempt_number
        self.model = model

    def embed(self, text: str) -> list[float]:
        path = self.store.path(self.case_id, "embedding_completed")
        if path.is_file():
            artifact = self.store.load(
                case_id=self.case_id,
                split=self.split,
                question=self.question,
                stage="embedding_completed",
            )
            return [float(value) for value in artifact["payload"]["vector"]]
        if self.delegate.embedding_request_count >= MAX_NEW_EMBEDDING_REQUESTS:
            raise RAGProviderError(
                "Resume embedding request budget exhausted",
                category="budget_exhausted",
            )
        vector = [float(value) for value in self.delegate.embed(text)]
        self.store.write(
            case_id=self.case_id,
            split=self.split,
            question=self.question,
            stage="embedding_completed",
            payload={
                "model": self.model,
                "vector_size": len(vector),
                # The vector is required to avoid another paid embedding call.
                # It remains inside the checkpoint and is never printed.
                "vector": vector,
                "usage": dict(self.delegate.embedding_usage),
            },
            attempt_number=self.attempt_number,
        )
        return vector


class _BudgetedGenerationProvider:
    def __init__(self, delegate: FourGeeksHTTPClient) -> None:
        self.delegate = delegate

    def generate(self, question: str, context: str) -> str:
        if self.delegate.generation_request_count >= MAX_NEW_GENERATION_REQUESTS:
            raise RAGProviderError(
                "Resume generation request budget exhausted",
                category="budget_exhausted",
            )
        return self.delegate.generate(question, context)


class _CheckpointedEvaluationService:
    """Stage adapter around the unchanged frozen ``RAGService``."""

    def __init__(
        self,
        delegate: RAGService,
        store: CheckpointStore,
        gateway: FourGeeksHTTPClient,
        *,
        case_id: str,
        split: str,
        question: str,
        attempt_number: int,
        generation_model: str,
    ) -> None:
        self.delegate = delegate
        self.store = store
        self.gateway = gateway
        self.case_id = case_id
        self.split = split
        self.question = question
        self.attempt_number = attempt_number
        self.generation_model = generation_model

    def retrieve_with_trace(
        self, query: str, k: int = 3, min_score: float | None = None
    ) -> RetrievalTrace:
        path = self.store.path(self.case_id, "retrieval_completed")
        if path.is_file():
            artifact = self.store.load(
                case_id=self.case_id,
                split=self.split,
                question=self.question,
                stage="retrieval_completed",
            )
            return deserialize_trace(artifact["payload"])
        trace = self.delegate.retrieve_with_trace(query, k=k, min_score=min_score)
        self.store.write(
            case_id=self.case_id,
            split=self.split,
            question=self.question,
            stage="retrieval_completed",
            payload=serialize_trace(trace),
            attempt_number=self.attempt_number,
            previous_path=self.store.path(self.case_id, "embedding_completed"),
        )
        return trace

    def generate_answer(
        self, question: str, context: Sequence[RetrievalResult]
    ) -> str:
        path = self.store.path(self.case_id, "generation_completed")
        if path.is_file():
            artifact = self.store.load(
                case_id=self.case_id,
                split=self.split,
                question=self.question,
                stage="generation_completed",
            )
            return str(artifact["payload"]["answer"])
        answer = self.delegate.generate_answer(question, context)
        self.store.write(
            case_id=self.case_id,
            split=self.split,
            question=self.question,
            stage="generation_completed",
            payload={
                "answer": answer,
                "model": self.generation_model,
                "usage": dict(self.gateway.generation_usage),
            },
            attempt_number=self.attempt_number,
            previous_path=self.store.path(self.case_id, "retrieval_completed"),
        )
        return answer


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("artifact must be a JSON object")
    return value


def _numeric_usage_sum(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, int | float]:
    keys = set(first) | set(second)
    return {
        key: first.get(key, 0) + second.get(key, 0)
        for key in sorted(keys)
        if isinstance(first.get(key, 0), (int, float))
        and not isinstance(first.get(key, 0), bool)
        and isinstance(second.get(key, 0), (int, float))
        and not isinstance(second.get(key, 0), bool)
    }


def validate_resume_preflight(
    binding_path: Path = DEFAULT_BINDING,
    review_path: Path = DEFAULT_REVIEW,
    parent_path: Path = DEFAULT_PARENT,
) -> dict[str, Any]:
    binding, validation, holdout_path, rubric_path, _ = load_evaluation_binding(
        binding_path
    )
    review = validate_review_record(review_path, validation)
    parent_sha256 = _sha256(parent_path)
    if parent_sha256 != EXPECTED_PARENT_SHA256:
        raise ValueError("parent result hash differs from the interrupted attempt")
    parent = _load_json(parent_path)
    if parent.get("status") != "interrupted_on_technical_error":
        raise ValueError("parent result is not an interrupted evaluation")
    if parent.get("holdout_sha256") != validation["sha256"]:
        raise ValueError("parent result targets a different holdout")
    if parent.get("rubric_sha256") != validation["rubric_sha256"]:
        raise ValueError("parent result targets a different rubric")

    holdout = _load_json(holdout_path)
    ordered = [
        *(("positive", case) for case in holdout["positive"]),
        *(("negative", case) for case in holdout["negative"]),
    ]
    parent_cases = parent.get("cases")
    if not isinstance(parent_cases, list) or len(parent_cases) != 3:
        raise ValueError("parent result has an unexpected case history")
    if [case.get("id") for case in parent_cases] != [
        ordered[0][1]["id"],
        ordered[1][1]["id"],
        ordered[2][1]["id"],
    ]:
        raise ValueError("parent result case order differs from the holdout")
    if [case.get("status") for case in parent_cases] != [
        "completed",
        "completed",
        "technical_error",
    ]:
        raise ValueError("parent result statuses do not define the expected resume point")
    failed = parent_cases[2]
    if failed.get("stage") != "generation" or failed.get("error_category") != "timeout":
        raise ValueError("parent interruption is not the recorded generation timeout")
    if parent.get("embedding_requests") != 3 or parent.get("generation_requests") != 3:
        raise ValueError("parent request counts differ from the immutable evidence")

    resume_index = 2
    remaining = ordered[resume_index:]
    if len(remaining) != MAX_NEW_EMBEDDING_REQUESTS:
        raise ValueError("resume budget differs from the number of remaining cases")
    return {
        "status": "ready_for_resume_authorization",
        "system_version": SYSTEM_VERSION,
        "evaluation_version": validation["evaluation_version"],
        "binding": binding,
        "binding_sha256": validation["binding_sha256"],
        "holdout_path": holdout_path,
        "rubric_path": rubric_path,
        "validation": validation,
        "review": review,
        "parent_path": parent_path,
        "parent_sha256": parent_sha256,
        "parent": parent,
        "ordered_cases": ordered,
        "resume_index": resume_index,
        "resume_case_id": str(remaining[0][1]["id"]),
        "remaining_cases": len(remaining),
        "skipped_completed_case_ids": [
            str(parent_cases[0]["id"]),
            str(parent_cases[1]["id"]),
        ],
        "maximum_new_embedding_requests": MAX_NEW_EMBEDDING_REQUESTS,
        "maximum_new_generation_requests": MAX_NEW_GENERATION_REQUESTS,
        "maximum_new_external_requests": MAX_NEW_EXTERNAL_REQUESTS,
    }


def validate_resume_authorization(
    path: Path, *, parent_sha256: str, binding_sha256: str
) -> dict[str, Any]:
    record = _load_json(path)
    if record.get("approved") is not True:
        raise ValueError("resume authorization is not approved")
    if record.get("execution_kind") != "resume_interrupted_holdout_v2.1":
        raise ValueError("resume authorization has an unexpected execution kind")
    if record.get("parent_result_sha256") != parent_sha256:
        raise ValueError("resume authorization targets a different parent result")
    if record.get("binding_sha256") != binding_sha256:
        raise ValueError("resume authorization targets a different binding")
    if record.get("maximum_one_execution") is not True:
        raise ValueError("resume authorization must permit exactly one execution")
    fixed_budget = {
        "embedding_requests_maximum": MAX_NEW_EMBEDDING_REQUESTS,
        "generation_requests_maximum": MAX_NEW_GENERATION_REQUESTS,
        "external_requests_maximum": MAX_NEW_EXTERNAL_REQUESTS,
        "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
        "automatic_retries": 0,
    }
    attempt_number = int(record.get("attempt_number", 2))
    budget = record.get("budget")
    if attempt_number < 3:
        if budget != fixed_budget:
            raise ValueError("resume authorization has an unexpected budget")
    else:
        if not isinstance(budget, dict):
            raise ValueError("resume authorization has an unexpected budget")
        embedding_maximum = budget.get("embedding_requests_maximum")
        generation_maximum = budget.get("generation_requests_maximum")
        external_maximum = budget.get("external_requests_maximum")
        if (
            not isinstance(embedding_maximum, int)
            or not 0 <= embedding_maximum <= MAX_NEW_EMBEDDING_REQUESTS
            or not isinstance(generation_maximum, int)
            or not 0 <= generation_maximum <= MAX_NEW_GENERATION_REQUESTS
            or external_maximum != embedding_maximum + generation_maximum
            or budget.get("minimum_interval_seconds") != MINIMUM_INTERVAL_SECONDS
            or budget.get("automatic_retries") != 0
        ):
            raise ValueError("resume authorization has an unexpected budget")
    authorized_by = record.get("authorized_by")
    if not isinstance(authorized_by, str) or len(authorized_by.strip()) < 3:
        raise ValueError("resume authorization is not attributable")
    authorized_at = record.get("authorized_at")
    if not isinstance(authorized_at, str):
        raise ValueError("resume authorization has no timestamp")
    try:
        parsed = datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("resume authorization timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("resume authorization timestamp needs a timezone")
    return record


def validate_checkpoint_authorization_scope(
    authorization: dict[str, Any],
    *,
    output_path: Path,
    checkpoint_directory: Path,
    checkpoint_store: CheckpointStore,
    remaining_cases: Sequence[tuple[str, dict[str, Any]]],
) -> None:
    """Bind attempt 3+ to a new output and exact reusable checkpoint hashes."""
    attempt_number = int(authorization.get("attempt_number", 2))
    if attempt_number < 3:
        return
    if output_path.parent.resolve() != ROOT.resolve():
        raise ValueError("resume output must remain in the evaluation directory")
    if checkpoint_directory.parent.resolve() != ROOT.resolve():
        raise ValueError("checkpoint directory must remain in the evaluation directory")
    if authorization.get("output_artifact") != output_path.name:
        raise ValueError("resume authorization targets a different output")
    if authorization.get("checkpoint_directory") != checkpoint_directory.name:
        raise ValueError("resume authorization targets a different checkpoint directory")
    authorized = authorization.get("reusable_checkpoint_sha256")
    if not isinstance(authorized, dict) or not all(
        isinstance(name, str) and isinstance(digest, str)
        for name, digest in authorized.items()
    ):
        raise ValueError("resume authorization has no checkpoint hash inventory")
    actual = {
        path.name: _sha256(path)
        for path in sorted(checkpoint_store.root.glob("*.json"))
        if path.is_file()
    }
    if authorized != actual:
        raise ValueError("authorized checkpoint hash inventory differs")
    prior_attempts = {
        DEFAULT_PARENT.name: _sha256(DEFAULT_PARENT),
        **{
            path.name: _sha256(path)
            for path in sorted(ROOT.glob("independent-holdout-v2.1-resume-*-results.json"))
            if path.resolve() != output_path.resolve()
        },
    }
    if authorization.get("prior_attempt_sha256") != prior_attempts:
        raise ValueError("resume authorization omits or changes prior attempts")
    expected_embedding = 0
    expected_generation = 0
    for split, case in remaining_cases:
        case_id = str(case["id"])
        question = str(case["question"])
        stages = checkpoint_store.existing_stages(case_id)
        if "generation_completed" in stages:
            continue
        if "retrieval_completed" in stages:
            retrieval = checkpoint_store.load(
                case_id=case_id,
                split=split,
                question=question,
                stage="retrieval_completed",
            )
            if deserialize_trace(retrieval["payload"]).selected:
                expected_generation += 1
            continue
        if "embedding_completed" not in stages:
            expected_embedding += 1
        expected_generation += 1
    expected_budget = {
        "embedding_requests_maximum": expected_embedding,
        "generation_requests_maximum": expected_generation,
        "external_requests_maximum": expected_embedding + expected_generation,
        "minimum_interval_seconds": MINIMUM_INTERVAL_SECONDS,
        "automatic_retries": 0,
    }
    if authorization.get("budget") != expected_budget:
        raise ValueError("resume authorization budget differs from checkpoint state")


def _combined_metrics(
    parent: dict[str, Any], resume_cases: list[dict[str, Any]]
) -> dict[str, Any]:
    immutable_completed = [
        case for case in parent["cases"] if case.get("status") == "completed"
    ]
    return calculate_metrics([*immutable_completed, *resume_cases])


def execute_resume(
    *,
    binding_path: Path,
    review_path: Path,
    parent_path: Path,
    authorization_path: Path,
    output_path: Path,
    checkpoint_directory: Path,
    approved_budget: int,
) -> int:
    preflight = validate_resume_preflight(binding_path, review_path, parent_path)
    authorization = validate_resume_authorization(
        authorization_path,
        parent_sha256=preflight["parent_sha256"],
        binding_sha256=preflight["binding_sha256"],
    )
    if approved_budget != authorization["budget"]["external_requests_maximum"]:
        raise ValueError("CLI budget differs from the authorized external budget")
    ensure_output_available(output_path)

    settings = load_settings()
    if settings.min_score != 0.57:
        raise ValueError("RAG_MIN_SCORE must remain 0.57 for this evaluation")
    if settings.expected_vector_size != 1024:
        raise ValueError("RAG_VECTOR_SIZE must remain 1024 for this evaluation")
    if settings.min_request_interval_seconds < MINIMUM_INTERVAL_SECONDS:
        raise ValueError("RAG_REQUEST_INTERVAL_SECONDS must be at least 2")

    freeze = validate_freeze_manifest(FREEZE_MANIFEST, ROOT.parents[2])
    checkpoint_binding = build_checkpoint_binding(
        system_version=SYSTEM_VERSION,
        evaluation_version=preflight["evaluation_version"],
        holdout_sha256=preflight["validation"]["sha256"],
        rubric_sha256=preflight["validation"]["rubric_sha256"],
        binding_sha256=preflight["binding_sha256"],
        freeze_manifest_sha256=freeze["freeze_manifest_sha256"],
        corpus_sha256=freeze["corpus_sha256"],
    )
    checkpoint_store = CheckpointStore(checkpoint_directory, checkpoint_binding)
    attempt_number = int(authorization.get("attempt_number", 2))
    if attempt_number < 2:
        raise ValueError("resume attempt number must be at least two")
    remaining = preflight["ordered_cases"][preflight["resume_index"] :]
    validate_checkpoint_authorization_scope(
        authorization,
        output_path=output_path,
        checkpoint_directory=checkpoint_directory,
        checkpoint_store=checkpoint_store,
        remaining_cases=remaining,
    )

    parent = preflight["parent"]
    rubric = _load_json(preflight["rubric_path"])
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "system_version": SYSTEM_VERSION,
        "evaluation_version": preflight["evaluation_version"],
        "run_kind": "resume_after_technical_error",
        "attempt_number": attempt_number,
        "parent_result": parent_path.name,
        "parent_result_sha256": preflight["parent_sha256"],
        "authorization": authorization_path.name,
        "authorization_sha256": _sha256(authorization_path),
        "prior_attempt_sha256": authorization.get("prior_attempt_sha256", {}),
        "resumed_from_case_id": preflight["resume_case_id"],
        "skipped_immutable_case_ids": preflight["skipped_completed_case_ids"],
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "budget": authorization["budget"],
        "checkpoint_binding": checkpoint_binding,
        "checkpoint_directory": checkpoint_directory.name,
        "checkpoint_stages": {},
        "cases": [],
        "attempt_metrics": {},
        "combined_metrics": {},
        "new_usage": {},
        "cumulative_usage": {},
        "fidelity_rubric": rubric,
    }
    _persist(output_path, artifact)

    gateway = FourGeeksHTTPClient(settings)
    qdrant = QdrantHTTPVectorStore(
        settings.qdrant_url, timeout=settings.request_timeout_seconds
    )
    exit_code = 0
    try:
        for split, case in remaining:
            case_id = str(case["id"])
            question = str(case["question"])
            embedding_provider = _CheckpointedEmbeddingProvider(
                gateway,
                checkpoint_store,
                case_id=case_id,
                split=split,
                question=question,
                attempt_number=attempt_number,
                model=settings.embedding_model,
            )
            base_service = RAGService(
                embedding_provider,
                _BudgetedGenerationProvider(gateway),
                qdrant,
                default_min_score=settings.min_score,
                expected_vector_size=settings.expected_vector_size,
            )
            service = _CheckpointedEvaluationService(
                base_service,
                checkpoint_store,
                gateway,
                case_id=case_id,
                split=split,
                question=question,
                attempt_number=attempt_number,
                generation_model=settings.generation_model,
            )
            try:
                artifact["cases"].append(
                    _case_result(case=case, split=split, service=service)
                )
            except HoldoutCaseError as case_error:
                exc: RAGError = case_error.cause
                artifact["cases"].append(
                    {
                        "id": str(case["id"]),
                        "split": split,
                        "status": "technical_error",
                        "stage": case_error.stage,
                        "error_category": (
                            exc.category
                            if isinstance(exc, RAGProviderError)
                            else "provider_error"
                        ),
                        "http_status": (
                            exc.status_code if isinstance(exc, RAGProviderError) else None
                        ),
                    }
                )
                artifact["status"] = "interrupted_on_technical_error"
                exit_code = 1

            artifact["checkpoint_stages"][case_id] = list(
                checkpoint_store.existing_stages(case_id)
            )

            artifact["new_embedding_requests"] = gateway.embedding_request_count
            artifact["new_generation_requests"] = gateway.generation_request_count
            artifact["new_usage"] = {
                "embedding": gateway.embedding_usage,
                "generation": gateway.generation_usage,
            }
            parent_usage = parent.get("usage", {})
            artifact["cumulative_usage"] = {
                "embedding": _numeric_usage_sum(
                    dict(parent_usage.get("embedding", {})), gateway.embedding_usage
                ),
                "generation": _numeric_usage_sum(
                    dict(parent_usage.get("generation", {})), gateway.generation_usage
                ),
            }
            artifact["attempt_metrics"] = calculate_metrics(artifact["cases"])
            artifact["combined_metrics"] = _combined_metrics(parent, artifact["cases"])
            _persist(output_path, artifact)
            if exit_code:
                break
            if gateway.embedding_request_count > MAX_NEW_EMBEDDING_REQUESTS:
                raise RuntimeError("resume embedding request budget exceeded")
            if gateway.generation_request_count > MAX_NEW_GENERATION_REQUESTS:
                raise RuntimeError("resume generation request budget exceeded")
    finally:
        gateway.close()
        qdrant.close()

    artifact["finished_at"] = _now()
    if not exit_code:
        artifact["status"] = "completed_pending_fidelity_adjudication"
    _persist(output_path, artifact)
    print(
        json.dumps(
            {
                "status": artifact["status"],
                "new_completed_cases": artifact["attempt_metrics"].get(
                    "completed_cases"
                ),
                "combined_completed_cases": artifact["combined_metrics"].get(
                    "completed_cases"
                ),
                "new_embedding_requests": artifact.get("new_embedding_requests", 0),
                "new_generation_requests": artifact.get("new_generation_requests", 0),
                "output": str(output_path),
            },
            separators=(",", ":"),
        )
    )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--review-record", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--parent-result", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--checkpoint-directory", type=Path, default=DEFAULT_CHECKPOINT_DIRECTORY
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorization-record", type=Path)
    parser.add_argument("--approved-budget", type=int, choices=range(0, 29))
    args = parser.parse_args()

    try:
        if not args.execute:
            preflight = validate_resume_preflight(
                args.binding, args.review_record, args.parent_result
            )
            print(
                json.dumps(
                    {
                        "status": preflight["status"],
                        "parent_sha256": preflight["parent_sha256"],
                        "resume_case_id": preflight["resume_case_id"],
                        "skipped_completed_case_ids": preflight[
                            "skipped_completed_case_ids"
                        ],
                        "remaining_cases": preflight["remaining_cases"],
                        "maximum_new_embedding_requests": preflight[
                            "maximum_new_embedding_requests"
                        ],
                        "maximum_new_generation_requests": preflight[
                            "maximum_new_generation_requests"
                        ],
                        "maximum_new_external_requests": preflight[
                            "maximum_new_external_requests"
                        ],
                        "output": str(args.output),
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if args.authorization_record is None or args.approved_budget is None:
            parser.error(
                "resume execution requires --authorization-record and --approved-budget"
            )
        return execute_resume(
            binding_path=args.binding,
            review_path=args.review_record,
            parent_path=args.parent_result,
            authorization_path=args.authorization_record,
            output_path=args.output,
            checkpoint_directory=args.checkpoint_directory,
            approved_budget=args.approved_budget,
        )
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
