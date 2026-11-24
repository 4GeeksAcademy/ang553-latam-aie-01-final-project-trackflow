from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from data.pipelines.rfp_intake.contracts import DepartmentId
from services.rfp_approval.read_model import list_department_review_inbox


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _Result(self.rows)


def test_inbox_query_is_environment_department_current_and_pending_scoped() -> None:
    now = datetime.now(timezone.utc)
    row = {
        "approval_run_id": uuid4(),
        "ticket_id": uuid4(),
        "revision_id": uuid4(),
        "department_id": "warehouse",
        "review_revision_no": 2,
        "status": "pending_approval",
        "content_hash": "a" * 64,
        "validation_bundle_id": uuid4(),
        "created_at": now,
    }
    session = _Session([row])

    items = list_department_review_inbox(
        session, department="warehouse", environment="qa"
    )

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "rfp_approval_runs.environment = 'qa'" in compiled
    assert "rfp_approval_runs.status = 'active'" in compiled
    assert "rfp_approval_department_revisions.department_id = 'warehouse'" in compiled
    assert "rfp_approval_department_revisions.is_current IS true" in compiled
    assert "rfp_approval_department_revisions.status = 'pending_approval'" in compiled
    assert items[0]["department"] is DepartmentId.warehouse
    assert items[0]["revision_id"] == row["revision_id"]
