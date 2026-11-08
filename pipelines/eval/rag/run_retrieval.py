"""Run one frozen TrackFlow retrieval split and persist sanitized evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pipelines.rag import SequentialEmbeddingProvider
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGError
from services.rag.service import RAGService


EVAL_PATH = Path(__file__).with_name("test-queries.json")
ALLOWED_SPLITS = ("calibration", "evaluation", "negative")


def _load_cases(split: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = EVAL_PATH.read_bytes()
    data = json.loads(raw)
    cases = data[split]
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{split} must contain cases")
    ids = [str(case["id"]) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{split} case IDs must be unique")
    return data, cases, hashlib.sha256(raw).hexdigest()


def _expected_pairs(case: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item["source_document"]), str(item["section"]))
        for item in case.get("expected", ())
    }


def run_split(split: str, threshold: float | None) -> dict[str, Any]:
    _, cases, source_hash = _load_cases(split)
    settings = load_settings()
    gateway = FourGeeksHTTPClient(settings)
    qdrant = QdrantHTTPVectorStore(
        settings.qdrant_url, timeout=settings.request_timeout_seconds
    )
    embedding_provider = SequentialEmbeddingProvider(gateway)
    service = RAGService(
        embedding_provider,
        gateway,
        qdrant,
        expected_vector_size=settings.expected_vector_size,
    )
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            retrieved = service.retrieve(str(case["question"]), k=3, min_score=threshold)
            top = [
                {
                    "rank": rank,
                    "point_id": item.chunk.point_id,
                    "source_document": item.chunk.source_document,
                    "section": item.chunk.section,
                    "chunk_index": item.chunk.chunk_index,
                    "score": item.score,
                    "text": item.chunk.text,
                }
                for rank, item in enumerate(retrieved, start=1)
            ]
            expected = _expected_pairs(case)
            if split == "negative":
                passed = not top
            else:
                passed = bool(
                    expected
                    & {(item["source_document"], item["section"]) for item in top}
                )
            results.append(
                {
                    "id": str(case["id"]),
                    "question": str(case["question"]),
                    "expected": list(case.get("expected", ())),
                    "top3": top,
                    "pass": passed,
                }
            )
    finally:
        gateway.close()
        qdrant.close()

    passed = sum(bool(item["pass"]) for item in results)
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "split": split,
        "source_sha256": source_hash,
        "threshold": threshold,
        "k": 3,
        "cases": results,
        "passed": passed,
        "total": len(results),
        "rate": passed / len(results),
        "embedding_requests": gateway.embedding_request_count,
    }
    if gateway.embedding_usage:
        artifact["usage"] = gateway.embedding_usage
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=ALLOWED_SPLITS, required=True)
    parser.add_argument("--threshold", type=float)
    args = parser.parse_args()
    if args.split == "calibration" and args.threshold is not None:
        parser.error("calibration must retrieve unfiltered scores")
    if args.split != "calibration" and args.threshold is None:
        parser.error("evaluation and negative splits require --threshold")
    if args.threshold is not None and not 0 <= args.threshold <= 1:
        parser.error("threshold must be between 0 and 1")

    try:
        artifact = run_split(args.split, args.threshold)
    except (RAGError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1

    output = Path(__file__).with_name(f"{args.split}-results.json")
    output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    summary: dict[str, Any] = {
        "status": "succeeded",
        "split": args.split,
        "passed": artifact["passed"],
        "total": artifact["total"],
        "rate": artifact["rate"],
        "embedding_requests": artifact["embedding_requests"],
        "output": str(output),
    }
    if "usage" in artifact:
        summary["usage"] = artifact["usage"]
    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
