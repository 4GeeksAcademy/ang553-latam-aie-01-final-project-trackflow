"""Deterministic LangGraph routing across RAG and operational read tools."""

from __future__ import annotations

import re
import inspect
import hashlib
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from typing import Callable, Literal, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlmodel import Session

from agents.customer_support.memory_evaluation import (
    AUTOEVALUATION_MAX_SECONDS,
    AUTOEVALUATION_MIN_SECONDS,
    AUTOEVALUATION_RESPONSE_RESERVE_SECONDS,
    FourGeeksStructuredMemoryEvaluator,
    MemoryCandidate,
    MemoryEvaluationInvalid,
    MemoryEvaluationMetadata,
    MemoryEvaluator,
    prefilter_memory_candidate,
)
from agents.customer_support.memory_context import (
    RetrievedMemoryContext,
    append_memory_context,
    retrieve_relevant_memory,
)
from agents.customer_support.guardrail_contracts import (
    GuardrailEvent,
    GuardrailEventAction,
    OutputValidationStatus,
)
from agents.customer_support.guardrail_observability import record_guardrail_event
from agents.customer_support.output_guardrails import (
    SAFE_OUTPUT_MESSAGE,
    validate_external_result,
)
from agents.customer_support.graph import (
    SupportState,
    add_rag_nodes,
)
from agents.customer_support.tools import (
    InventoryLookupInput,
    InventoryLookupResult,
    TicketLookupInput,
    TicketLookupResult,
    TrackingLookupInput,
    TrackingLookupResult,
    ToolStatus,
)
from agents.customer_support.tracing import TraceRecorder
from services.api.access_control import AccessContext
from services.api.agent_memory.contracts import (
    ConsentClassification,
    MemoryProposalCreate,
    MemoryProposalRecord,
    MemoryScope,
)
from services.api.agent_memory.consent import (
    CONSENT_CLASSIFIER_MAX_SECONDS,
    ConsentClassifier,
    FourGeeksStructuredConsentClassifier,
)
from services.api.agent_memory.service import (
    MemoryConflict,
    MemoryForbidden,
    MemoryPolicyViolation,
    MemoryStore,
    canonical_key_for,
)
from services.api.auth_models import Role
from services.incidents.models import clients
from services.rag.contracts import QueryResult, RAGConfigurationError, RAGProviderError
from services.rag.resilience import QueryDeadline
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService


SupportRoute = Literal[
    "clarification", "knowledge", "ticket", "inventory", "tracking", "mixed"
]
ComponentStatus = Literal["ok", "not_found", "timeout", "unavailable"]

class ExternalSupportState(SupportState, total=False):
    route: SupportRoute
    needs_ticket: bool
    needs_inventory: bool
    needs_tracking: bool
    needs_knowledge: bool
    incident_id: str
    product_id: int
    tracking_number: str
    clarification: str
    ticket: TicketLookupResult
    inventory: InventoryLookupResult
    tracking: TrackingLookupResult
    knowledge_status: ComponentStatus
    knowledge_error_category: str
    partial: bool
    needs_clarification: bool
    assembled: "ExternalSupportResult"
    memory_candidate: MemoryCandidate
    memory_scope: MemoryScope
    memory_client_id: str
    memory_read_enabled: bool
    memory_context: tuple[RetrievedMemoryContext, ...]
    memory_read_status: str


@dataclass(frozen=True)
class RouteDecision:
    route: SupportRoute
    needs_ticket: bool = False
    needs_inventory: bool = False
    needs_tracking: bool = False
    needs_knowledge: bool = False
    incident_id: str | None = None
    product_id: int | None = None
    tracking_number: str | None = None
    clarification: str | None = None


@dataclass(frozen=True)
class ExternalSupportResult:
    answer: str
    route: SupportRoute
    partial: bool
    needs_clarification: bool
    ticket: TicketLookupResult | None
    inventory: InventoryLookupResult | None
    knowledge: QueryResult | None
    knowledge_status: ComponentStatus | None
    tracking: TrackingLookupResult | None = None
    memory_proposal: MemoryProposalRecord | None = None
    memory_evaluation: MemoryEvaluationMetadata = field(
        default_factory=MemoryEvaluationMetadata
    )
    memory_context: tuple[RetrievedMemoryContext, ...] = ()
    memory_read_status: str = "skipped"


@dataclass(frozen=True)
class ExternalSupportExecution:
    run_id: str
    result: ExternalSupportResult
    memory_proposal: MemoryProposalRecord | None = None
    memory_evaluation: MemoryEvaluationMetadata = field(
        default_factory=MemoryEvaluationMetadata
    )
    memory_context: tuple[RetrievedMemoryContext, ...] = ()
    memory_read_status: str = "skipped"


def _safe_output_result() -> ExternalSupportResult:
    return ExternalSupportResult(
        answer=SAFE_OUTPUT_MESSAGE,
        route="clarification",
        partial=False,
        needs_clarification=False,
        ticket=None,
        inventory=None,
        knowledge=None,
        knowledge_status=None,
    )


