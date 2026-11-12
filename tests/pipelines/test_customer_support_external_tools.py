"""Deterministic routing and graph tests for LangGraph Part 2."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from agents.customer_support.external_graph import (
    ExternalSupportAgent,
    ExternalSupportState,
    SupportUnavailable,
    route_support_query,
)
from agents.customer_support.tools import (
    InventoryLookupResult,
    TicketLookupResult,
    ToolStatus,
)
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.rag.contracts import (
    KnowledgeChunk,
    RAGConfigurationError,
    RAGProviderError,
    RetrievalResult,
)
from services.rag.resilience import QueryDeadline


def run(coroutine):
    return asyncio.run(coroutine)


class StubRAGService:
    default_k = 3
    default_min_score = 0.57

    def __init__(self, *, error=None):
        self.error = error
        self.retrieve_calls = 0
        self.generate_calls = 0

    def retrieve(self, question, k, min_score, *, request_id=None):
        self.retrieve_calls += 1
        if self.error is not None:
            raise self.error
        return (
            RetrievalResult(
                KnowledgeChunk(
                    point_id="point",
                    source_document="policy.md",
                    section="Returns",
                    chunk_index=0,
                    text="private policy text",
                ),
                0.9,
            ),
        )

    def generate_answer(self, question, context):
        self.generate_calls += 1
        return "Grounded policy answer."

    def resilience_trace(self):
        return {"external_calls": self.retrieve_calls + self.generate_calls}


@contextmanager
def rag_context(service):
    yield service


def access():
    return AccessContext("manager", Role.manager, frozenset({"client-a"}), frozenset())


def test_external_state_requires_question_without_typing_required():
    assert ExternalSupportState.__required_keys__ == frozenset({"question"})
    assert "question" not in ExternalSupportState.__optional_keys__


@pytest.mark.parametrize(
    ("question", "route", "ticket", "product"),
    [
        ("What is the returns policy?", "knowledge", None, None),
        ("What is the status of TRF-000482?", "ticket", "TRF-000482", None),
        ("Please check ticket 482", "ticket", "TRF-000482", None),
        ("How much stock is available for product 17?", "inventory", None, 17),
        ("Check stock for product_id 17", "inventory", None, 17),
        (
            "Check ticket 482 and stock for product 17",
            "mixed",
            "TRF-000482",
            17,
        ),
        (
            "Check TRF-000482 and explain the incident procedure",
            "mixed",
            "TRF-000482",
            None,
        ),
    ],
)
def test_deterministic_route_matrix(question, route, ticket, product):
    decision = route_support_query(question)
    assert decision.route == route
    assert decision.incident_id == ticket
    assert decision.product_id == product


@pytest.mark.parametrize(
    "question",
    [
        "What is the status of my ticket?",
        "Check my ticket status",
        "Check TRF-482",
        "Check TRF-000000",
        "Compare TRF-000482 and TRF-000483",
        "Compare tickets 482 and 483",
        "How much inventory is available?",
        "Check product 0",
        "Compare product 3 and product 4",
        "Compare products 3 and 4",
    ],
)
def test_missing_invalid_or_multiple_identifiers_require_clarification(question):
    assert route_support_query(question).route == "clarification"


def test_bare_number_is_not_interpreted_as_a_ticket():
    decision = route_support_query("What does 482 mean in the policy?")
    assert decision.route == "knowledge"
    assert decision.incident_id is None


def test_stock_policy_without_product_id_routes_to_knowledge():
    decision = route_support_query("What is the inventory stock policy?")
    assert decision.route == "knowledge"
    assert decision.product_id is None


def test_ticket_only_is_rag_lazy_and_checkpoint_is_deleted():
    rag_calls = 0

    def factory():
        nonlocal rag_calls
        rag_calls += 1
        return rag_context(StubRAGService())

    def ticket_tool(payload, deadline):
        return TicketLookupResult(
            ToolStatus.ok,
            incident_id=payload.incident_id,
            incident_status="OPEN",
            category="DELAYED_DELIVERY",
            version=2,
            updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        )

    agent = ExternalSupportAgent()
    execution = run(agent.execute(
        "Check ticket 482",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=factory,
        ticket_tool=ticket_tool,
    ))
    assert rag_calls == 0
    assert execution.result.ticket.incident_id == "TRF-000482"
    trace = agent.tracer.get(execution.run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "route_query",
        "ticket_lookup",
        "assemble_response",
    ]
    assert agent.checkpointer.get_tuple(
        {"configurable": {"thread_id": execution.run_id}}
    ) is None


def test_inventory_only_skips_ticket_and_rag():
    calls = []

    def forbidden(*args):
        calls.append("forbidden")
        raise AssertionError("unnecessary source called")

    def inventory_tool(payload, deadline):
        calls.append("inventory")
        return InventoryLookupResult(
            ToolStatus.ok,
            product_id=payload.product_id,
            name="Widget",
            sku="W-1",
            warehouse="LA",
            current_stock=2,
        )

    result = run(ExternalSupportAgent().execute(
        "How much stock is available for product 17?",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=forbidden,
        ticket_tool=forbidden,
        inventory_tool=inventory_tool,
    )).result
    assert calls == ["inventory"]
    assert result.inventory.current_stock == 2


def test_knowledge_only_uses_deterministic_rag_without_operational_tools():
    rag = StubRAGService()

    def forbidden(*args):
        raise AssertionError("knowledge route must not call operational tools")

    result = run(ExternalSupportAgent().execute(
        "Explain the returns policy",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=lambda: rag_context(rag),
        ticket_tool=forbidden,
        inventory_tool=forbidden,
    )).result

    assert result.route == "knowledge"
    assert result.knowledge_status == "ok"
    assert result.knowledge is not None
    assert rag.retrieve_calls == rag.generate_calls == 1


def test_mixed_execution_is_sequential_and_reuses_internal_rag_nodes():
    calls = []
    rag = StubRAGService()

    def ticket_tool(payload, deadline):
        calls.append("ticket")
        return TicketLookupResult(
            ToolStatus.ok,
            incident_id=payload.incident_id,
            incident_status="CLOSED",
            category="DAMAGE",
            version=3,
            updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        )

    def inventory_tool(payload, deadline):
        calls.append("inventory")
        return InventoryLookupResult(
            ToolStatus.ok,
            product_id=payload.product_id,
            name="Widget",
            sku="W-1",
            warehouse="LA",
            current_stock=0,
        )

    agent = ExternalSupportAgent()
    execution = run(agent.execute(
        "Check ticket 482, product 17 stock, and explain the returns policy",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=lambda: rag_context(rag),
        ticket_tool=ticket_tool,
        inventory_tool=inventory_tool,
    ))
    assert calls == ["ticket", "inventory"]
    assert rag.retrieve_calls == rag.generate_calls == 1
    assert execution.result.inventory.current_stock == 0
    trace = agent.tracer.get(execution.run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "route_query",
        "ticket_lookup",
        "inventory_lookup",
        "retrieve_context",
        "generate_answer",
        "finalize_answer",
        "assemble_response",
    ]
    assert [node["transition"] for node in trace["nodes"]] == [
        "ticket_lookup",
        "inventory_lookup",
        "retrieve_context",
        "generate_answer",
        "finalize_answer",
        "assemble_response",
        "__end__",
    ]


def test_partial_result_preserves_successful_source():
    def ticket_tool(payload, deadline):
        return TicketLookupResult(ToolStatus.timeout)

    def inventory_tool(payload, deadline):
        return InventoryLookupResult(
            ToolStatus.ok,
            product_id=payload.product_id,
            name="Widget",
            sku="W-1",
            warehouse="LA",
            current_stock=5,
        )

    result = run(ExternalSupportAgent().execute(
        "Check ticket 482 and product 17 stock",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=lambda: rag_context(StubRAGService()),
        ticket_tool=ticket_tool,
        inventory_tool=inventory_tool,
    )).result
    assert result.partial is True
    assert result.ticket.status is ToolStatus.timeout
    assert result.inventory.current_stock == 5


def test_total_technical_failure_is_sanitized_and_cleans_checkpoint():
    secret = "postgresql://user:password@host/db TRF-000482 stock=99"
    rag = StubRAGService(error=RAGProviderError(secret, category="timeout"))
    agent = ExternalSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440099"
    with pytest.raises(SupportUnavailable):
        run(agent.execute(
            "Explain the returns policy",
            session=object(),
            access=access(),
            deadline=QueryDeadline(80),
            rag_factory=lambda: rag_context(rag),
            run_id=run_id,
        ))
    serialized = json.dumps(agent.tracer.get(run_id))
    assert "returns policy" not in serialized
    assert "TRF-000482" not in serialized
    assert "stock" not in serialized
    assert "password" not in serialized
    assert agent.checkpointer.get_tuple({"configurable": {"thread_id": run_id}}) is None


def test_rag_failure_keeps_valid_ticket_as_partial_response():
    rag = StubRAGService(
        error=RAGProviderError("private provider error", category="timeout")
    )

    def ticket_tool(payload, deadline):
        return TicketLookupResult(
            ToolStatus.ok,
            incident_id=payload.incident_id,
            incident_status="OPEN",
            category="DAMAGE",
            version=1,
            updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        )

    agent = ExternalSupportAgent()
    execution = run(agent.execute(
        "Check ticket 482 and explain the damage policy",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=lambda: rag_context(rag),
        ticket_tool=ticket_tool,
    ))
    assert execution.result.partial is True
    assert execution.result.ticket.status is ToolStatus.ok
    assert execution.result.knowledge is None
    trace = agent.tracer.get(execution.run_id)
    retrieval = next(node for node in trace["nodes"] if node["node"] == "retrieve_context")
    assert retrieval["state"]["knowledge_error_category"] == "timeout"


def test_clarification_does_not_call_tools_or_rag():
    calls = []

    def forbidden(*args):
        calls.append("called")
        raise AssertionError("source must remain lazy")

    result = run(ExternalSupportAgent().execute(
        "What is the status of my ticket?",
        session=object(),
        access=access(),
        deadline=QueryDeadline(80),
        rag_factory=forbidden,
        ticket_tool=forbidden,
        inventory_tool=forbidden,
    )).result
    assert result.needs_clarification is True
    assert calls == []


def test_tools_and_rag_share_one_deadline_started_before_routing():
    class Clock:
        value = 10.0

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = QueryDeadline(80, clock=clock)
    remaining_at_rag = []

    def ticket_tool(payload, selected_deadline):
        assert selected_deadline is deadline
        clock.value += 7.5
        return TicketLookupResult(ToolStatus.not_found)

    @contextmanager
    def factory():
        remaining_at_rag.append(deadline.remaining())
        yield StubRAGService()

    run(ExternalSupportAgent().execute(
        "Check ticket 482 and explain the returns policy",
        session=object(),
        access=access(),
        deadline=deadline,
        rag_factory=factory,
        ticket_tool=ticket_tool,
    ))
    assert remaining_at_rag == [72.5]


def test_lazy_rag_configuration_failure_is_controlled_and_checkpoint_is_deleted():
    class FailingContext:
        def __enter__(self):
            raise RAGConfigurationError("private configuration")

        def __exit__(self, *args):
            raise AssertionError("an unentered context must not be exited")

    agent = ExternalSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440088"
    with pytest.raises(SupportUnavailable):
        run(agent.execute(
            "Explain the returns policy",
            session=object(),
            access=access(),
            deadline=QueryDeadline(80),
            rag_factory=FailingContext,
            run_id=run_id,
        ))
    trace = agent.tracer.get(run_id)
    assert trace["nodes"][1]["state"]["knowledge_error_category"] == "configuration_error"
    assert agent.checkpointer.get_tuple({"configurable": {"thread_id": run_id}}) is None
