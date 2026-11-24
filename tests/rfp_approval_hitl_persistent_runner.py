"""Child-process runner for the persistent HITL PostgreSQL/SQLite test.

This is test-only code.  Authentication contexts are created by the sealed QA
test adapter in phase B and are passed to LangGraph as runtime context; they are
never included in graph state or the SQLite checkpoint.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID

from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import create_engine, func, select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.hitl import (
    DepartmentApprovalTarget,
    DepartmentApprovalWorkflow,
    PostgresDepartmentApprovalBackend,
    new_human_resume,
)
from services.rfp_approval.identity import VerifiedAuthenticationContext
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_final_documents,
    rfp_human_decisions,
    rfp_workflow_events,
)
from services.rfp_intake.models import rfp_tickets


_SCHEMA_PATTERN = re.compile(r"gate_hitl_persistent_[a-f0-9]{32}")


def _manifest(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("persistent HITL manifest must be an object")
    return value


def _target(value: dict[str, str]) -> DepartmentApprovalTarget:
    return DepartmentApprovalTarget(
        approval_run_id=UUID(value["approval_run_id"]),
        ticket_id=UUID(value["ticket_id"]),
        revision_id=UUID(value["revision_id"]),
        department=DepartmentId(value["department"]),
    )


def _carlos_context() -> VerifiedAuthenticationContext:
    # Test-only boundary standing in for claims already verified by the future
    # API adapter.  No request payload is deserialized into this sealed type.
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc:hitl-persistent",
        subject="qa-carlos-hitl-persistent",
        trackflow_user_id="qa-carlos-hitl-persistent",
        general_role="manager",
    )


def _workflow(database_url: str, schema: str, saver: SqliteSaver):
    if not _SCHEMA_PATTERN.fullmatch(schema):
        raise RuntimeError("invalid persistent HITL schema")
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    backend = PostgresDepartmentApprovalBackend(lambda: Session(engine))
    return DepartmentApprovalWorkflow(backend=backend, checkpointer=saver), engine


def _phase_a(workflow, engine, warehouse, lastmile) -> dict[str, object]:
    warehouse_result = workflow.start(warehouse)
    lastmile_result = workflow.start(lastmile)
    assert warehouse_result["status"] == "awaiting_human_decision"
    assert lastmile_result["status"] == "awaiting_human_decision"
    assert warehouse_result["__interrupt__"]
    assert lastmile_result["__interrupt__"]
    with Session(engine) as session:
        decision_count = session.execute(
            select(func.count()).select_from(rfp_human_decisions)
        ).scalar_one()
        revision_statuses = dict(
            session.execute(
                select(
                    rfp_approval_department_revisions.c.department_id,
                    rfp_approval_department_revisions.c.status,
                ).where(
                    rfp_approval_department_revisions.c.revision_id.in_(
                        [warehouse.revision_id, lastmile.revision_id]
                    )
                )
            ).all()
        )
    assert decision_count == 0
    assert revision_statuses == {
        "warehouse": "pending_approval",
        "lastmile": "pending_approval",
    }
    return {
        "warehouse_interrupted": True,
        "lastmile_interrupted": True,
        "decision_count": decision_count,
    }


def _phase_b(workflow, warehouse, lastmile) -> dict[str, object]:
    assert workflow.state(warehouse.thread_id).tasks[0].interrupts
    assert workflow.state(lastmile.thread_id).tasks[0].interrupts
    result = workflow.resume(
        thread_id=lastmile.thread_id,
        command=new_human_resume(action="approve"),
        authentication=_carlos_context(),
    )
    assert result["status"] == "decision_applied"
    assert result["application"]["revision_status"] == "approved"
    assert workflow.state(lastmile.thread_id).next == ()
    assert workflow.state(warehouse.thread_id).tasks[0].interrupts
    return {
        "lastmile_status": result["application"]["revision_status"],
        "warehouse_interrupted": True,
        "decision_id": result["decision"]["decision_id"],
    }


def _phase_c(workflow, engine, warehouse, lastmile) -> dict[str, object]:
    warehouse_state = workflow.state(warehouse.thread_id)
    lastmile_state = workflow.state(lastmile.thread_id)
    assert warehouse_state.tasks[0].interrupts
    assert lastmile_state.next == ()
    assert lastmile_state.values["status"] == "decision_applied"
    decision_id = UUID(lastmile_state.values["decision"]["decision_id"])
    with Session(engine) as session:
        revisions = dict(
            session.execute(
                select(
                    rfp_approval_department_revisions.c.department_id,
                    rfp_approval_department_revisions.c.status,
                ).where(
                    rfp_approval_department_revisions.c.revision_id.in_(
                        [warehouse.revision_id, lastmile.revision_id]
                    )
                )
            ).all()
        )
        ticket_status = session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == warehouse.ticket_id
            )
        ).scalar_one()
        decision_count = session.execute(
            select(func.count())
            .select_from(rfp_human_decisions)
            .where(rfp_human_decisions.c.decision_id == decision_id)
        ).scalar_one()
        event_counts = dict(
            session.execute(
                select(rfp_workflow_events.c.event_type, func.count())
                .where(
                    rfp_workflow_events.c.decision_id == decision_id,
                    rfp_workflow_events.c.event_type.in_(
                        [
                            "human_decision_queued",
                            "human_decision_claimed",
                            "human_decision_applied",
                        ]
                    ),
                )
                .group_by(rfp_workflow_events.c.event_type)
            ).all()
        )
        final_count = session.execute(
            select(func.count()).select_from(rfp_final_documents)
        ).scalar_one()
    assert revisions == {"warehouse": "pending_approval", "lastmile": "approved"}
    assert ticket_status == "esperando_aprobación"
    assert decision_count == 1
    assert event_counts == {
        "human_decision_queued": 1,
        "human_decision_claimed": 1,
        "human_decision_applied": 1,
    }
    assert final_count == 0
    return {
        "warehouse_status": revisions["warehouse"],
        "lastmile_status": revisions["lastmile"],
        "ticket_status": ticket_status,
        "decision_count": decision_count,
        "event_counts": event_counts,
        "final_document_count": final_count,
    }


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"a", "b", "c"}:
        raise SystemExit("usage: runner <a|b|c> <manifest.json>")
    if os.getenv("LANGGRAPH_STRICT_MSGPACK") != "true":
        raise RuntimeError("LANGGRAPH_STRICT_MSGPACK=true is required")
    database_url = os.environ["TRACKFLOW_RFP_APPROVAL_HITL_TEST_DATABASE_URL"]
    manifest = _manifest(Path(sys.argv[2]))
    schema = str(manifest["schema"])
    checkpoint_path = Path(str(manifest["checkpoint_path"])).resolve()
    warehouse = _target(manifest["warehouse"])
    lastmile = _target(manifest["lastmile"])
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        workflow, engine = _workflow(database_url, schema, saver)
        try:
            result = {
                "a": lambda: _phase_a(
                    workflow, engine, warehouse, lastmile
                ),
                "b": lambda: _phase_b(workflow, warehouse, lastmile),
                "c": lambda: _phase_c(
                    workflow, engine, warehouse, lastmile
                ),
            }[sys.argv[1]]()
        finally:
            engine.dispose()
    print(json.dumps({"phase": sys.argv[1], "pid": os.getpid(), **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
