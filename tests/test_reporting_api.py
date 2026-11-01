from __future__ import annotations

from contextlib import contextmanager
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
import httpx

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


class ASGIClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def request(self, method: str, url: str, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as session:
                return await session.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


def client(role: Role) -> ASGIClient:
    app = FastAPI()
    app.include_router(reporting.router)
    app.dependency_overrides[get_current_user] = lambda: user(role)
    app.dependency_overrides[reporting.get_reporting_engine] = lambda: Engine()
    app.dependency_overrides[reporting.get_pipeline_source_engine] = lambda: Engine()
    return ASGIClient(app)


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
    assert ASGIClient(app).get("/reporting/pipeline-runs/latest").status_code == 401


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


def test_post_durable_acceptance_returns_task_identity_without_scheduling(monkeypatch):
    run_id = uuid4()
    task_id = uuid4()
    accepted = []

    def accept(_connection, week, requested_by):
        accepted.append((week, requested_by))
        return run_id, task_id, True

    monkeypatch.setattr(reporting, "accept_async_task", accept)
    response = client(Role.admin).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 202
    assert response.json() == {
        "task_id": str(task_id),
        "run_id": str(run_id),
        "week_start": "2026-01-05",
        "status": "pending",
        "status_url": f"/tasks/{task_id}",
    }
    assert accepted[0][0] == date(2026, 1, 5)
    assert isinstance(accepted[0][1], type(run_id))


def test_post_duplicate_is_409(monkeypatch):
    monkeypatch.setattr(
        reporting,
        "accept_async_task",
        lambda *_args: (uuid4(), uuid4(), False),
    )
    response = client(Role.admin).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 409


def test_post_durable_transaction_failure_is_safe_503(monkeypatch):
    monkeypatch.setattr(
        reporting,
        "accept_async_task",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("postgresql://user:secret@host/db")),
    )
    response = client(Role.admin).post(
        "/reporting/pipeline-runs", json={"week_start": "2026-01-05"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "durable acceptance unavailable"
    assert "secret" not in response.text


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