class SupportUnavailable(RuntimeError):
    category = "unavailable"


_TRF_TOKEN = re.compile(r"\bTRF-[A-Za-z0-9-]+\b", re.IGNORECASE)
_VALID_TRF = re.compile(r"\bTRF-(\d{6})\b", re.IGNORECASE)
_TICKET_NUMBER = re.compile(
    r"\b(?:ticket|incidencia|caso)\s+(?:id\s+)?#?(\d+)\b", re.IGNORECASE
)
_TICKET_PAIR = re.compile(
    r"\b(?:tickets?|incidencias?|casos?)\s+(?:ids?\s+)?#?(\d+)\s*"
    r"(?:,|and|y)\s*(?:(?:ticket|incidencia|caso)\s*)?#?(\d+)\b",
    re.IGNORECASE,
)
_PRODUCT_NUMBER = re.compile(
    r"\b(?:product|producto)(?:\s+id|_id)?\s+#?(-?\d+)\b", re.IGNORECASE
)
_PRODUCT_PAIR = re.compile(
    r"\b(?:products?|productos?)\s+(?:ids?\s+)?#?(-?\d+)\s*"
    r"(?:,|and|y)\s*(?:(?:product|producto)\s*)?#?(-?\d+)\b",
    re.IGNORECASE,
)
_TICKET_WORD = re.compile(
    r"\b(ticket|incident|support case|incidencia|caso de soporte)\b", re.IGNORECASE
)
_TICKET_OPERATION = re.compile(
    r"\b(status|current|lookup|look up|check|open|closed|discarded|estado|actual|"
    r"consultar|consulta|revisar|revisa|abierto|cerrado|descartado)\b",
    re.IGNORECASE,
)
_INVENTORY_OPERATION = re.compile(
    r"\b(stock|inventory|available|availability|units?|inventario|existencias?|"
    r"disponible|disponibilidad|unidades?)\b",
    re.IGNORECASE,
)
_KNOWLEDGE_WORD = re.compile(
    r"\b(policy|policies|procedure|procedures|process|rules?|sla|requirements?|"
    r"guidelines?|pol[ií]tica|pol[ií]ticas|procedimiento|procedimientos|proceso|"
    r"procesos|reglas?|requisitos?|directrices)\b",
    re.IGNORECASE,
)
_TRACKING_WORD = re.compile(
    r"\b(tracking|tracking number|seguimiento)\b",
    re.IGNORECASE,
)
_TRACKING_ENTITY = re.compile(
    r"\b(shipment|order|package|pedido|env[ií]o|paquete)\b",
    re.IGNORECASE,
)
_TRACKING_OPERATION = re.compile(
    r"\b(where|status|track|check|find|lookup|compare|d[oó]nde|estado|rastrear|"
    r"consulta|revisa|compara)\b",
    re.IGNORECASE,
)
_TRACKING_REFERENCE = re.compile(
    r"\b(?:tracking(?:\s+number)?|shipment|order|package|seguimiento|pedido|env[ií]o|paquete)"
    r"(?:\s+(?:number|n[uú]mero|id))?\s*#?\s*"
    r"((?=[A-Za-z0-9-]{4,128}\b)(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]*)\b",
    re.IGNORECASE,
)


