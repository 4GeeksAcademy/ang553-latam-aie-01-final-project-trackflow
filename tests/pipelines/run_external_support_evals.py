"""Offline, reproducible evaluations for the external support graph."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from agents.customer_support.external_graph import (
    ExternalSupportAgent,
    SupportUnavailable,
    route_support_query,
)
from agents.customer_support.tools import InventoryLookupResult, TicketLookupResult, ToolStatus
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.rag.contracts import KnowledgeChunk, RetrievalResult
from services.rag.resilience import QueryDeadline

DEFAULT_OUTPUT = REPOSITORY_ROOT / "agents/customer_support/external-eval-results.json"


class StubRAG:
    default_k = 3
    default_min_score = 0.57

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.retrieve_calls = 0
        self.generate_calls = 0

    def retrieve(self, *_args, **_kwargs):
        self.retrieve_calls += 1
        if not self.available:
            from services.rag.contracts import RAGProviderError

            raise RAGProviderError("synthetic provider failure", category="provider_error")
        return (
            RetrievalResult(
                KnowledgeChunk("eval-point", "approved-policy", "returns", 0, "synthetic context"),
                0.95,
            ),
        )

    def generate_answer(self, *_args, **_kwargs):
        self.generate_calls += 1
        return "Grounded synthetic evaluation answer."

    def resilience_trace(self):
        return {"external_calls": self.retrieve_calls + self.generate_calls}


@contextmanager
def rag_context(service):
    yield service


def _access() -> AccessContext:
    return AccessContext("eval-manager", Role.manager, frozenset({"eval-client"}), frozenset())


def _ticket(status: ToolStatus):
    calls = {"count": 0}

    def tool(*_args):
        calls["count"] += 1
        if status is not ToolStatus.ok:
            return TicketLookupResult(status)
        return TicketLookupResult(
            status,
            incident_id="TRF-000482",
            incident_status="OPEN",
            category="DELAYED_DELIVERY",
            version=1,
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    return tool, calls


def _inventory(status: ToolStatus, *, stock: int = 4):
    calls = {"count": 0}

    def tool(*_args):
        calls["count"] += 1
        if status is not ToolStatus.ok:
            return InventoryLookupResult(status)
        return InventoryLookupResult(
            status,
            product_id=17,
            name="Evaluation product",
            sku="EVAL-SKU",
            warehouse="Evaluation warehouse",
            current_stock=stock,
        )

    return tool, calls


def run_evals() -> dict[str, object]:
    scenarios = (
        {"name": "knowledge_only", "question": "What is the returns policy?", "route": "knowledge", "rag": True, "calls": (0, 0, 1), "statuses": (None, None, "ok")},
        {"name": "ticket_found", "question": "Check ticket 482", "route": "ticket", "calls": (1, 0, 0), "statuses": ("ok", None, None)},
        {"name": "ticket_cross_tenant_hidden", "question": "Check ticket 482", "route": "ticket", "ticket": ToolStatus.not_found, "calls": (1, 0, 0), "statuses": ("not_found", None, None)},
        {"name": "inventory_existing", "question": "Check stock for product 17", "route": "inventory", "calls": (0, 1, 0), "statuses": (None, "ok", None)},
        {"name": "inventory_zero", "question": "Check stock for product 17", "route": "inventory", "stock": 0, "calls": (0, 1, 0), "statuses": (None, "ok", None)},
        {"name": "inventory_cross_tenant_hidden", "question": "Check stock for product 17", "route": "inventory", "inventory": ToolStatus.not_found, "calls": (0, 1, 0), "statuses": (None, "not_found", None)},
        {"name": "ticket_inventory", "question": "Check ticket 482 and stock for product 17", "route": "mixed", "calls": (1, 1, 0), "statuses": ("ok", "ok", None)},
        {"name": "ticket_knowledge", "question": "Check ticket 482 and explain the incident policy", "route": "mixed", "rag": True, "calls": (1, 0, 1), "statuses": ("ok", None, "ok")},
        {"name": "missing_identifier", "question": "Check my ticket status", "route": "clarification", "calls": (0, 0, 0), "statuses": (None, None, None), "clarification": True},
        {"name": "partial_ticket_timeout", "question": "Check ticket 482 and stock for product 17", "route": "mixed", "ticket": ToolStatus.timeout, "calls": (1, 1, 0), "statuses": ("timeout", "ok", None), "partial": True},
        {"name": "partial_inventory_unavailable", "question": "Check ticket 482 and stock for product 17", "route": "mixed", "inventory": ToolStatus.unavailable, "calls": (1, 1, 0), "statuses": ("ok", "unavailable", None), "partial": True},
        {"name": "partial_knowledge_failure", "question": "Check ticket 482 and explain the incident policy", "route": "mixed", "rag": False, "calls": (1, 0, 1), "statuses": ("ok", None, "unavailable"), "partial": True},
        {"name": "total_failure", "question": "Check ticket 482", "route": "ticket", "ticket": ToolStatus.timeout, "calls": (1, 0, 0), "statuses": ("timeout", None, None), "raises": True},
    )
    evaluations: list[dict[str, object]] = []
    for index, scenario in enumerate(scenarios, 1):
        rag = StubRAG(available=scenario.get("rag", True))
        agent = ExternalSupportAgent()
        ticket_tool, ticket_calls = _ticket(scenario.get("ticket", ToolStatus.ok))
        inventory_tool, inventory_calls = _inventory(
            scenario.get("inventory", ToolStatus.ok), stock=scenario.get("stock", 4)
        )
        run_id = f"00000000-0000-4000-8000-{index:012d}"
        result = None
        raised = False
        try:
            execution = agent.execute(
                scenario["question"],
                session=object(),
                access=_access(),
                deadline=QueryDeadline(80),
                rag_factory=lambda rag=rag: rag_context(rag),
                run_id=run_id,
                ticket_tool=ticket_tool,
                inventory_tool=inventory_tool,
            )
            result = execution.result
        except SupportUnavailable:
            raised = True
        trace = agent.tracer.get(run_id)
        nodes = [] if trace is None else [
            {"node": item["node"], "status": item["status"]}
            for item in trace["nodes"]
        ]
        actual_statuses = (
            None if result is None or result.ticket is None else result.ticket.status.value,
            None if result is None or result.inventory is None else result.inventory.status.value,
            None if result is None else result.knowledge_status,
        )
        if result is None and trace is not None and trace["nodes"]:
            terminal_state = trace["nodes"][-1]["state"]
            actual_statuses = (
                terminal_state["ticket_status"],
                terminal_state["inventory_status"],
                terminal_state["knowledge_status"],
            )
        checks = {
            "route": route_support_query(scenario["question"]).route == scenario["route"],
            "controlled_total_failure": raised is bool(scenario.get("raises", False)),
            "partial": result is None or result.partial is bool(scenario.get("partial", False)),
            "clarification": result is None or result.needs_clarification is bool(scenario.get("clarification", False)),
            "component_status": actual_statuses == scenario["statuses"],
            "call_counts": (ticket_calls["count"], inventory_calls["count"], rag.retrieve_calls) == scenario["calls"],
            "checkpoint_removed": agent.checkpointer.get_tuple({"configurable": {"thread_id": run_id}}) is None,
        }
        evaluations.append({
            "case": scenario["name"],
            "passed": all(checks.values()),
            "checks": checks,
            "route": scenario["route"],
            "component_status": {
                "ticket": actual_statuses[0],
                "inventory": actual_statuses[1],
                "knowledge": actual_statuses[2],
            },
            "trace": nodes,
        })
    return {
        "schema_version": 1,
        "scope": "external_support_graph_offline_fakes",
        "precision_claim": False,
        "contains_operational_data": False,
        "passed": all(item["passed"] for item in evaluations),
        "evaluations": evaluations,
    }


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    report = run_evals()
    write_report(arguments.output, report)
    print(json.dumps({"output": str(arguments.output), "passed": report["passed"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
