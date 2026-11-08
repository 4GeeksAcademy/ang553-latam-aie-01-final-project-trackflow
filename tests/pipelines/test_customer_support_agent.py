from __future__ import annotations

import json

import pytest

from agents.customer_support import CustomerSupportAgent
from agents.customer_support.graph import SupportState
from agents.customer_support.tracing import TraceRecorder
from services.rag.contracts import (
    KnowledgeChunk,
    QueryResult,
    RAGProviderError,
    RetrievalResult,
    SourceReference,
)
from services.rag.service import (
    INSUFFICIENT_CONTEXT_SENTINEL,
    INSUFFICIENT_INFORMATION_ANSWER,
    RAGService,
)


def retrieved(
    source: str,
    section: str,
    *,
    kind: str = "primary",
    index: int = 0,
) -> RetrievalResult:
    return RetrievalResult(
        KnowledgeChunk(
            point_id=f"{source}-{index}",
            source_document=source,
            section=section,
            chunk_index=index,
            text="private chunk text",
        ),
        score=0.9,
        retrieval_kind=kind,
    )


class StubService:
    default_k = 3
    default_min_score = 0.61

    def __init__(
        self,
        context=(),
        answer="grounded answer",
        retrieval_error=None,
        generation_error=None,
    ):
        self.context = tuple(context)
        self.answer = answer
        self.retrieval_error = retrieval_error
        self.generation_error = generation_error
        self.retrieve_calls = []
        self.generate_calls = []

    def retrieve(self, question, k, min_score, *, request_id=None):
        self.retrieve_calls.append((question, k, min_score, request_id))
        if self.retrieval_error is not None:
            raise self.retrieval_error
        return self.context

    def generate_answer(self, question, context):
        self.generate_calls.append((question, context))
        if self.generation_error is not None:
            raise self.generation_error
        return self.answer


def test_support_state_requires_only_question():
    assert SupportState.__required_keys__ == frozenset({"question"})
    assert SupportState.__optional_keys__ == frozenset(
        {"context", "answer", "result"}
    )


def test_answered_graph_reuses_retrieval_once_and_preserves_query_contract():
    context = (
        retrieved("pricing", "storage", kind="primary"),
        retrieved("pricing", "storage", kind="complementary", index=1),
        retrieved("sla", "exceptions", kind="complementary"),
    )
    service = StubService(context)
    agent = CustomerSupportAgent()

    execution = agent.execute(
        "private question",
        service,
        request_id="550e8400-e29b-41d4-a716-446655440097",
        run_id="550e8400-e29b-41d4-a716-446655440001",
    )

    assert service.retrieve_calls == [
        (
            "private question",
            service.default_k,
            service.default_min_score,
            "550e8400-e29b-41d4-a716-446655440097",
        )
    ]
    assert service.generate_calls == [("private question", context)]
    assert execution.result == QueryResult(
        "grounded answer",
        (
            SourceReference("pricing", "storage", "primary"),
            SourceReference("sla", "exceptions", "complementary"),
        ),
        False,
    )
    trace = agent.tracer.get(execution.run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "retrieve_context",
        "generate_answer",
        "finalize_answer",
    ]
    assert [node["transition"] for node in trace["nodes"]] == [
        "generate_answer",
        "finalize_answer",
        "__end__",
    ]
    assert agent.checkpointer.get_tuple(
        {"configurable": {"thread_id": execution.run_id}}
    ) is None


def test_retrieval_abstention_skips_generation():
    service = StubService()
    agent = CustomerSupportAgent()
    execution = agent.execute("unknown", service)

    assert execution.result == QueryResult(INSUFFICIENT_INFORMATION_ANSWER, (), True)
    assert service.generate_calls == []
    trace = agent.tracer.get(execution.run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "retrieve_context",
        "abstain",
    ]


def test_generation_abstention_has_no_sources():
    service = StubService(
        (retrieved("policy", "limits"),),
        answer=INSUFFICIENT_INFORMATION_ANSWER,
    )
    agent = CustomerSupportAgent()
    execution = agent.execute("ambiguous", service)

    assert execution.result == QueryResult(INSUFFICIENT_INFORMATION_ANSWER, (), True)
    trace = agent.tracer.get(execution.run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "retrieve_context",
        "generate_answer",
        "abstain",
    ]


