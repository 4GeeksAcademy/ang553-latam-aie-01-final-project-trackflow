"""Run the frozen grounded-generation rubric and persist real evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from services.rag.chunking import load_knowledge_chunks
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import load_settings
from services.rag.contracts import RAGError, RetrievalResult
from services.rag.service import RAGService


CASES_PATH = Path(__file__).with_name("generation-cases.json")
OUTPUT_PATH = Path(__file__).with_name("generation-results.json")


def _persist(artifact: dict[str, Any]) -> None:
    pending = OUTPUT_PATH.with_suffix(".json.pending")
    pending.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending.replace(OUTPUT_PATH)


def main() -> int:
    raw = CASES_PATH.read_bytes()
    data = json.loads(raw)
    cases = data["cases"]
    chunks = {
        (chunk.source_document, chunk.section): chunk
        for chunk in load_knowledge_chunks()
    }
    settings = load_settings()
    gateway = FourGeeksHTTPClient(settings)
    service = RAGService(gateway, gateway, vector_store=None)  # type: ignore[arg-type]
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "model": settings.generation_model,
        "cases": [],
        "completed": 0,
        "total": len(cases),
    }
    try:
        for case in cases:
            key = (str(case["source_document"]), str(case["section"]))
            chunk = chunks[key]
            context = (RetrievalResult(chunk, 1.0),)
            answer = service.generate_answer(str(case["question"]), context)
            checks = [
                bool(re.search(pattern, answer, flags=re.IGNORECASE))
                for pattern in case["required_patterns"]
            ]
            artifact["cases"].append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "source_document": case["source_document"],
                    "section": case["section"],
                    "required_patterns": case["required_patterns"],
                    "pattern_checks": checks,
                    "automatic_pass": all(checks),
                    "answer": answer,
                }
            )
            artifact["completed"] = len(artifact["cases"])
            artifact["generation_requests"] = gateway.generation_request_count
            if gateway.generation_usage:
                artifact["usage"] = gateway.generation_usage
            _persist(artifact)
    except (RAGError, OSError, KeyError, TypeError, ValueError) as exc:
        artifact["status"] = "failed"
        artifact["error"] = type(exc).__name__
        artifact["http_status"] = gateway.last_generation_status_code
        artifact["generation_requests"] = gateway.generation_request_count
        if gateway.generation_usage:
            artifact["usage"] = gateway.generation_usage
        _persist(artifact)
        print(
            json.dumps(
                {
                    "status": "failed",
                    "completed": artifact["completed"],
                    "generation_requests": gateway.generation_request_count,
                    "http_status": gateway.last_generation_status_code,
                },
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        gateway.close()

    artifact["status"] = "succeeded"
    artifact["automatic_passed"] = sum(
        bool(case["automatic_pass"]) for case in artifact["cases"]
    )
    artifact["generation_requests"] = gateway.generation_request_count
    if gateway.generation_usage:
        artifact["usage"] = gateway.generation_usage
    _persist(artifact)
    print(
        json.dumps(
            {
                "status": "succeeded",
                "completed": artifact["completed"],
                "automatic_passed": artifact["automatic_passed"],
                "generation_requests": artifact["generation_requests"],
                "usage": artifact.get("usage"),
                "output": str(OUTPUT_PATH),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
