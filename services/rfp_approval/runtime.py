"""Operational construction and safe projection of the approval workflow.

SQLite stores only LangGraph state. PostgreSQL remains the business source of
truth for identities, decisions, revisions, events and final documents.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver

from pipelines.rfp_approval.hitl import (
    DepartmentApprovalWorkflow,
    PostgresDepartmentApprovalBackend,
)
from services.api.database import open_db_session


class ApprovalRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("RFP approval runtime is unavailable")
        self.code = code


def checkpoint_path() -> Path:
    raw = os.getenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH")
    if not raw:
        raise ApprovalRuntimeError("checkpoint_path_not_configured")
    path = Path(raw)
    if not path.is_absolute() or path.suffix not in {".sqlite", ".sqlite3"}:
        raise ApprovalRuntimeError("checkpoint_path_invalid")
    runtime_raw = os.getenv("TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR")
    if not runtime_raw:
        raise ApprovalRuntimeError("runtime_directory_not_configured")
    runtime_dir = Path(runtime_raw)
    if not runtime_dir.is_absolute():
        raise ApprovalRuntimeError("runtime_directory_invalid")
    try:
        path.resolve(strict=False).relative_to(runtime_dir.resolve(strict=False))
    except ValueError:
        raise ApprovalRuntimeError("checkpoint_path_outside_runtime_directory") from None
    if path.exists() and not path.is_file():
        raise ApprovalRuntimeError("checkpoint_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def open_approval_workflow() -> Iterator[DepartmentApprovalWorkflow]:
    """Open one workflow instance over the configured persistent checkpoint."""

    if os.getenv("LANGGRAPH_STRICT_MSGPACK") != "true":
        raise ApprovalRuntimeError("strict_msgpack_required")
    path = checkpoint_path()
    connection = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        saver = SqliteSaver(connection)
        backend = PostgresDepartmentApprovalBackend(open_db_session)
        yield DepartmentApprovalWorkflow(backend=backend, checkpointer=saver)
    finally:
        connection.close()


def public_workflow_state(workflow: DepartmentApprovalWorkflow, thread_id: str) -> dict[str, Any]:
    """Return the non-sensitive checkpoint projection used by API/Backoffice."""

    snapshot = workflow.state(thread_id)
    if not snapshot.values:
        raise ApprovalRuntimeError("workflow_thread_not_found")
    values = snapshot.values
    target = values.get("target", {})
    return {
        "thread_id": thread_id,
        "status": values.get("status"),
        "target": {
            "approval_run_id": target.get("approval_run_id"),
            "ticket_id": target.get("ticket_id"),
            "revision_id": target.get("revision_id"),
            "department": target.get("department"),
        },
        "interrupted": any(task.interrupts for task in snapshot.tasks),
        "interrupt_payload": values.get("interrupt_payload"),
        "decision": values.get("decision"),
        "application": values.get("application"),
        "coordination": values.get("coordination"),
    }
