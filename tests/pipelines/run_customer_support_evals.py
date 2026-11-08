"""Deterministic LangGraph evaluations; no network or persistent services."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from agents.customer_support import CustomerSupportAgent
from agents.customer_support.tracing import TraceRecorder
from services.rag.contracts import SourceReference
from services.rag.chunking import load_knowledge_chunks
from services.rag.service import INSUFFICIENT_CONTEXT_SENTINEL, RAGService


DEFAULT_OUTPUT = REPOSITORY_ROOT / "agents/customer_support/eval-results.json"


class StepClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, _text: str):
        self.calls += 1
        return (1.0, 0.0)


class FakeGenerationProvider:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def generate(self, _question: str, _context: str) -> str:
        self.calls += 1
        return self.answer


class FakeVectorStore:
    def __init__(self, results) -> None:
        self.results = results
        self.calls = 0

    def search(self, *_args, **_kwargs):
        self.calls += 1
        return self.results


def service_for(chunk, answer: str, *, has_context: bool = True):
    embedding = FakeEmbeddingProvider()
    generation = FakeGenerationProvider(answer)
    store = FakeVectorStore(
        [(chunk.point_id, 0.95, chunk.payload())] if has_context else []
    )
    service = RAGService(
        embedding,
        generation,
        store,
        default_k=3,
        default_min_score=0.61,
    )
    return service, embedding, generation, store


def run_evals() -> dict[str, object]:
    chunk = next(
        item
        for item in load_knowledge_chunks()
        if item.source_document == "sla-delivery"
        and item.section == "high-demand-periods"
    )
    scenarios = [
        {
            "name": "grounded_answer_route",
            "run_id": "550e8400-e29b-41d4-a716-446655440011",
            "answer": (
                "TrackFlow does not guarantee the delivery SLA during Black Friday; "
                "delivery may take up to 40% longer and this must be communicated "
                "proactively to clients in advance."
            ),
            "has_context": True,
            "expected_nodes": [
                "retrieve_context",
                "generate_answer",
                "finalize_answer",
            ],
            "expected_insufficient": False,
            "expected_generation_calls": 1,
        },
        {
            "name": "retrieval_abstention_route",
            "run_id": "550e8400-e29b-41d4-a716-446655440012",
            "answer": "unused fake answer",
            "has_context": False,
            "expected_nodes": ["retrieve_context", "abstain"],
            "expected_insufficient": True,
            "expected_generation_calls": 0,
        },
        {
            "name": "generation_abstention_route",
            "run_id": "550e8400-e29b-41d4-a716-446655440013",
            "answer": INSUFFICIENT_CONTEXT_SENTINEL,
            "has_context": True,
            "expected_nodes": ["retrieve_context", "generate_answer", "abstain"],
            "expected_insufficient": True,
            "expected_generation_calls": 1,
        },
    ]
    evaluations: list[dict[str, object]] = []
    for scenario in scenarios:
        tracer = TraceRecorder(clock=StepClock())
        agent = CustomerSupportAgent(tracer=tracer)
        service, embedding, generation, store = service_for(
            chunk,
            scenario["answer"],
            has_context=scenario["has_context"],
        )
        execution = agent.execute(
            "Academic evaluation input",
            service,
            request_id="550e8400-e29b-41d4-a716-446655440010",
            run_id=scenario["run_id"],
        )
        trace = tracer.get(execution.run_id)
        actual_nodes = [node["node"] for node in trace["nodes"]]
        checks = {
            "compiled_route": actual_nodes == scenario["expected_nodes"],
            "retrieval_once": embedding.calls == 1 and store.calls == 1,
            "generation_calls": generation.calls
            == scenario["expected_generation_calls"],
            "expected_abstention": execution.result.insufficient_context
            is scenario["expected_insufficient"],
            "checkpoint_removed": agent.checkpointer.get_tuple(
                {"configurable": {"thread_id": execution.run_id}}
            )
            is None,
        }
        if scenario["name"] == "grounded_answer_route":
            normalized_answer = execution.result.answer.casefold()
            checks["exact_authentic_source"] = execution.result.sources == (
                SourceReference("sla-delivery", "high-demand-periods", "primary"),
            )
            checks["authentic_policy_conditions"] = all(
                condition in normalized_answer
                for condition in (
                    "black friday",
                    "does not guarantee",
                    "40%",
                    "communicated proactively",
                )
            )
        evaluations.append(
            {
                "name": scenario["name"],
                "run_id": execution.run_id,
                "passed": all(checks.values()),
                "checks": checks,
                "trace": trace,
            }
        )
    return {
        "schema_version": 1,
        "scope": "compiled_graph_with_local_fakes",
        "precision_claim": False,
        "passed": all(item["passed"] for item in evaluations),
        "evaluations": evaluations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    report = run_evals()
    arguments.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(arguments.output), "passed": report["passed"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
