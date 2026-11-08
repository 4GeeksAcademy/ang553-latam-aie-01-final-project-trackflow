"""Compiled LangGraph wrapper around the frozen TrackFlow RAG service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, cast
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agents.customer_support.tracing import TraceRecorder
from services.rag.contracts import QueryResult, RetrievalResult, SourceReference
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService


class _SupportInput(TypedDict):
    question: str


class SupportState(_SupportInput, total=False):
    context: tuple[RetrievalResult, ...]
    answer: str
    result: QueryResult


@dataclass(frozen=True)
class SupportRuntime:
    service: RAGService
    request_id: str | None
    run_id: str
    tracer: TraceRecorder


@dataclass(frozen=True)
class SupportExecution:
    run_id: str
    result: QueryResult


def _state_summary(state: SupportState) -> dict[str, object]:
    context = state.get("context", ())
    result = state.get("result")
    return {
        "has_question": "question" in state,
        "context_count": len(context),
        "has_answer": "answer" in state,
        "has_result": result is not None,
        "source_count": 0 if result is None else len(result.sources),
        "insufficient_context": None if result is None else result.insufficient_context,
    }


def _recorded_node(name: str, operation):
    def node(state: SupportState, runtime: Runtime[SupportRuntime]) -> SupportState:
        started = runtime.context.tracer.now()
        try:
            updates, transition = operation(state, runtime.context)
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
        combined = cast(SupportState, {**state, **updates})
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


def _retrieve(state: SupportState, context: SupportRuntime):
    service = context.service
    retrieved = service.retrieve(
        state["question"],
        service.default_k,
        service.default_min_score,
        request_id=context.request_id,
    )
    transition = "generate_answer" if retrieved else "abstain"
    return {"context": retrieved}, transition


def _generate(state: SupportState, context: SupportRuntime):
    answer = context.service.generate_answer(state["question"], state["context"])
    transition = (
        "abstain" if answer == INSUFFICIENT_INFORMATION_ANSWER else "finalize_answer"
    )
    return {"answer": answer}, transition


def _abstain(state: SupportState, _context: SupportRuntime):
    return {
        "result": QueryResult(INSUFFICIENT_INFORMATION_ANSWER, (), True)
    }, "__end__"


def _finalize_answer(state: SupportState, _context: SupportRuntime):
    sources: list[SourceReference] = []
    seen: set[tuple[str, str]] = set()
    for retrieved in state["context"]:
        key = (retrieved.chunk.source_document, retrieved.chunk.section)
        if key not in seen:
            seen.add(key)
            sources.append(SourceReference(*key, retrieval_kind=retrieved.retrieval_kind))
    return {
        "result": QueryResult(state["answer"], tuple(sources), False)
    }, "__end__"


def _after_retrieval(state: SupportState) -> str:
    return "generate_answer" if state["context"] else "abstain"


def _after_generation(state: SupportState) -> str:
    return (
        "abstain"
        if state["answer"] == INSUFFICIENT_INFORMATION_ANSWER
        else "finalize_answer"
    )


class CustomerSupportAgent:
    """Own the compiled graph, ephemeral checkpoints, and sanitized traces."""

    def __init__(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        tracer: TraceRecorder | None = None,
    ) -> None:
        self.checkpointer = checkpointer or InMemorySaver()
        self.tracer = tracer or TraceRecorder()
        builder = StateGraph(SupportState, context_schema=SupportRuntime)
        builder.add_node("retrieve_context", _recorded_node("retrieve_context", _retrieve))
        builder.add_node("generate_answer", _recorded_node("generate_answer", _generate))
        builder.add_node("abstain", _recorded_node("abstain", _abstain))
        builder.add_node(
            "finalize_answer",
            _recorded_node("finalize_answer", _finalize_answer),
        )
        builder.add_edge(START, "retrieve_context")
        builder.add_conditional_edges(
            "retrieve_context",
            _after_retrieval,
            {"generate_answer": "generate_answer", "abstain": "abstain"},
        )
        builder.add_conditional_edges(
            "generate_answer",
            _after_generation,
            {"finalize_answer": "finalize_answer", "abstain": "abstain"},
        )
        builder.add_edge("abstain", END)
        builder.add_edge("finalize_answer", END)
        self.graph = builder.compile(
            checkpointer=self.checkpointer,
            name="trackflow_customer_support",
        )

    def execute(
        self,
        question: str,
        service: RAGService,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> SupportExecution:
        selected_run_id = run_id or str(uuid4())
        self.tracer.begin(selected_run_id)
        try:
            output = self.graph.invoke(
                {"question": question},
                config={"configurable": {"thread_id": selected_run_id}},
                context=SupportRuntime(
                    service=service,
                    request_id=request_id,
                    run_id=selected_run_id,
                    tracer=self.tracer,
                ),
            )
            result = cast(QueryResult, output["result"])
            outcome = "abstained" if result.insufficient_context else "answered"
            self.tracer.finish(selected_run_id, outcome=outcome)
            return SupportExecution(selected_run_id, result)
        except Exception as exc:
            self.tracer.finish(selected_run_id, outcome="technical_error", error=exc)
            raise
        finally:
            # Checkpoints contain graph state, including content. They are useful only
            # during this synchronous Part 1 run and are removed at termination.
            self.checkpointer.delete_thread(selected_run_id)


_DEFAULT_AGENT = CustomerSupportAgent()


def get_customer_support_agent() -> CustomerSupportAgent:
    return _DEFAULT_AGENT

