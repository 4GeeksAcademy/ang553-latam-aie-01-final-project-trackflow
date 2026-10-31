from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.reporting.router as reporting
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user


class Engine:
    @contextmanager
    def connect(self):
        yield object()

    @contextmanager
    def begin(self):
        yield object()


def user(role: Role) -> UserInDB:
    return UserInDB(id=str(uuid4()), email=f"{role.value}@example.com", hashed_password="hash", role=role)


def client(role: Role, *, scheduler=None) -> TestClient:
    app = FastAPI()
    app.include_router(reporting.router)
    app.dependency_overrides[get_current_user] = lambda: user(role)
    app.dependency_overrides[reporting.get_reporting_engine] = lambda: Engine()
    app.dependency_overrides[reporting.get_pipeline_source_engine] = lambda: Engine()
    if scheduler is not None:
        app.dependency_overrides[reporting.get_scheduler] = lambda: scheduler
    return TestClient(app)


def test_pipeline_source_dependency_requires_distinct_engine():
    engine = Engine()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(pipeline_source_engine=engine)
        )
    )

    with pytest.raises(reporting.HTTPException) as caught:
        reporting.get_pipeline_source_engine(request, engine)

    assert caught.value.status_code == 503
    assert "must be distinct" in caught.value.detail


@pytest.mark.parametrize("role", [Role.admin, Role.manager])
def test_get_weekly_report_authorized_fallback_and_decimal_number(monkeypatch, role):
    monkeypatch.setattr(
        reporting,
        "weekly_report",
        lambda _connection, selected: (
            date(2026, 1, 5),
            [{
                "warehouse": "los_angeles",
                "client_id": "client-a",
                "inbound_units_count": 5,
                "outbound_orders_count": 2,
                "stockout_events_count": 1,
                "discrepancy_events_count": 1,
                "discrepancy_rate": Decimal("0.333333333333333333"),
            }],
        ),
    )
    response = client(role).get("/reporting/weekly-warehouse-client-performance")
    assert response.status_code == 200
    assert response.json()["week_start"] == "2026-01-05"
    assert '"discrepancy_rate":0.333333333333333333' in response.text
    assert response.json()["entries"][0]["discrepancy_rate"] == pytest.approx(1 / 3)
    assert isinstance(response.json()["entries"][0]["discrepancy_rate"], float)


def test_gets_forbid_regular_user_and_unauthenticated():
    regular = client(Role.user)
    assert regular.get("/reporting/pipeline-runs/latest").status_code == 403
    app = FastAPI()
    app.include_router(reporting.router)
    assert TestClient(app).get("/reporting/pipeline-runs/latest").status_code == 401


def test_explicit_invalid_week_is_422_before_store(monkeypatch):
    monkeypatch.setattr(reporting, "weekly_report", lambda *_args: pytest.fail("store called"))
    response = client(Role.manager).get(
        "/reporting/weekly-warehouse-client-performance?week_start=2026-01-06"
    )
    assert response.status_code == 422


def test_latest_run_contract(monkeypatch):
    run_id = uuid4()
    monkeypatch.setattr(
        reporting,
        "latest_pipeline_run",
        lambda _connection: {
            "run_id": run_id,
            "week_start": date(2026, 1, 5),
            "trigger_type": "http",
            "status": "pending",
            "started_at": datetime(2026, 1, 12, tzinfo=timezone.utc),
            "finished_at": None,
            "records_processed": None,
            "aggregates_calculated": None,
            "rows_inserted": None,
            "rows_updated": None,
            "rows_unchanged": None,
            "error_code": None,
            "error_message": None,
            "block_reason": None,
            "commit_result": "not_attempted",
            "prefect_flow_run_id": None,
            "prefect_task_run_id": None,
        },
    )
    response = client(Role.admin).get("/reporting/pipeline-runs/latest")
    assert response.status_code == 200
    assert response.json()["run_id"] == str(run_id)
    assert response.json()["status"] == "pending"


