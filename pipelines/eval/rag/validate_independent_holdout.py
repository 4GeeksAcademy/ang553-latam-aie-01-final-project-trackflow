"""Validate and fingerprint the prospective rag-contextual-v2 holdout offline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.rag.chunking import load_knowledge_chunks


ROOT = Path(__file__).resolve().parent
HOLDOUT_PATH = ROOT / "independent-holdout-v1.json"
RUBRIC_PATH = ROOT / "independent-holdout-v1-rubric.json"
HISTORICAL_PATHS = (ROOT / "test-queries.json", ROOT / "generation-cases.json")


def _historical_questions() -> set[str]:
    questions: set[str] = set()
    for path in HISTORICAL_PATHS:
        data = json.loads(path.read_text(encoding="utf-8"))
        if path.name == "test-queries.json":
            groups = (data["calibration"], data["evaluation"], data["negative"])
            cases = [case for group in groups for case in group]
        else:
            cases = data["cases"]
        questions.update(str(case["question"]).strip().casefold() for case in cases)
    return questions


def validate_holdout(
    holdout_path: Path = HOLDOUT_PATH,
    rubric_path: Path = RUBRIC_PATH,
    *,
    expected_evaluation_version: str = "rag-contextual-v2-independent-v1",
    expected_target_system_version: str | None = None,
    expected_sha256: str | None = None,
    expected_rubric_sha256: str | None = None,
) -> dict[str, Any]:
    raw = holdout_path.read_bytes()
    data = json.loads(raw)
    rubric_raw = rubric_path.read_bytes()
    rubric = json.loads(rubric_raw)
    positive = data["positive"]
    negative = data["negative"]
    cases = [*positive, *negative]
    ids = [str(case["id"]) for case in cases]
    questions = [str(case["question"]).strip() for case in cases]

    if data["evaluation_version"] != expected_evaluation_version:
        raise ValueError("unexpected evaluation version")
    if (
        expected_target_system_version is not None
        and data.get("target_system_version") != expected_target_system_version
    ):
        raise ValueError("unexpected target system version")
    if len(positive) < 10 or len(negative) < 3:
        raise ValueError("holdout lacks required positive or negative coverage")
    if len(ids) != len(set(ids)) or len(questions) != len(set(questions)):
        raise ValueError("holdout IDs and questions must be unique")
    if _historical_questions() & {question.casefold() for question in questions}:
        raise ValueError("holdout reuses a historical question")

    canonical_pairs = {
        (chunk.source_document, chunk.section) for chunk in load_knowledge_chunks()
    }
    expected_pairs = {
        (str(item["source_document"]), str(item["section"]))
        for case in positive
        for item in case["expected"]
    }
    if not expected_pairs <= canonical_pairs:
        raise ValueError("holdout contains a non-canonical reference")
    if {source for source, _ in expected_pairs} != {
        "sla-delivery",
        "returns-policy",
        "carrier-coverage",
        "storage-pricing",
    }:
        raise ValueError("holdout does not cover all four documents")
    if ("sla-delivery", "high-demand-periods") not in expected_pairs:
        raise ValueError("holdout lacks a high-demand case")
    if any(case["expected"] for case in negative):
        raise ValueError("negative cases must not declare expected references")
    rubric_ids = [str(case["id"]) for case in rubric["cases"]]
    if rubric["evaluation_version"] != data["evaluation_version"]:
        raise ValueError("holdout and rubric versions differ")
    if set(rubric_ids) != set(ids) or len(rubric_ids) != len(set(rubric_ids)):
        raise ValueError("rubric coverage does not match the holdout")
    if any(not case.get("required_facts") for case in rubric["cases"]):
        raise ValueError("every rubric case needs material facts")

    sha256 = hashlib.sha256(raw).hexdigest()
    rubric_sha256 = hashlib.sha256(rubric_raw).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ValueError("holdout hash differs from the evaluation binding")
    if (
        expected_rubric_sha256 is not None
        and rubric_sha256 != expected_rubric_sha256
    ):
        raise ValueError("rubric hash differs from the evaluation binding")

    return {
        "status": "valid",
        "evaluation_version": data["evaluation_version"],
        "sha256": sha256,
        "rubric_sha256": rubric_sha256,
        "positive": len(positive),
        "negative": len(negative),
        "documents": sorted({source for source, _ in expected_pairs}),
    }


def main() -> int:
    print(json.dumps(validate_holdout(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