def route_support_query(question: str) -> RouteDecision:
    """Classify supported English and Spanish requests without a model call."""
    knowledge_intent = bool(_KNOWLEDGE_WORD.search(question))
    valid_trf = {
        match.group(0).upper()
        for match in _VALID_TRF.finditer(question)
        if int(match.group(1)) > 0
    }
    raw_trf = {match.group(0).upper() for match in _TRF_TOKEN.finditer(question)}
    numeric_tickets = {int(value) for value in _TICKET_NUMBER.findall(question)}
    numeric_tickets.update(
        int(value)
        for pair in _TICKET_PAIR.findall(question)
        for value in pair
    )
    normalized_numeric = {
        f"TRF-{value:06d}" for value in numeric_tickets if 1 <= value <= 999999
    }
    invalid_ticket = bool(raw_trf - valid_trf) or any(
        value <= 0 or value > 999999 for value in numeric_tickets
    )
    ticket_ids = valid_trf | normalized_numeric
    ticket_intent = bool(ticket_ids or raw_trf) or bool(
        not knowledge_intent
        and _TICKET_WORD.search(question)
        and _TICKET_OPERATION.search(question)
    )

    product_values = [int(value) for value in _PRODUCT_NUMBER.findall(question)]
    product_values.extend(
        int(value)
        for pair in _PRODUCT_PAIR.findall(question)
        for value in pair
    )
    product_ids = {value for value in product_values if value > 0}
    invalid_product = any(value <= 0 for value in product_values)
    inventory_intent = bool(product_values) or bool(
        not knowledge_intent and _INVENTORY_OPERATION.search(question)
    )
    referenced_tracking_numbers = {
        value.upper() for value in _TRACKING_REFERENCE.findall(question)
    }
    tracking_request_signal = bool(_TRACKING_OPERATION.search(question)) or bool(
        re.match(r"(?i)^\s*(?:tracking|seguimiento|pedido|order)\b", question)
    )
    tracking_numbers = (
        referenced_tracking_numbers if tracking_request_signal else set()
    )
    tracking_intent = (
        bool(tracking_numbers)
        or bool(
            tracking_request_signal
            and (
                _TRACKING_WORD.search(question)
                or _TRACKING_ENTITY.search(question)
            )
        )
    )

    if invalid_ticket or len(ticket_ids) > 1:
        return RouteDecision(
            "clarification",
            clarification="Provide exactly one valid ticket identifier, for example TRF-000482.",
        )
    if ticket_intent and not ticket_ids:
        return RouteDecision(
            "clarification",
            clarification="Which ticket should I check? Provide an identifier such as TRF-000482.",
        )
    if invalid_product or len(product_ids) > 1:
        return RouteDecision(
            "clarification",
            clarification="Provide exactly one positive numeric product ID.",
        )
    if inventory_intent and not product_ids:
        return RouteDecision(
            "clarification",
            clarification="Which product should I check? Provide its positive numeric product ID.",
        )
    if len(tracking_numbers) > 1:
        return RouteDecision(
            "clarification",
            clarification="Provide exactly one tracking identifier.",
        )
    if tracking_intent and not tracking_numbers and not (ticket_intent or inventory_intent):
        return RouteDecision(
            "clarification",
            clarification="Which shipment should I check? Provide its tracking identifier.",
        )

    needs_ticket = bool(ticket_ids)
    needs_inventory = bool(product_ids)
    needs_tracking = bool(tracking_numbers)
    needs_knowledge = knowledge_intent or not (
        needs_ticket or needs_inventory or needs_tracking
    )
    selected = sum((needs_ticket, needs_inventory, needs_tracking, needs_knowledge))
    if selected > 1:
        route: SupportRoute = "mixed"
    elif needs_ticket:
        route = "ticket"
    elif needs_inventory:
        route = "inventory"
    elif needs_tracking:
        route = "tracking"
    else:
        route = "knowledge"
    return RouteDecision(
        route,
        needs_ticket=needs_ticket,
        needs_inventory=needs_inventory,
        needs_tracking=needs_tracking,
        needs_knowledge=needs_knowledge,
        incident_id=next(iter(ticket_ids), None),
        product_id=next(iter(product_ids), None),
        tracking_number=next(iter(tracking_numbers), None),
    )


class ExternalSupportRuntime:
    def __init__(
        self,
        *,
        session: Session,
        access: AccessContext,
        deadline: QueryDeadline,
        rag_factory: Callable[[], AbstractContextManager[RAGService]],
        request_id: str | None,
        run_id: str,
        tracer: TraceRecorder,
        ticket_tool=None,
        inventory_tool=None,
        tracking_tool=None,
        memory_evaluator: MemoryEvaluator | None = None,
        consent_classifier: ConsentClassifier | None = None,
        memory_store: MemoryStore | None = None,
        conversation_id: UUID | None = None,
        memory_client_id: str | None = None,
        memory_scope: MemoryScope | None = None,
    ) -> None:
        self.session = session
        self.access = access
        self.deadline = deadline
        self.rag_factory = rag_factory
        self.request_id = request_id
        self.run_id = run_id
        self.tracer = tracer
        self.ticket_tool = ticket_tool
        self.inventory_tool = inventory_tool
        self.tracking_tool = tracking_tool
        self.memory_evaluator = memory_evaluator
        self.consent_classifier = consent_classifier
        self.memory_store = memory_store or MemoryStore()
        self.conversation_id = conversation_id
        self.memory_client_id = memory_client_id
        self.memory_scope = memory_scope
        self._rag_context: AbstractContextManager[RAGService] | None = None
        self._service: RAGService | None = None

    @property
    def service(self) -> RAGService:
        if self._service is None:
            context = self.rag_factory()
            service = context.__enter__()
            self._rag_context = context
            self._service = service
        return self._service

    def close(self) -> None:
        if self._rag_context is not None:
            self._rag_context.__exit__(None, None, None)
            self._rag_context = None
            self._service = None

    def compatible_memory_evaluator(self) -> MemoryEvaluator | None:
        if self.memory_evaluator is not None:
            return self.memory_evaluator
        if self._service is None:
            return None
        generation = getattr(self._service, "generation_provider", None)
        provider = getattr(generation, "provider", None)
        if provider is None or not callable(getattr(provider, "generate_structured", None)):
            return None
        return FourGeeksStructuredMemoryEvaluator(provider)


