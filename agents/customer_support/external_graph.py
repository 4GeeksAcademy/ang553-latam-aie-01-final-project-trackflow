"""Deterministic LangGraph routing across RAG and operational read tools."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Literal, cast
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlmodel import Session

from agents.customer_support.graph import (
    SupportState,
    add_rag_nodes,
)
from agents.customer_support.tools import (
    InventoryLookupInput,
    InventoryLookupResult,
    TicketLookupInput,
    TicketLookupResult,
    ToolStatus,
    lookup_inventory_product,
    lookup_ticket,
)
from agents.customer_support.tracing import TraceRecorder
from services.api.access_control import AccessContext
from services.rag.contracts import QueryResult, RAGConfigurationError, RAGProviderError
from services.rag.resilience import QueryDeadline
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService


SupportRoute = Literal["clarification", "knowledge", "ticket", "inventory", "mixed"]
ComponentStatus = Literal["ok", "not_found", "timeout", "unavailable"]


class ExternalSupportState(SupportState, total=False):
    route: SupportRoute
    needs_ticket: bool
    needs_inventory: bool
    needs_knowledge: bool
    incident_id: str
    product_id: int
    clarification: str
    ticket: TicketLookupResult
    inventory: InventoryLookupResult
    knowledge_status: ComponentStatus
    knowledge_error_category: str
    partial: bool
    needs_clarification: bool
    assembled: "ExternalSupportResult"


@dataclass(frozen=True)
class RouteDecision:
    route: SupportRoute
    needs_ticket: bool = False
    needs_inventory: bool = False
    needs_knowledge: bool = False
    incident_id: str | None = None
    product_id: int | None = None
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


@dataclass(frozen=True)
class ExternalSupportExecution:
    run_id: str
    result: ExternalSupportResult


class SupportUnavailable(RuntimeError):
    category = "unavailable"


_TRF_TOKEN = re.compile(r"\bTRF-[A-Za-z0-9-]+\b", re.IGNORECASE)
_VALID_TRF = re.compile(r"\bTRF-(\d{6})\b", re.IGNORECASE)
_TICKET_NUMBER = re.compile(r"\bticket\s+(?:id\s+)?#?(\d+)\b", re.IGNORECASE)
_TICKET_PAIR = re.compile(
    r"\btickets?\s+(?:ids?\s+)?#?(\d+)\s*(?:,|and)\s*(?:ticket\s*)?#?(\d+)\b",
    re.IGNORECASE,
)
_PRODUCT_NUMBER = re.compile(
    r"\bproduct(?:\s+id|_id)?\s+#?(-?\d+)\b", re.IGNORECASE
)
_PRODUCT_PAIR = re.compile(
    r"\bproducts?\s+(?:ids?\s+)?#?(-?\d+)\s*(?:,|and)\s*(?:product\s*)?#?(-?\d+)\b",
    re.IGNORECASE,
)
_TICKET_WORD = re.compile(r"\b(ticket|incident|support case)\b", re.IGNORECASE)
_TICKET_OPERATION = re.compile(
    r"\b(status|current|lookup|look up|check|open|closed|discarded)\b",
    re.IGNORECASE,
)
_INVENTORY_OPERATION = re.compile(
    r"\b(stock|inventory|available|availability|units?)\b", re.IGNORECASE
)
_KNOWLEDGE_WORD = re.compile(
    r"\b(policy|policies|procedure|procedures|process|rules?|sla|requirements?|guidelines?)\b",
    re.IGNORECASE,
)


def route_support_query(question: str) -> RouteDecision:
    """Classify supported English requests without a model call."""
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

    needs_ticket = bool(ticket_ids)
    needs_inventory = bool(product_ids)
    needs_knowledge = knowledge_intent or not (needs_ticket or needs_inventory)
    selected = sum((needs_ticket, needs_inventory, needs_knowledge))
    if selected > 1:
        route: SupportRoute = "mixed"
    elif needs_ticket:
        route = "ticket"
    elif needs_inventory:
        route = "inventory"
    else:
        route = "knowledge"
    return RouteDecision(
        route,
        needs_ticket=needs_ticket,
        needs_inventory=needs_inventory,
        needs_knowledge=needs_knowledge,
        incident_id=next(iter(ticket_ids), None),
        product_id=next(iter(product_ids), None),
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
        ticket_tool=lookup_ticket,
        inventory_tool=lookup_inventory_product,
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


def _state_summary(state: ExternalSupportState) -> dict[str, object]:
    ticket = state.get("ticket")
    inventory = state.get("inventory")
    knowledge = state.get("result")
    return {
        "route": state.get("route"),
        "needs_ticket": state.get("needs_ticket", False),
        "needs_inventory": state.get("needs_inventory", False),
        "needs_knowledge": state.get("needs_knowledge", False),
        "ticket_status": None if ticket is None else ticket.status.value,
        "ticket_call_count": int(ticket is not None),
        "inventory_status": None if inventory is None else inventory.status.value,
        "inventory_call_count": int(inventory is not None),
        "knowledge_status": state.get("knowledge_status"),
        "knowledge_error_category": state.get("knowledge_error_category"),
        "knowledge_source_count": 0 if knowledge is None else len(knowledge.sources),
        "partial": state.get("partial", False),
        "needs_clarification": state.get("needs_clarification", False),
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


def _route_node(state: ExternalSupportState, _runtime: ExternalSupportRuntime):
    decision = route_support_query(state["question"])
    updates: ExternalSupportState = {
        "route": decision.route,
        "needs_ticket": decision.needs_ticket,
        "needs_inventory": decision.needs_inventory,
        "needs_knowledge": decision.needs_knowledge,
    }
    if decision.incident_id is not None:
        updates["incident_id"] = decision.incident_id
    if decision.product_id is not None:
        updates["product_id"] = decision.product_id
    if decision.clarification is not None:
        updates["clarification"] = decision.clarification
    transition = _after_route(cast(ExternalSupportState, {**state, **updates}))
    return updates, transition


def _after_route(state: ExternalSupportState) -> str:
    if state["route"] == "clarification":
        return "assemble_response"
    if state.get("needs_ticket"):
        return "ticket_lookup"
    if state.get("needs_inventory"):
        return "inventory_lookup"
    return "retrieve_context"


def _ticket_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    result = runtime.ticket_tool(
        runtime.session,
        TicketLookupInput(state["incident_id"]),
        runtime.access,
        runtime.deadline,
    )
    transition = (
        "inventory_lookup"
        if state.get("needs_inventory")
        else "retrieve_context"
        if state.get("needs_knowledge")
        else "assemble_response"
    )
    return {"ticket": result}, transition


def _inventory_node(state: ExternalSupportState, runtime: ExternalSupportRuntime):
    result = runtime.inventory_tool(
        runtime.session,
        InventoryLookupInput(state["product_id"]),
        runtime.access,
        runtime.deadline,
    )
    transition = (
        "retrieve_context" if state.get("needs_knowledge") else "assemble_response"
    )
    return {"inventory": result}, transition


def _after_ticket(state: ExternalSupportState) -> str:
    if state.get("needs_inventory"):
        return "inventory_lookup"
    return "retrieve_context" if state.get("needs_knowledge") else "assemble_response"


def _after_inventory(state: ExternalSupportState) -> str:
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


def _assemble_node(state: ExternalSupportState, _runtime: ExternalSupportRuntime):
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
        answer="\n\n".join(parts),
        route=route,
        partial=partial,
        needs_clarification=False,
        ticket=ticket,
        inventory=inventory,
        knowledge=knowledge,
        knowledge_status=knowledge_status,
    )
    return {
        "partial": partial,
        "needs_clarification": False,
        "assembled": result,
    }, "__end__"


class ExternalSupportAgent:
    """Compiled graph with deterministic routing and ephemeral checkpoints."""

    def __init__(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        tracer: TraceRecorder | None = None,
    ) -> None:
        self.checkpointer = checkpointer or InMemorySaver()
        self.tracer = tracer or TraceRecorder()
        builder = StateGraph(ExternalSupportState, context_schema=ExternalSupportRuntime)
        builder.add_node("route_query", _recorded_node("route_query", _route_node))
        builder.add_node("ticket_lookup", _recorded_node("ticket_lookup", _ticket_node))
        builder.add_node(
            "inventory_lookup", _recorded_node("inventory_lookup", _inventory_node)
        )
        builder.add_node(
            "assemble_response", _recorded_node("assemble_response", _assemble_node)
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
                "ticket_lookup": "ticket_lookup",
                "inventory_lookup": "inventory_lookup",
                "retrieve_context": "retrieve_context",
            },
        )
        builder.add_conditional_edges(
            "ticket_lookup",
            _after_ticket,
            {
                "inventory_lookup": "inventory_lookup",
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_conditional_edges(
            "inventory_lookup",
            _after_inventory,
            {
                "retrieve_context": "retrieve_context",
                "assemble_response": "assemble_response",
            },
        )
        builder.add_edge("assemble_response", END)
        self.graph = builder.compile(
            checkpointer=self.checkpointer,
            name="trackflow_external_support",
        )

    def execute(
        self,
        question: str,
        *,
        session: Session,
        access: AccessContext,
        deadline: QueryDeadline,
        rag_factory: Callable[[], AbstractContextManager[RAGService]],
        request_id: str | None = None,
        run_id: str | None = None,
        ticket_tool=lookup_ticket,
        inventory_tool=lookup_inventory_product,
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
        )
        self.tracer.begin(selected_run_id)
        try:
            output = self.graph.invoke(
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
            return ExternalSupportExecution(selected_run_id, result)
        except Exception as exc:
            self.tracer.finish(selected_run_id, outcome="technical_error", error=exc)
            raise
        finally:
            try:
                runtime.close()
            finally:
                self.checkpointer.delete_thread(selected_run_id)


_DEFAULT_EXTERNAL_AGENT = ExternalSupportAgent()


def get_external_support_agent() -> ExternalSupportAgent:
    return _DEFAULT_EXTERNAL_AGENT
