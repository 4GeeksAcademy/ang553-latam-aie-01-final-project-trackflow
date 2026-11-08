"""Reassess recorded answers without contacting a model or changing history."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pipelines.eval.rag.fidelity import assess_material_fidelity, parse_requirements
from services.rag.chunking import load_knowledge_chunks


ROOT = Path(__file__).parent
CASES_PATH = ROOT / "fidelity-cases.json"
RESULTS_PATH = ROOT / "generation-results.json"
OUTPUT_PATH = ROOT / "generation-fidelity-review.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_review() -> dict[str, Any]:
    profiles = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))["cases"]
    answers = {str(case["id"]): str(case["answer"]) for case in results}
    chunks = {
        (chunk.source_document, chunk.section): chunk.text
        for chunk in load_knowledge_chunks()
    }

    reviewed: list[dict[str, Any]] = []
    for profile in profiles:
        case_id = str(profile["id"])
        key = (str(profile["source_document"]), str(profile["section"]))
        requirements = parse_requirements(profile["material_requirements"])
        source_text = chunks[key]
        invalid_requirements = [
            requirement.name
            for requirement in requirements
            if not any(
                re.search(pattern, source_text, flags=re.IGNORECASE)
                for pattern in requirement.any_patterns
            )
        ]
        if invalid_requirements:
            names = ", ".join(invalid_requirements)
            raise ValueError(
                f"requirements not grounded in source for {case_id}: {names}"
            )
        assessment = assess_material_fidelity(answers[case_id], requirements)
        reviewed.append(
            {
                "id": case_id,
                "pass": assessment.passed,
                "matched_requirements": list(assessment.matched),
                "missing_requirements": list(assessment.missing),
            }
        )

    return {
        "schema_version": 1,
        "kind": "post_hoc_offline_review",
        "generation_results_sha256": _sha256(RESULTS_PATH),
        "fidelity_cases_sha256": _sha256(CASES_PATH),
        "reviewed": len(reviewed),
        "passed": sum(bool(case["pass"]) for case in reviewed),
        "cases": reviewed,
    }


def main() -> int:
    review = build_review()
    pending = OUTPUT_PATH.with_suffix(".json.pending")
    pending.write_text(
        json.dumps(review, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    pending.replace(OUTPUT_PATH)
    print(
        json.dumps(
            {
                "status": "succeeded",
                "reviewed": review["reviewed"],
                "passed": review["passed"],
                "output": str(OUTPUT_PATH),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