def _state_summary(state: ExternalSupportState) -> dict[str, object]:
    ticket = state.get("ticket")
    inventory = state.get("inventory")
    tracking = state.get("tracking")
    knowledge = state.get("result")
    return {
        "route": state.get("route"),
        "needs_ticket": state.get("needs_ticket", False),
        "needs_inventory": state.get("needs_inventory", False),
        "needs_tracking": state.get("needs_tracking", False),
        "needs_knowledge": state.get("needs_knowledge", False),
        "ticket_status": None if ticket is None else ticket.status.value,
        "ticket_call_count": int(ticket is not None),
        "inventory_status": None if inventory is None else inventory.status.value,
        "inventory_call_count": int(inventory is not None),
        "tracking_status": None if tracking is None else tracking.status.value,
        "tracking_call_count": int(tracking is not None),
        "knowledge_status": state.get("knowledge_status"),
        "knowledge_error_category": state.get("knowledge_error_category"),
        "knowledge_source_count": 0 if knowledge is None else len(knowledge.sources),
        "partial": state.get("partial", False),
        "needs_clarification": state.get("needs_clarification", False),
        "memory_candidate": (
            None
            if state.get("memory_candidate") is None
            else state["memory_candidate"].category.value
        ),
        "memory_evaluation_status": (
            None
            if state.get("assembled") is None
            else state["assembled"].memory_evaluation.status
        ),
        "memory_evaluation_diagnostic": (
            None
            if state.get("assembled") is None
            else state["assembled"].memory_evaluation.diagnostic
        ),
        "memory_context_count": len(state.get("memory_context", ())),
        "memory_read_status": state.get("memory_read_status"),
    }


def _recorded_node(name: str, operation):
    def node(
        state: ExternalSupportState,
        runtime: Runtime[ExternalSupportRuntime],
    ) -> ExternalSupportState:
        started = runtime.context.tracer.now()
        try:
            updates, transition = operation(state, runtime.context)
        except Exception as exc:
            resilience = (
                runtime.context.service.resilience_trace()
                if name in {"retrieve_context", "generate_answer"}
                and runtime.context._service is not None
                else {}
            )
            runtime.context.tracer.record_node(
                runtime.context.run_id,
                node=name,
                status="failed",
                started=started,
                transition=None,
                state=_state_summary(state),
                error=exc,
                resilience=resilience,
            )
            raise
        combined = cast(ExternalSupportState, {**state, **updates})
        resilience = (
            runtime.context.service.resilience_trace()
            if name in {"retrieve_context", "generate_answer"}
            and runtime.context._service is not None
            else {}
        )
        runtime.context.tracer.record_node(
            runtime.context.run_id,
            node=name,
            status="completed",
            started=started,
            transition=transition,
            state=_state_summary(combined),
            resilience=resilience,
        )
        return updates

    node.__name__ = name
    return node


def _recorded_async_node(name: str, operation):
    async def node(
        state: ExternalSupportState,
        runtime: Runtime[ExternalSupportRuntime],
    ) -> ExternalSupportState:
        started = runtime.context.tracer.now()
        try:
            updates, transition = await operation(state, runtime.context)
        except Exception as exc:
            runtime.context.tracer.record_node(
                runtime.context.run_id,
                node=name,
                status="failed",
                started=started,
                transition=None,
                state=_state_summary(state),
                error=exc,
            )
            raise
        combined = cast(ExternalSupportState, {**state, **updates})
        runtime.context.tracer.record_node(
            runtime.context.run_id,
            node=name,
            status="completed",
            started=started,
            transition=transition,
            state=_state_summary(combined),
        )
        return updates

    node.__name__ = name
    return node


def _rag_node(name: str, operation):
    def adapted(state: ExternalSupportState, runtime: ExternalSupportRuntime):
        try:
            updates, transition = operation(state, runtime)
        except (RAGConfigurationError, RAGProviderError) as exc:
            status: ComponentStatus = (
                "timeout"
                if getattr(exc, "category", None)
                in {"timeout", "deadline_exhausted", "retry_exhausted"}
                else "unavailable"
            )
            category = getattr(exc, "category", None)
            controlled_category = (
                category
                if category
                in {
                    "timeout",
                    "deadline_exhausted",
                    "retry_exhausted",
                    "network_error",
                    "http_error",
                    "validation_error",
                    "provider_error",
                }
                else "configuration_error"
                if isinstance(exc, RAGConfigurationError)
                else "provider_error"
            )
            return {
                "knowledge_status": status,
                "knowledge_error_category": controlled_category,
            }, "assemble_response"
        if name == "abstain":
            updates["knowledge_status"] = "not_found"
            transition = "assemble_response"
        elif name == "finalize_answer":
            updates["knowledge_status"] = "ok"
            transition = "assemble_response"
        return updates, transition

    return _recorded_node(name, adapted)


def _route_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    decision = route_support_query(state["question"])
    updates: ExternalSupportState = {
        "route": decision.route,
        "needs_ticket": decision.needs_ticket,
        "needs_inventory": decision.needs_inventory,
        "needs_tracking": decision.needs_tracking,
        "needs_knowledge": decision.needs_knowledge,
        "memory_read_enabled": runtime.conversation_id is not None,
    }
    if decision.incident_id is not None:
        updates["incident_id"] = decision.incident_id
    if decision.product_id is not None:
        updates["product_id"] = decision.product_id
    if decision.tracking_number is not None:
        updates["tracking_number"] = decision.tracking_number
    if decision.clarification is not None:
        updates["clarification"] = decision.clarification
    transition = _after_route(cast(ExternalSupportState, {**state, **updates}))
    return updates, transition


