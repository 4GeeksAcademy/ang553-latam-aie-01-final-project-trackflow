"""Controlled indexing entry point for TrackFlow's RAG knowledge base."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from services.rag.chunking import DEFAULT_CORPUS_DIR, load_knowledge_chunks
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGConfigurationError, RAGError
from services.rag.service import RAGService


class SequentialEmbeddingProvider:
    """Enforce a minimum pause between sequential embedding requests."""

    def __init__(
        self,
        provider: FourGeeksHTTPClient,
        *,
        delay_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        self.provider = provider
        self.delay_seconds = delay_seconds
        self.sleep = sleep
        self._started = False

    def embed(self, text: str):
        if self._started and self.delay_seconds:
            self.sleep(self.delay_seconds)
        self._started = True
        return self.provider.embed(text)


def _embedding_error_category(status_code: int | None) -> str:
    if status_code == 403:
        return "http_forbidden"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and 400 <= status_code < 500:
        return "http_client_error"
    if status_code is not None and status_code >= 500:
        return "http_server_error"
    return "provider_error"


def index_knowledge_base(service: RAGService, corpus_dir: Path = DEFAULT_CORPUS_DIR):
    """Load the canonical corpus and reconcile it through the shared service."""
    return service.setup(load_knowledge_chunks(corpus_dir))


def recall_at_k(
    cases: Sequence[Mapping[str, Any]],
    retrieved: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    k: int = 3,
) -> float:
    """Calculate recall for precomputed retrieval output without model calls."""
    if not cases:
        raise ValueError("At least one positive evaluation case is required")
    hits = 0
    for case in cases:
        case_id = str(case["id"])
        expected = {
            (str(item["source_document"]), str(item["section"]))
            for item in case["expected"]
        }
        actual = {
            (str(item["source_document"]), str(item["section"]))
            for item in retrieved.get(case_id, ())[:k]
        }
        hits += bool(expected & actual)
    return hits / len(cases)


def main() -> int:
    """Run real indexing only when an operator explicitly invokes this module."""
    gateway: FourGeeksHTTPClient | None = None
    qdrant: QdrantHTTPVectorStore | None = None
    try:
        settings = load_settings()
        if settings.expected_vector_size is None:
            raise RAGConfigurationError(
                "RAG_VECTOR_SIZE must be verified before real indexing"
            )
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
        result = index_knowledge_base(service)
        output: dict[str, Any] = {
            "status": "succeeded",
            "indexed": result.indexed,
            "deleted_stale": result.deleted_stale,
            "vector_size": result.vector_size,
            "embedding_requests": gateway.embedding_request_count,
        }
        if gateway.embedding_usage:
            output["usage"] = gateway.embedding_usage
        print(json.dumps(output))
        return 0
    except RAGError as exc:
        output = {"status": "failed", "error": type(exc).__name__}
        if gateway is not None:
            output["embedding_requests"] = gateway.embedding_request_count
            output["http_status"] = gateway.last_embedding_status_code
            output["error_category"] = _embedding_error_category(
                gateway.last_embedding_status_code
            )
            if gateway.embedding_usage:
                output["usage"] = gateway.embedding_usage
        print(json.dumps(output))
        return 1
    finally:
        if gateway is not None:
            gateway.close()
        if qdrant is not None:
            qdrant.close()


if __name__ == "__main__":
    raise SystemExit(main())
