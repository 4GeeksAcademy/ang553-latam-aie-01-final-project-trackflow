"""Authenticated multi-source customer-support query endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from agents.customer_support.external_graph import (
    ExternalSupportAgent,
    ExternalSupportResult,
    SupportUnavailable,
    get_external_support_agent,
)
from agents.customer_support.mcp_tools import SupportMCPSettings, SupportMCPTools
from agents.customer_support.tools import apply_statement_timeout
from services.api.access_control import build_access_context
from services.api.database import get_db
from services.api.support_oidc import SupportIdentity, require_support_reader
from services.rag.resilience import QueryDeadline
from services.rag.runtime import rag_service_context


router = APIRouter(prefix="/support", tags=["Customer Support"])
SUPPORT_QUERY_BUDGET_SECONDS = 80.0


class SupportQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized


class SupportTicketResponse(BaseModel):
    status: Literal["ok", "not_found", "timeout", "unavailable"]
    incident_id: str | None = None
    incident_status: str | None = None
    category: str | None = None
    version: int | None = None
    updated_at: datetime | None = None


class SupportInventoryResponse(BaseModel):
    status: Literal["ok", "not_found", "timeout", "unavailable"]
    product_id: int | None = None
    name: str | None = None
    sku: str | None = None
    warehouse: str | None = None
    current_stock: int | None = None


class SupportKnowledgeSourceResponse(BaseModel):
    source_document: str
    section: str
    retrieval_kind: Literal["primary", "complementary"]


class SupportKnowledgeResponse(BaseModel):
    status: Literal["ok", "not_found", "timeout", "unavailable"]
    answer: str | None = None
    insufficient_context: bool = False
    sources: list[SupportKnowledgeSourceResponse] = Field(default_factory=list)


class SupportQueryResponse(BaseModel):
    answer: str
    route: Literal["clarification", "knowledge", "ticket", "inventory", "mixed"]
    partial: bool
    needs_clarification: bool
    ticket: SupportTicketResponse | None = None
    inventory: SupportInventoryResponse | None = None
    knowledge: SupportKnowledgeResponse | None = None


def get_support_mcp_tools(
    identity: Annotated[SupportIdentity, Depends(require_support_reader)],
) -> SupportMCPTools:
    try:
        settings = SupportMCPSettings.from_env()
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support service unavailable",
        ) from None
    return SupportMCPTools(identity.bearer_for_mcp(), settings)


def _to_response(result: ExternalSupportResult) -> SupportQueryResponse:
    ticket = (
        None
        if result.ticket is None
        else SupportTicketResponse(
            status=result.ticket.status.value,
            incident_id=result.ticket.incident_id,
            incident_status=result.ticket.incident_status,
            category=result.ticket.category,
            version=result.ticket.version,
            updated_at=result.ticket.updated_at,
        )
    )
    inventory = (
        None
        if result.inventory is None
        else SupportInventoryResponse(
            status=result.inventory.status.value,
            product_id=result.inventory.product_id,
            name=result.inventory.name,
            sku=result.inventory.sku,
            warehouse=result.inventory.warehouse,
            current_stock=result.inventory.current_stock,
        )
    )
    knowledge = None
    if result.knowledge_status is not None:
        knowledge = SupportKnowledgeResponse(
            status=result.knowledge_status,
            answer=None if result.knowledge is None else result.knowledge.answer,
            insufficient_context=(
                False
                if result.knowledge is None
                else result.knowledge.insufficient_context
            ),
            sources=(
                []
                if result.knowledge is None
                else [
                    SupportKnowledgeSourceResponse(
                        source_document=source.source_document,
                        section=source.section,
                        retrieval_kind=source.retrieval_kind,
                    )
                    for source in result.knowledge.sources
                ]
            ),
        )
    return SupportQueryResponse(
        answer=result.answer,
        route=result.route,
        partial=result.partial,
        needs_clarification=result.needs_clarification,
        ticket=ticket,
        inventory=inventory,
        knowledge=knowledge,
    )


@router.post(
    "/query",
    response_model=SupportQueryResponse,
    responses={
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "The authenticated role is not authorized."},
        503: {"description": "All selected support sources are unavailable."},
    },
)
async def query_support(
    payload: SupportQueryRequest,
    request: Request,
    response: Response,
    identity: Annotated[SupportIdentity, Depends(require_support_reader)],
    session: Annotated[Session, Depends(get_db)],
    agent: Annotated[ExternalSupportAgent, Depends(get_external_support_agent)],
    mcp_tools: Annotated[SupportMCPTools, Depends(get_support_mcp_tools)],
) -> SupportQueryResponse:
    deadline = QueryDeadline(SUPPORT_QUERY_BUDGET_SECONDS)
    try:
        apply_statement_timeout(session, deadline)
        access = build_access_context(session, identity.user)
    except (SQLAlchemyError, TimeoutError):
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support service unavailable",
        ) from None

    run_id = str(uuid4())
    try:
        execution = await agent.execute(
            payload.question,
            session=session,
            access=access,
            deadline=deadline,
            rag_factory=lambda: rag_service_context(deadline=deadline),
            request_id=getattr(request.state, "request_id", None),
            run_id=run_id,
            ticket_tool=mcp_tools.lookup_ticket,
            inventory_tool=mcp_tools.lookup_inventory_product,
        )
    except SupportUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support service unavailable",
            headers={"X-TrackFlow-Run-ID": run_id},
        ) from None
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support service unavailable",
            headers={"X-TrackFlow-Run-ID": run_id},
        ) from None
    response.headers["X-TrackFlow-Run-ID"] = run_id
    return _to_response(execution.result)