def _after_route_sources(state: ExternalSupportState) -> str:
    if state["route"] == "clarification":
        return "assemble_response"
    if state.get("needs_ticket"):
        return "ticket_lookup"
    if state.get("needs_inventory"):
        return "inventory_lookup"
    if state.get("needs_tracking"):
        return "tracking_lookup"
    return "retrieve_context"


def _after_route(state: ExternalSupportState) -> str:
    if state["route"] != "clarification" and state.get("memory_read_enabled"):
        return "retrieve_memory"
    return _after_route_sources(state)


def _retrieve_memory_node(
    state: ExternalSupportState,
    runtime: ExternalSupportRuntime,
):
    try:
        contexts = retrieve_relevant_memory(
            runtime.session,
            runtime.access,
            state["question"],
            store=runtime.memory_store,
        )
        status = "used" if contexts else "empty"
    except Exception:
        contexts = ()
        status = "unavailable"
    return {
        "memory_context": contexts,
        "memory_read_status": status,
    }, _after_route_sources(state)


def _after_memory(state: ExternalSupportState) -> str:
    return _after_route_sources(state)


async def _ticket_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    if runtime.ticket_tool is None:
        result = TicketLookupResult(status=ToolStatus.unavailable)
    else:
        result = runtime.ticket_tool(
            TicketLookupInput(state["incident_id"]),
            runtime.deadline,
        )
        if inspect.isawaitable(result):
            result = await result
    transition = (
        "inventory_lookup"
        if state.get("needs_inventory")
        else "tracking_lookup"
        if state.get("needs_tracking")
        else "retrieve_context"
        if state.get("needs_knowledge")
        else "assemble_response"
    )
    return {"ticket": result}, transition


async def _inventory_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    if runtime.inventory_tool is None:
        result = InventoryLookupResult(status=ToolStatus.unavailable)
    else:
        result = runtime.inventory_tool(
            InventoryLookupInput(state["product_id"]),
            runtime.deadline,
        )
        if inspect.isawaitable(result):
            result = await result
    transition = (
        "tracking_lookup"
        if state.get("needs_tracking")
        else "retrieve_context"
        if state.get("needs_knowledge")
        else "assemble_response"
    )
    return {"inventory": result}, transition


async def _tracking_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    if runtime.tracking_tool is None:
        result = TrackingLookupResult(status=ToolStatus.unavailable)
    else:
        result = runtime.tracking_tool(
            TrackingLookupInput(state["tracking_number"]),
            runtime.deadline,
        )
        if inspect.isawaitable(result):
            result = await result
    transition = (
        "retrieve_context" if state.get("needs_knowledge") else "assemble_response"
    )
    return {"tracking": result}, transition


def _after_ticket(state: ExternalSupportState) -> str:
    if state.get("needs_inventory"):
        return "inventory_lookup"
    if state.get("needs_tracking"):
        return "tracking_lookup"
    return "retrieve_context" if state.get("needs_knowledge") else "assemble_response"


def _after_inventory(state: ExternalSupportState) -> str:
    if state.get("needs_tracking"):
        return "tracking_lookup"
    return "retrieve_context" if state.get("needs_knowledge") else "assemble_response"


def _after_tracking(state: ExternalSupportState) -> str:
    return "retrieve_context" if state.get("needs_knowledge") else "assemble_response"


def _after_rag_retrieval(state: ExternalSupportState) -> str:
    if state.get("knowledge_status") in {"timeout", "unavailable"}:
        return "terminal"
    return "generate_answer" if state.get("context") else "abstain"


def _after_rag_generation(state: ExternalSupportState) -> str:
    if state.get("knowledge_status") in {"timeout", "unavailable"}:
        return "terminal"
    return (
        "abstain"
        if state.get("answer") == INSUFFICIENT_INFORMATION_ANSWER
        else "finalize_answer"
    )


def _component_sentence(label: str, status: ToolStatus) -> str:
    if status is ToolStatus.not_found:
        return f"{label} was not found or is not visible to this account."
    if status is ToolStatus.timeout:
        return f"{label} lookup timed out."
    return f"{label} service is unavailable."


def _memory_plan(
    question: str,
    runtime: ExternalSupportRuntime,
) -> tuple[
    MemoryCandidate | None,
    MemoryScope | None,
    str | None,
    MemoryEvaluationMetadata,
]:
    candidate = prefilter_memory_candidate(question)
    if candidate is None:
        return None, None, None, MemoryEvaluationMetadata()
    unresolved = MemoryEvaluationMetadata(
        status="scope_unresolved",
        candidate_category=candidate.category,
        evaluator_called=False,
    )
    if runtime.conversation_id is None or runtime.access.role is Role.user:
        return None, None, None, unresolved

    requested_scope = runtime.memory_scope or MemoryScope.client
    if requested_scope is MemoryScope.global_:
        if (
            runtime.access.role is not Role.admin
            or not runtime.access.global_access
            or candidate.category.value != "carrier_coverage"
        ):
            return None, None, None, unresolved
        return (
            candidate,
            MemoryScope.global_,
            None,
            MemoryEvaluationMetadata(
                status="not_proposed",
                candidate_category=candidate.category,
                evaluator_called=False,
            ),
        )

    client_id = runtime.memory_client_id
    if client_id is None:
        if len(runtime.access.allowed_client_ids) != 1:
            return None, None, None, unresolved
        client_id = next(iter(runtime.access.allowed_client_ids))
    if not runtime.access.can_access_client(client_id):
        return None, None, None, unresolved
    try:
        active = runtime.session.execute(
            select(clients.c.client_id).where(
                clients.c.client_id == client_id,
                clients.c.is_active.is_(True),
            )
        ).first()
    except Exception:
        return None, None, None, unresolved
    if active is None:
        return None, None, None, unresolved
    return (
        candidate,
        MemoryScope.client,
        client_id,
        MemoryEvaluationMetadata(
            status="not_proposed",
            candidate_category=candidate.category,
            evaluator_called=False,
        ),
    )


