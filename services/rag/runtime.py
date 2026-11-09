"""Request-scoped construction of the resilient TrackFlow RAG service."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGConfigurationError
from services.rag.resilience import (
    BudgetedGenerationProvider,
    BudgetedVectorStore,
    EmbeddingRetryPolicy,
    QueryDeadline,
    ResilientEmbeddingProvider,
    process_query_embedding_cache,
)
from services.rag.service import RAGService


@contextmanager
def rag_service_context(
    *, deadline: QueryDeadline | None = None
) -> Iterator[RAGService]:
    """Build adapters lazily and optionally share an already-started deadline."""
    gateway: FourGeeksHTTPClient | None = None
    qdrant: QdrantHTTPVectorStore | None = None
    try:
        settings = load_settings()
        if settings.min_score is None:
            raise RAGConfigurationError(
                "RAG_MIN_SCORE must be calibrated before serving live queries"
            )
        if settings.expected_vector_size is None:
            raise RAGConfigurationError(
                "RAG_VECTOR_SIZE is required for resilient query caching"
            )
        shared_deadline = deadline or QueryDeadline(settings.total_budget_seconds)
        gateway = FourGeeksHTTPClient(settings)
        qdrant = QdrantHTTPVectorStore(
            settings.qdrant_url,
            timeout=settings.request_timeout_seconds,
        )
        downstream_reserve = (
            settings.qdrant_timeout_seconds
            + settings.generation_timeout_seconds
            + settings.response_reserve_seconds
        )
        embedding = ResilientEmbeddingProvider(
            gateway,
            deadline=shared_deadline,
            cache=process_query_embedding_cache(
                settings.embedding_cache_capacity,
                settings.embedding_cache_ttl_seconds,
            ),
            cache_key_context={
                "model": settings.embedding_model,
                "isolation_scope": "trackflow:en",
            },
            expected_dimension=settings.expected_vector_size,
            policy=EmbeddingRetryPolicy(
                attempt_timeout_seconds=settings.embedding_attempt_timeout_seconds,
                downstream_reserve_seconds=downstream_reserve,
                backoff_seconds=settings.retry_backoff_seconds,
                max_backoff_seconds=settings.retry_max_backoff_seconds,
                jitter_seconds=settings.retry_jitter_seconds,
                retry_after_cap_seconds=settings.retry_after_cap_seconds,
                minimum_retry_timeout_seconds=(
                    settings.embedding_min_retry_timeout_seconds
                ),
            ),
        )
        yield RAGService(
            embedding,
            BudgetedGenerationProvider(
                gateway,
                shared_deadline,
                settings.generation_timeout_seconds,
                settings.response_reserve_seconds,
            ),
            BudgetedVectorStore(
                qdrant,
                shared_deadline,
                settings.qdrant_timeout_seconds,
                settings.generation_timeout_seconds
                + settings.response_reserve_seconds,
            ),
            default_min_score=settings.min_score,
            expected_vector_size=settings.expected_vector_size,
        )
    finally:
        if gateway is not None:
            gateway.close()
        if qdrant is not None:
            qdrant.close()
