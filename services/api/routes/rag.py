"""Authenticated TrackFlow knowledge-base query endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.customer_support import CustomerSupportAgent, get_customer_support_agent
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import load_settings
from services.rag.contracts import RAGConfigurationError, RAGProviderError
from services.rag.service import RAGService
from services.rag.resilience import (
    BudgetedGenerationProvider,
    BudgetedVectorStore,
    EmbeddingRetryPolicy,
    QueryDeadline,
    ResilientEmbeddingProvider,
    process_query_embedding_cache,
)


router = APIRouter(prefix="/knowledge", tags=["Knowledge Base"])


class KnowledgeQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized


class KnowledgeSourceResponse(BaseModel):
    source_document: str
    section: str
    retrieval_kind: Literal["primary", "complementary"]


class KnowledgeQueryResponse(BaseModel):
    answer: str
    insufficient_context: bool
    sources: list[KnowledgeSourceResponse]


def require_knowledge_reader(
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> UserInDB:
    """Limit the internal knowledge base to existing trusted roles."""
    if user.role not in {Role.admin, Role.manager}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin or manager role required",
        )
    return user


def get_rag_service() -> Iterator[RAGService]:
    """Build request-scoped HTTP adapters without exposing their configuration."""
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
        gateway = FourGeeksHTTPClient(settings)
        qdrant = QdrantHTTPVectorStore(
            settings.qdrant_url,
            timeout=settings.request_timeout_seconds,
        )
        deadline = QueryDeadline(settings.total_budget_seconds)
        downstream_reserve = (
            settings.qdrant_timeout_seconds
            + settings.generation_timeout_seconds
            + settings.response_reserve_seconds
        )
        embedding = ResilientEmbeddingProvider(
            gateway,
            deadline=deadline,
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
        generation = BudgetedGenerationProvider(
            gateway,
            deadline,
            settings.generation_timeout_seconds,
            settings.response_reserve_seconds,
        )
        vector_store = BudgetedVectorStore(
            qdrant,
            deadline,
            settings.qdrant_timeout_seconds,
            settings.generation_timeout_seconds + settings.response_reserve_seconds,
        )
        yield RAGService(
            embedding,
            generation,
            vector_store,
            default_min_score=settings.min_score,
            expected_vector_size=settings.expected_vector_size,
        )
    except RAGConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge service unavailable",
        ) from None
    finally:
        if gateway is not None:
            gateway.close()
        if qdrant is not None:
            qdrant.close()


@router.post(
    "/query",
    response_model=KnowledgeQueryResponse,
    dependencies=[Depends(require_knowledge_reader)],
    responses={
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "The authenticated role is not authorized."},
        503: {"description": "The knowledge service is not available."},
    },
)
def query_knowledge_base(
    payload: KnowledgeQueryRequest,
    request: Request,
    response: Response,
    service: Annotated[RAGService, Depends(get_rag_service)],
    agent: Annotated[CustomerSupportAgent, Depends(get_customer_support_agent)],
) -> KnowledgeQueryResponse:
    run_id = str(uuid4())
    try:
        execution = agent.execute(
            payload.question,
            service,
            request_id=getattr(request.state, "request_id", None),
            run_id=run_id,
        )
    except (RAGConfigurationError, RAGProviderError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge service unavailable",
            headers={"X-TrackFlow-Run-ID": run_id},
        ) from None

    response.headers["X-TrackFlow-Run-ID"] = run_id
    result = execution.result
    return KnowledgeQueryResponse(
        answer=result.answer,
        insufficient_context=result.insufficient_context,
        sources=[
            KnowledgeSourceResponse(
                source_document=source.source_document,
                section=source.section,
                retrieval_kind=source.retrieval_kind,
            )
            for source in result.sources
        ],
    )