def _assemble_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    route = state["route"]
    if route == "clarification":
        result = ExternalSupportResult(
            answer=state["clarification"],
            route=route,
            partial=False,
            needs_clarification=True,
            ticket=None,
            inventory=None,
            knowledge=None,
            knowledge_status=None,
        )
        return {
            "partial": False,
            "needs_clarification": True,
            "assembled": result,
        }, "__end__"

    ticket = state.get("ticket")
    inventory = state.get("inventory")
    tracking = state.get("tracking")
    knowledge = state.get("result")
    knowledge_status = state.get("knowledge_status")
    technical: list[str] = []
    usable = 0
    parts: list[str] = []
    if ticket is not None:
        if ticket.status is ToolStatus.ok:
            usable += 1
            parts.append(
                f"Ticket {ticket.incident_id} is {ticket.incident_status} "
                f"(category {ticket.category}, version {ticket.version}, updated {ticket.updated_at.isoformat()})."
            )
        else:
            parts.append(_component_sentence("Ticket", ticket.status))
            if ticket.status in {ToolStatus.timeout, ToolStatus.unavailable}:
                technical.append("ticket")
            else:
                usable += 1
    if inventory is not None:
        if inventory.status is ToolStatus.ok:
            usable += 1
            parts.append(
                f"Product {inventory.product_id} ({inventory.name}, SKU {inventory.sku}) "
                f"has {inventory.current_stock} units in warehouse {inventory.warehouse}."
            )
        else:
            parts.append(_component_sentence("Product", inventory.status))
            if inventory.status in {ToolStatus.timeout, ToolStatus.unavailable}:
                technical.append("inventory")
            else:
                usable += 1
    if tracking is not None:
        if tracking.status is ToolStatus.ok:
            usable += 1
            parts.append(
                f"Tracking {tracking.tracking_number} has a recorded dispatch "
                f"for commercial order {tracking.commercial_order_id}."
            )
        else:
            parts.append(_component_sentence("Tracking", tracking.status))
            if tracking.status in {ToolStatus.timeout, ToolStatus.unavailable}:
                technical.append("tracking")
            else:
                usable += 1
    if state.get("needs_knowledge"):
        if knowledge_status in {"timeout", "unavailable"}:
            technical.append("knowledge")
            parts.append("Knowledge service is unavailable.")
        elif knowledge is not None:
            usable += 1
            parts.append(knowledge.answer)
    if technical and usable == 0:
        raise SupportUnavailable("all selected support sources failed")
    partial = bool(technical)
    result = ExternalSupportResult(
        answer=append_memory_context(
            "\n\n".join(parts),
            state.get("memory_context", ()),
        ),
        route=route,
        partial=partial,
        needs_clarification=False,
        ticket=ticket,
        inventory=inventory,
        knowledge=knowledge,
        knowledge_status=knowledge_status,
        tracking=tracking,
        memory_context=state.get("memory_context", ()),
        memory_read_status=state.get("memory_read_status", "skipped"),
    )
    output_validation = validate_external_result(result)
    if output_validation.status is OutputValidationStatus.blocked:
        record_guardrail_event(
            GuardrailEvent(
                guard_id="support.output.graph.v1",
                failure_category=output_validation.failure_category,
                action=GuardrailEventAction.blocked,
                reason_code=output_validation.reason_code,
                correlation_id=runtime.run_id,
            )
        )
        safe_result = _safe_output_result()
        return {
            "partial": False,
            "needs_clarification": False,
            "assembled": safe_result,
        }, "__end__"
    candidate, scope, client_id, metadata = _memory_plan(state["question"], runtime)
    result = replace(result, memory_evaluation=metadata)
    updates: ExternalSupportState = {
        "partial": partial,
        "needs_clarification": False,
        "assembled": result,
    }
    if candidate is not None and scope is not None:
        updates["memory_candidate"] = candidate
        updates["memory_scope"] = scope
        if client_id is not None:
            updates["memory_client_id"] = client_id
    return updates, "evaluate_memory" if candidate is not None else "__end__"


def _after_assemble(state: ExternalSupportState) -> str:
    return "evaluate_memory" if state.get("memory_candidate") is not None else "__end__"


