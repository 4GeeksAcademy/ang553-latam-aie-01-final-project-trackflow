"""Authenticated multi-source customer-support query endpoint."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import logging
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
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
from services.api.agent_memory.consent import (
    ConsentClassificationInvalid,
    ConsentCoordinator,
    ConservativeTextConsentClassifier,
)
from services.api.agent_memory.contracts import (
    ConsentAction,
    ConsentClassification,
    ConsentDisposition,
    ConsentRequest,
    ConsentResult,
    MemoryCategory,
    MemoryProposalCreate,
    MemoryProposalRecord,
    MemoryScope,
    ProposalStatus,
)
from services.api.agent_memory.service import (
    MemoryConflict,
    MemoryForbidden,
    MemoryNotFound,
    MemoryPersistenceError,
    MemoryPolicyViolation,
    MemoryStore,
    canonical_key_for,
)
from services.api.database import get_db
from services.api.support_oidc import SupportIdentity, require_support_reader
from services.incidents.models import clients
from services.rag.contracts import RAGProviderError
from services.rag.resilience import QueryDeadline
from services.rag.runtime import rag_service_context


router = APIRouter(prefix="/support", tags=["Customer Support"])
SUPPORT_QUERY_BUDGET_SECONDS = 80.0
_CONSENT_LOGGER = logging.getLogger("agent.customer_support.consent")


class SupportQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    client_id: str | None = Field(default=None, min_length=1, max_length=36)
    memory_scope: MemoryScope | None = None

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_memory_scope(self) -> "SupportQueryRequest":
        if self.memory_scope is MemoryScope.global_ and self.client_id is not None:
            raise ValueError("global memory scope must not include client_id")
        return self


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
    conversation_id: UUID | None = None
    memory_proposal: "SupportMemoryProposalResponse | None" = None
    memory_evaluation: "SupportMemoryEvaluationResponse | None" = None
    memory_read_status: str | None = None
    memory_consent: "SupportConversationalConsentResponse | None" = None


class SupportMemoryProposalResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    scope: MemoryScope
    client_id: str | None
    category: MemoryCategory
    content: dict[str, Any]
    verification_status: Literal["user_reported"] = "user_reported"
    expires_at: datetime


class SupportMemoryEvaluationResponse(BaseModel):
    status: str
    candidate_category: MemoryCategory | None = None
    evaluator_called: bool


class SupportConversationalConsentResponse(BaseModel):
    action: ConsentAction
    disposition: ConsentDisposition
    continue_support_question: bool


class SupportMemoryDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    proposal_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=64)
    action: ConsentAction
    edited_content: dict[str, Any] | None = None
    user_text: str | None = Field(default=None, max_length=2000)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_action_payload(self) -> "SupportMemoryDecisionRequest":
        if self.action is ConsentAction.edit and self.edited_content is None:
            raise ValueError("edit requires edited_content")
        if self.action is not ConsentAction.edit and self.edited_content is not None:
            raise ValueError("edited_content is valid only for edit")
        if self.action is ConsentAction.topic_change and not (self.user_text or "").strip():
            raise ValueError("topic_change requires user_text")
        return self


class SupportMemoryDecisionResponse(BaseModel):
    conversation_id: UUID
    disposition: ConsentDisposition
    proposal: SupportMemoryProposalResponse | None = None
    memory_active: bool
    continue_support_question: bool


class SupportPendingMemoryResponse(BaseModel):
    conversation_id: UUID
    proposal: SupportMemoryProposalResponse | None = None


def get_memory_store() -> MemoryStore:
    return MemoryStore()


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


@router.get(
    "/memory/pending",
    response_model=SupportPendingMemoryResponse,
    responses={
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "The authenticated role is not authorized."},
        503: {"description": "Memory persistence unavailable."},
    },
)
def get_support_pending_memory(
    identity: Annotated[SupportIdentity, Depends(require_support_reader)],
    session: Annotated[Session, Depends(get_db)],
    memory_store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversation_id: UUID | None = None,
    proposal_id: UUID | None = None,
) -> SupportPendingMemoryResponse:
    """Recover only a currently actionable proposal visible to this identity."""
    if (conversation_id is None) == (proposal_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="exactly one memory recovery identifier is required",
        )
    try:
        access = build_access_context(session, identity.user)
        if proposal_id is not None:
            proposal = memory_store.get_proposal(session, proposal_id, access)
            recovered_conversation_id = proposal.conversation_id
            if proposal.status is not ProposalStatus.pending:
                proposal = None
        else:
            assert conversation_id is not None
            recovered_conversation_id = conversation_id
            proposal = memory_store.get_pending(session, conversation_id, access)
        if proposal is not None and proposal.expires_at <= datetime.now(UTC):
            proposal = None
        return SupportPendingMemoryResponse(
            conversation_id=recovered_conversation_id,
            proposal=_safe_proposal(proposal),
        )
    except (MemoryNotFound, MemoryConflict, MemoryForbidden, MemoryPolicyViolation) as exc:
        raise _memory_http_error(exc) from None
    except (MemoryPersistenceError, SQLAlchemyError, TimeoutError):
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support memory unavailable",
        ) from None


def _safe_proposal(
    proposal: MemoryProposalRecord | None,
) -> SupportMemoryProposalResponse | None:
    if proposal is None:
        return None
    return SupportMemoryProposalResponse(
        id=proposal.id,
        conversation_id=proposal.conversation_id,
        scope=proposal.scope,
        client_id=proposal.client_id,
        category=proposal.category,
        content=proposal.content,
        expires_at=proposal.expires_at,
    )


def _to_response(
    result: ExternalSupportResult,
    conversation_id: UUID | None = None,
    memory_proposal: MemoryProposalRecord | None = None,
    memory_consent: ConsentResult | None = None,
) -> SupportQueryResponse:
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
        conversation_id=conversation_id,
        memory_proposal=_safe_proposal(memory_proposal or result.memory_proposal),
        memory_evaluation=SupportMemoryEvaluationResponse(
            status=result.memory_evaluation.status,
            candidate_category=result.memory_evaluation.candidate_category,
            evaluator_called=result.memory_evaluation.evaluator_called,
        ),
        memory_read_status=result.memory_read_status,
        memory_consent=(
            None
            if memory_consent is None
            else SupportConversationalConsentResponse(
                action=memory_consent.classification.action,
                disposition=memory_consent.disposition,
                continue_support_question=memory_consent.continue_support_question,
            )
        ),
    )


class _FixedConsentClassifier:
    def __init__(self, classification: ConsentClassification) -> None:
        self._classification = classification

    def classify(
        self, _text: str, _proposal: MemoryProposalRecord
    ) -> ConsentClassification:
        return self._classification


def _consent_message(result: ConsentResult) -> str:
    return {
        ConsentDisposition.approved: "La memoria fue aprobada.",
        ConsentDisposition.rejected: "La propuesta de memoria fue rechazada.",
        ConsentDisposition.replacement_pending: (
            "Preparé una propuesta corregida; necesita una nueva confirmación."
        ),
        ConsentDisposition.discarded: "La propuesta pendiente fue descartada.",
        ConsentDisposition.expired: "La propuesta de memoria ya había expirado.",
    }[result.disposition]


def _consent_only_result(message: str) -> ExternalSupportResult:
    return ExternalSupportResult(
        answer=message,
        route="clarification",
        partial=False,
        needs_clarification=False,
        ticket=None,
        inventory=None,
        knowledge=None,
        knowledge_status=None,
    )


def _validate_query_memory_target(
    session: Session,
    access,
    payload: SupportQueryRequest,
) -> None:
    if payload.memory_scope is MemoryScope.global_:
        if access.role.value != "admin" or not access.global_access:
            raise MemoryForbidden("global memory scope is not authorized")
        return
    if payload.client_id is None:
        return
    if not access.can_access_client(payload.client_id):
        raise MemoryNotFound("client not found")
    active = session.execute(
        select(clients.c.client_id).where(
            clients.c.client_id == payload.client_id,
            clients.c.is_active.is_(True),
        )
    ).first()
    if active is None:
        raise MemoryNotFound("client not found")


def _memory_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFound):
        return HTTPException(status_code=404, detail="memory resource not found")
    if isinstance(exc, MemoryConflict):
        return HTTPException(status_code=409, detail="memory decision conflicts with current state")
    if isinstance(exc, MemoryForbidden):
        return HTTPException(status_code=403, detail="memory action is not authorized")
    if isinstance(exc, MemoryPolicyViolation):
        return HTTPException(status_code=422, detail="memory content violates policy")
    return HTTPException(status_code=503, detail="support memory unavailable")


def _consent_failure_diagnostic(exc: Exception) -> str:
    if isinstance(exc, ConsentClassificationInvalid):
        return exc.diagnostic
    if isinstance(exc, RAGProviderError):
        if exc.category in {"timeout", "deadline_exhausted"}:
            return "timeout"
        if exc.category == "http_error":
            return "http_or_schema"
        if exc.category == "validation_error":
            return "invalid_response"
        return "provider_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, SupportUnavailable):
        return "provider_unavailable"
    return "unexpected_error"


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
    memory_store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> SupportQueryResponse:
    deadline = QueryDeadline(SUPPORT_QUERY_BUDGET_SECONDS)
    try:
        access = build_access_context(session, identity.user)
        conversation_id = payload.conversation_id or uuid4()
        _validate_query_memory_target(session, access, payload)
        memory_store.bind_conversation(
            session,
            conversation_id,
            access,
            create=payload.conversation_id is None,
        )
        # bind_conversation commits its content-free ownership row. SET LOCAL
        # must be installed only after that commit so it remains effective for
        # the support graph's database work.
        apply_statement_timeout(session, deadline)
    except (MemoryNotFound, MemoryConflict, MemoryForbidden, MemoryPolicyViolation) as exc:
        raise _memory_http_error(exc) from None
    except MemoryPersistenceError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support memory unavailable",
        ) from None
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
    consent_result: ConsentResult | None = None
    support_question = payload.question
    try:
        pending_before_query = memory_store.get_pending(session, conversation_id, access)
        if pending_before_query is not None:
            classification: ConsentClassification | None = None
            classify = getattr(agent, "classify_pending_consent", None)
            if callable(classify):
                try:
                    classification = classify(
                        payload.question,
                        pending_before_query,
                        deadline=deadline,
                        rag_factory=lambda: rag_service_context(deadline=deadline),
                    )
                except Exception as exc:
                    _CONSENT_LOGGER.info(
                        "Support consent classification fallback | diagnostic=%s",
                        _consent_failure_diagnostic(exc),
                    )
                    classification = None
            if classification is None:
                classification = ConservativeTextConsentClassifier().classify(
                    payload.question, pending_before_query
                )
            idempotency_key = "query:" + hashlib.sha256(
                (
                    f"{access.user_id}:{pending_before_query.id}:"
                    + " ".join(payload.question.split())
                ).encode()
            ).hexdigest()[:58]
            consent_result = ConsentCoordinator(
                memory_store,
                _FixedConsentClassifier(classification),
            ).handle(
                session,
                ConsentRequest(
                    conversation_id=conversation_id,
                    proposal_id=pending_before_query.id,
                    idempotency_key=idempotency_key,
                    user_text=payload.question,
                ),
                access,
            )
            message = _consent_message(consent_result)
            if not consent_result.continue_support_question:
                response.headers["X-TrackFlow-Run-ID"] = run_id
                return _to_response(
                    _consent_only_result(message),
                    conversation_id,
                    consent_result.resolution.replacement_proposal,
                    consent_result,
                )
            support_question = consent_result.support_question or payload.question
            # Consent persistence commits. Restore the transaction-local timeout
            # before processing the separate support question.
            apply_statement_timeout(session, deadline)
    except (MemoryNotFound, MemoryConflict, MemoryForbidden, MemoryPolicyViolation) as exc:
        raise _memory_http_error(exc) from None
    except (MemoryPersistenceError, SQLAlchemyError, TimeoutError):
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support memory unavailable",
        ) from None

    try:
        execution = await agent.execute(
            support_question,
            session=session,
            access=access,
            deadline=deadline,
            rag_factory=lambda: rag_service_context(deadline=deadline),
            request_id=getattr(request.state, "request_id", None),
            run_id=run_id,
            ticket_tool=mcp_tools.lookup_ticket,
            inventory_tool=mcp_tools.lookup_inventory_product,
            conversation_id=conversation_id,
            memory_client_id=payload.client_id,
            memory_scope=payload.memory_scope,
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
    try:
        pending = execution.result.memory_proposal or memory_store.get_pending(
            session, conversation_id, access
        )
    except (MemoryNotFound, MemoryForbidden):
        pending = None
    except (MemoryPersistenceError, SQLAlchemyError):
        pending = execution.result.memory_proposal
    result = execution.result
    if consent_result is not None:
        result = replace(
            result,
            answer=f"{_consent_message(consent_result)}\n\n{result.answer}",
        )
    return _to_response(result, conversation_id, pending, consent_result)


@router.post(
    "/memory/decision",
    response_model=SupportMemoryDecisionResponse,
    responses={
        401: {"description": "Missing or invalid Bearer token."},
        403: {"description": "The authenticated role is not authorized."},
        404: {"description": "Conversation or proposal not found."},
        409: {"description": "Decision conflicts with current state."},
        422: {"description": "Edited memory violates policy."},
        503: {"description": "Memory persistence unavailable."},
    },
)
def decide_support_memory(
    payload: SupportMemoryDecisionRequest,
    identity: Annotated[SupportIdentity, Depends(require_support_reader)],
    session: Annotated[Session, Depends(get_db)],
    memory_store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> SupportMemoryDecisionResponse:
    try:
        access = build_access_context(session, identity.user)
        memory_store.bind_conversation(
            session,
            payload.conversation_id,
            access,
            create=False,
        )
        original = memory_store.get_proposal(session, payload.proposal_id, access)
        if original.conversation_id != payload.conversation_id:
            raise MemoryNotFound("proposal not found")
        edited_proposal = None
        if payload.action is ConsentAction.edit:
            assert payload.edited_content is not None
            canonical = canonical_key_for(original.category, payload.edited_content)
            serialized = json.dumps(
                payload.edited_content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            edited_proposal = MemoryProposalCreate(
                conversation_id=original.conversation_id,
                origin_interaction_id=(
                    "edit-" + hashlib.sha256(payload.idempotency_key.encode()).hexdigest()[:24]
                ),
                origin_message_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                scope=original.scope,
                client_id=original.client_id,
                category=original.category,
                content=payload.edited_content,
                canonical_key=canonical,
                valid_until=original.valid_until,
            )
        consent = ConsentCoordinator(memory_store).handle(
            session,
            ConsentRequest(
                conversation_id=payload.conversation_id,
                proposal_id=payload.proposal_id,
                idempotency_key=payload.idempotency_key,
                action=payload.action,
                user_text=payload.user_text,
                edited_proposal=edited_proposal,
            ),
            access,
        )
        return SupportMemoryDecisionResponse(
            conversation_id=payload.conversation_id,
            disposition=consent.disposition,
            proposal=_safe_proposal(consent.resolution.replacement_proposal),
            memory_active=consent.resolution.entry is not None,
            continue_support_question=consent.continue_support_question,
        )
    except (MemoryNotFound, MemoryConflict, MemoryForbidden, MemoryPolicyViolation) as exc:
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise _memory_http_error(exc) from None
    except (MemoryPersistenceError, SQLAlchemyError, TimeoutError):
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="support memory unavailable",
        ) from None