def test_post_requires_admin():
    assert client(Role.manager).post("/reporting/pipeline-runs", json={}).status_code == 403


def test_post_durable_acceptance_precedes_scheduler_and_202_is_not_published(monkeypatch):
    run_id = uuid4()
    events = []

    def accept(_connection, week):
        events.append(("durable", week))
        return run_id, True

    def schedule(_background, _engine, _source_engine, week, accepted_id):
        events.append(("scheduled", week, accepted_id))

    monkeypatch.setattr(reporting, "accept_pipeline_run", accept)
    response = client(Role.admin, scheduler=schedule).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 202
    assert response.json() == {
        "run_id": str(run_id),
        "week_start": "2026-01-05",
        "status": "accepted",
        "published": False,
    }
    assert [event[0] for event in events] == ["durable", "scheduled"]


def test_post_duplicate_is_409_and_not_scheduled(monkeypatch):
    monkeypatch.setattr(reporting, "accept_pipeline_run", lambda *_args: (uuid4(), False))
    response = client(Role.admin, scheduler=lambda *_args: pytest.fail("scheduled")).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 409


def test_scheduler_failure_after_durable_acceptance_is_recoverable_503(monkeypatch):
    run_id = uuid4()
    attempts = iter(((run_id, True), (uuid4(), True)))
    monkeypatch.setattr(reporting, "accept_pipeline_run", lambda *_args: next(attempts))
    closed = []
    monkeypatch.setattr(
        reporting,
        "mark_scheduling_failed",
        lambda _connection, failed_id, _now: closed.append(failed_id),
    )

    def fail(*_args):
        raise RuntimeError("scheduler unavailable")

    response = client(Role.admin, scheduler=fail).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 503
    assert "retryable" in response.json()["detail"]
    assert "scheduler unavailable" not in response.text
    assert closed == [run_id]

    retry = client(Role.admin, scheduler=lambda *_args: None).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert retry.status_code == 202


def test_scheduler_failure_with_uncertain_status_requires_reconciliation(monkeypatch):
    monkeypatch.setattr(reporting, "accept_pipeline_run", lambda *_args: (uuid4(), True))
    monkeypatch.setattr(
        reporting,
        "mark_scheduling_failed",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("write uncertain")),
    )
    response = client(
        Role.admin,
        scheduler=lambda *_args: (_ for _ in ()).throw(RuntimeError("scheduler unavailable")),
    ).post("/reporting/pipeline-runs", json={"week_start": "2026-01-05"})
    assert response.status_code == 503
    assert "reconciliation" in response.json()["detail"]
    assert "retry" not in response.json()["detail"]


def test_default_scheduler_reports_configuration_name_without_secret(monkeypatch):
    run_id = uuid4()
    closed = []
    monkeypatch.setattr(reporting, "accept_pipeline_run", lambda *_args: (run_id, True))
    monkeypatch.setattr(
        reporting,
        "mark_scheduling_failed",
        lambda _connection, failed_id, _now: closed.append(failed_id),
    )
    monkeypatch.delenv("TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT", raising=False)

    response = client(Role.admin).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )

    assert response.status_code == 503
    assert "TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT" in response.json()["detail"]
    assert "postgresql" not in response.text.lower()
    assert closed == [run_id]


def test_openapi_has_nominal_contracts_and_expected_statuses():
    app = FastAPI()
    app.include_router(reporting.router)
    schema = app.openapi()
    assert "/reporting/weekly-warehouse-client-performance" in schema["paths"]
    post = schema["paths"]["/reporting/pipeline-runs"]["post"]
    assert set(("202", "409", "422", "503")) <= set(post["responses"])
    assert "PipelineRunAcceptedResponse" in schema["components"]["schemas"]
    rate_schema = schema["components"]["schemas"]["WeeklyPerformanceEntry"]["properties"]["discrepancy_rate"]
    assert {item["type"] for item in rate_schema["anyOf"]} <= {"integer", "number"}