def _memory_policy_diagnostic(exc: MemoryPolicyViolation) -> str:
    message = str(exc)
    if message == "country must be US or ES":
        return "invalid_country"
    if message == "memory content violates TrackFlow retention policy":
        return "prohibited_content"
    return "contract_validation"


def _evaluate_memory_node(
    state: ExternalSupportState,
    runtime: ExternalSupportRuntime,
):
    primary = state["assembled"]
    candidate = state["memory_candidate"]
    evaluator = runtime.compatible_memory_evaluator()
    base = {
        "candidate_category": candidate.category,
        "evaluator_called": False,
    }
    if evaluator is None:
        metadata = MemoryEvaluationMetadata(status="provider_unavailable", **base)
        return {"assembled": replace(primary, memory_evaluation=metadata)}, "__end__"
    try:
        timeout = runtime.deadline.operation_timeout(
            AUTOEVALUATION_MAX_SECONDS,
            reserve=AUTOEVALUATION_RESPONSE_RESERVE_SECONDS,
        )
    except RAGProviderError:
        metadata = MemoryEvaluationMetadata(status="insufficient_budget", **base)
        return {"assembled": replace(primary, memory_evaluation=metadata)}, "__end__"
    if timeout < AUTOEVALUATION_MIN_SECONDS:
        metadata = MemoryEvaluationMetadata(status="insufficient_budget", **base)
        return {"assembled": replace(primary, memory_evaluation=metadata)}, "__end__"

    called = {
        "candidate_category": candidate.category,
        "evaluator_called": True,
    }
    diagnostic = None
    try:
        decision = evaluator.evaluate(
            question=state["question"],
            answer=primary.answer,
            candidate=candidate,
            timeout_seconds=timeout,
        )
        if not decision.should_propose:
            metadata = MemoryEvaluationMetadata(status="not_proposed", **called)
            return {"assembled": replace(primary, memory_evaluation=metadata)}, "__end__"
        if decision.category is not candidate.category or decision.content is None:
            raise MemoryEvaluationInvalid("contract_validation")
        scope = state["memory_scope"]
        content = decision.content.model_dump(mode="python")
        proposal_payload = MemoryProposalCreate(
            conversation_id=runtime.conversation_id,
            origin_interaction_id=runtime.run_id,
            origin_message_sha256=hashlib.sha256(
                state["question"].encode("utf-8")
            ).hexdigest(),
            scope=scope,
            client_id=(
                None if scope is MemoryScope.global_ else state.get("memory_client_id")
            ),
            category=decision.category,
            content=content,
            canonical_key=canonical_key_for(decision.category, content),
        )
        proposal = runtime.memory_store.create_proposal(
            runtime.session,
            proposal_payload,
            runtime.access,
        )
        metadata = MemoryEvaluationMetadata(status="proposed", **called)
        result = replace(
            primary,
            memory_proposal=proposal,
            memory_evaluation=metadata,
        )
        return {"assembled": result}, "__end__"
    except MemoryEvaluationInvalid as exc:
        status = "invalid"
        diagnostic = exc.diagnostic
    except (TimeoutError,):
        status = "timeout"
    except RAGProviderError as exc:
        status = "timeout" if exc.category in {"timeout", "deadline_exhausted"} else "error"
    except MemoryPolicyViolation as exc:
        status = "policy_rejected"
        diagnostic = _memory_policy_diagnostic(exc)
    except MemoryForbidden:
        status = "policy_rejected"
        diagnostic = "authorization"
    except MemoryConflict:
        status = "policy_rejected"
        diagnostic = "proposal_conflict"
    except ValueError:
        status = "policy_rejected"
        diagnostic = "contract_validation"
    except Exception:
        status = "error"
    metadata = MemoryEvaluationMetadata(
        status=status,
        diagnostic=diagnostic,
        **called,
    )
    return {"assembled": replace(primary, memory_evaluation=metadata)}, "__end__"