def test_technical_error_propagates_and_trace_is_sanitized():
    secret = "Bearer secret-token postgresql://user:password@host/db"
    service = StubService(
        retrieval_error=RAGProviderError(secret, category="timeout", status_code=504)
    )
    agent = CustomerSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440002"

    with pytest.raises(RAGProviderError, match="secret-token"):
        agent.execute("private question", service, run_id=run_id)

    serialized = json.dumps(agent.tracer.get(run_id))
    assert "private question" not in serialized
    assert "secret-token" not in serialized
    assert "password" not in serialized
    assert '"category": "timeout"' in serialized
    assert agent.checkpointer.get_tuple(
        {"configurable": {"thread_id": run_id}}
    ) is None


def test_trace_retention_is_bounded():
    agent = CustomerSupportAgent(tracer=TraceRecorder(max_runs=2, retention_seconds=60))
    for suffix in (3, 4, 5):
        agent.execute(
            "unknown",
            StubService(),
            run_id=f"550e8400-e29b-41d4-a716-44665544000{suffix}",
        )

    assert len(agent.tracer.export()) == 2
    assert agent.tracer.get("550e8400-e29b-41d4-a716-446655440003") is None


def test_trace_retention_expires_by_ttl():
    class MutableClock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = MutableClock()
    tracer = TraceRecorder(max_runs=2, retention_seconds=10, clock=clock)
    agent = CustomerSupportAgent(tracer=tracer)
    execution = agent.execute("unknown", StubService())
    assert tracer.get(execution.run_id) is not None

    clock.value = 11.0
    assert tracer.get(execution.run_id) is None


def test_generation_error_also_propagates_and_cleans_checkpoint():
    failure = RAGProviderError("private provider response", category="http_error", status_code=429)
    service = StubService(
        (retrieved("sla", "exceptions"),),
        generation_error=failure,
    )
    agent = CustomerSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440006"

    with pytest.raises(RAGProviderError) as captured:
        agent.execute("question", service, run_id=run_id)

    assert captured.value is failure
    trace = agent.tracer.get(run_id)
    assert [node["node"] for node in trace["nodes"]] == [
        "retrieve_context",
        "generate_answer",
    ]
    assert trace["nodes"][-1]["status"] == "failed"
    assert agent.checkpointer.get_tuple(
        {"configurable": {"thread_id": run_id}}
    ) is None


class LocalEmbedding:
    def embed(self, _text):
        return (1.0, 0.0)


class LocalGeneration:
    def __init__(self, answer):
        self.answer = answer

    def generate(self, _question, _context):
        return self.answer


class LocalStore:
    def __init__(self, results):
        self.results = results

    def search(self, *_args, **_kwargs):
        return self.results


def equivalent_service(*, has_context: bool, generated: str) -> RAGService:
    context = retrieved("sla-delivery", "high-demand-periods")
    results = (
        [(context.chunk.point_id, context.score, context.chunk.payload())]
        if has_context
        else []
    )
    return RAGService(
        LocalEmbedding(),
        LocalGeneration(generated),
        LocalStore(results),
        default_k=3,
        default_min_score=0.61,
    )


@pytest.mark.parametrize(
    ("has_context", "generated"),
    [
        (True, "grounded answer"),
        (False, "unused"),
        (True, INSUFFICIENT_CONTEXT_SENTINEL),
    ],
)
def test_compiled_graph_is_observably_equivalent_to_rag_query_oracle(
    has_context, generated
):
    oracle_service = equivalent_service(
        has_context=has_context,
        generated=generated,
    )
    graph_service = equivalent_service(
        has_context=has_context,
        generated=generated,
    )

    oracle = oracle_service.query("evaluation question")
    migrated = CustomerSupportAgent().execute(
        "evaluation question", graph_service
    ).result

    assert migrated == oracle
