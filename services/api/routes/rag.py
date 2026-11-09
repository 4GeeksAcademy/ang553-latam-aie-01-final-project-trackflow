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
from services.rag.contracts import RAGConfigurationError, RAGProviderError
from services.rag.service import RAGService
from services.rag.runtime import rag_service_context


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
    try:
        with rag_service_context() as service:
            yield service
    except RAGConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="knowledge service unavailable",
        ) from None


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