class ExternalSupportAgent:
    """Compiled graph with deterministic routing and ephemeral checkpoints."""

    def __init__(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        tracer: TraceRecorder | None = None,
        memory_evaluator: MemoryEvaluator | None = None,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.checkpointer = checkpointer or InMemorySaver()
        self.tracer = tracer or TraceRecorder()
        self.memory_evaluator = memory_evaluator
        self.memory_store = memory_store or MemoryStore()
        builder = StateGraph(ExternalSupportState, context_schema=ExternalSupportRuntime)
        builder.add_node("route_query", _recorded_node("route_query", _route_node))
        builder.add_node(
            "ticket_lookup", _recorded_async_node("ticket_lookup", _ticket_node)
        )
        builder.add_node(
            "inventory_lookup",
            _recorded_async_node("inventory_lookup", _inventory_node),
        )
        builder.add_node(
            "tracking_lookup",
            _recorded_async_node("tracking_lookup", _tracking_node),
        )
        builder.add_node(
            "retrieve_memory",
            _recorded_node("retrieve_memory", _retrieve_memory_node),
        )
        builder.add_node(
            "assemble_response", _recorded_node("assemble_response", _assemble_node)
        )
        builder.add_node(
            "evaluate_memory",
            _recorded_node("evaluate_memory", _evaluate_memory_node),
        )
        add_rag_nodes(
            builder,
            terminal="assemble_response",
            node_wrapper=_rag_node,
            after_retrieval=_after_rag_retrieval,
            after_generation=_after_rag_generation,
        )
        builder.add_edge(START, "route_query")
        builder.add_conditional_edges(
            "route_query",
            _after_route,
            {
                "assemble_response": "assemble_response",
                "retrieve_memory": "retrieve_memory",
                "ticket_lookup": "ticket_lookup",
                "inventory_lookup": "inventory_lookup",
                "tracking_lookup": "tracking_lookup",
                "retrieve_context": "retrieve_context",
            },
        )
        builder.add_conditional_edges(
            "retrieve_memory",
            _after_memory,
            {
                "ticket_lookup": "ticket_lookup",
                "inventory_lookup": "inventory_lookup",
                "tracking_lookup": "tracking_lookup",
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_conditional_edges(
            "ticket_lookup",
            _after_ticket,
            {
                "inventory_lookup": "inventory_lookup",
                "tracking_lookup": "tracking_lookup",
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_conditional_edges(
            "inventory_lookup",
            _after_inventory,
            {
                "tracking_lookup": "tracking_lookup",
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_conditional_edges(
            "tracking_lookup",
            _after_tracking,
            {
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_conditional_edges(
            "assemble_response",
            _after_assemble,
            {
                "evaluate_memory": "evaluate_memory",
                "__end__": END,
            },
        )
        builder.add_edge("evaluate_memory", END)
        self.graph = builder.compile(
            checkpointer=self.checkpointer,
            name="trackflow_external_support",
        )

    def classify_pending_consent(
        self,
        text: str,
        proposal: MemoryProposalRecord,
        *,
        deadline: QueryDeadline,
        rag_factory: Callable[[], AbstractContextManager[RAGService]],
    ) -> ConsentClassification:
        """Classify a pending proposal without changing graph or RAG contracts."""
        if self.consent_classifier is not None:
            return self.consent_classifier.classify(text, proposal)
        timeout = deadline.operation_timeout(
            CONSENT_CLASSIFIER_MAX_SECONDS,
            reserve=AUTOEVALUATION_RESPONSE_RESERVE_SECONDS,
        )
        with rag_factory() as service:
            generation = getattr(service, "generation_provider", None)
            provider = getattr(generation, "provider", None)
            if provider is None or not callable(
                getattr(provider, "generate_structured", None)
            ):
                raise SupportUnavailable("structured consent provider unavailable")
            return FourGeeksStructuredConsentClassifier(
                provider,
                timeout_seconds=timeout,
            ).classify(text, proposal)

    async def execute(
        self,
        question: str,
        *,
        session: Session,
        access: AccessContext,
        deadline: QueryDeadline,
        rag_factory: Callable[[], AbstractContextManager[RAGService]],
        request_id: str | None = None,
        run_id: str | None = None,
        ticket_tool=None,
        inventory_tool=None,
        tracking_tool=None,
        conversation_id: UUID | None = None,
        memory_client_id: str | None = None,
        memory_scope: MemoryScope | None = None,
    ) -> ExternalSupportExecution:
        selected_run_id = run_id or str(uuid4())
        runtime = ExternalSupportRuntime(
            session=session,
            access=access,
            deadline=deadline,
            rag_factory=rag_factory,
            request_id=request_id,
            run_id=selected_run_id,
            tracer=self.tracer,
            ticket_tool=ticket_tool,
            inventory_tool=inventory_tool,
            tracking_tool=tracking_tool,
            memory_evaluator=self.memory_evaluator,
            memory_store=self.memory_store,
            conversation_id=conversation_id,
            memory_client_id=memory_client_id,
            memory_scope=memory_scope,
        )
        self.tracer.begin(selected_run_id)
        try:
            output = await self.graph.ainvoke(
                {"question": question},
                config={"configurable": {"thread_id": selected_run_id}},
                context=runtime,
            )
            result = cast(ExternalSupportResult, output["assembled"])
            outcome = (
                "clarification"
                if result.needs_clarification
                else "partial"
                if result.partial
                else "answered"
            )
            self.tracer.finish(selected_run_id, outcome=outcome)
            return ExternalSupportExecution(
                selected_run_id,
                result,
                memory_proposal=result.memory_proposal,
                memory_evaluation=result.memory_evaluation,
                memory_context=result.memory_context,
                memory_read_status=result.memory_read_status,
            )
        except Exception as exc:
            self.tracer.finish(selected_run_id, outcome="technical_error", error=exc)
            raise
        finally:
            try:
                runtime.close()
            finally:
                await self.checkpointer.adelete_thread(selected_run_id)


_DEFAULT_EXTERNAL_AGENT = ExternalSupportAgent()


def get_external_support_agent() -> ExternalSupportAgent:
    return _DEFAULT_EXTERNAL_AGENT
