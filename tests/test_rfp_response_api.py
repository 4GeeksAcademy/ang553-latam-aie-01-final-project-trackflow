from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
from services.api.routes import rfp
from services.rfp_response.http_contracts import ResponseGlobalPublic, SafePreviewPublic
from services.rfp_response.store import (
    ResponsePersistenceConflict, _global_evaluation_status, _public_preview,
)


class ASGIClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def request(self, method: str, url: str, *, json: object | None = None):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
            ) as client:
                return await client.request(method, url, json=json)
        return asyncio.run(send())


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def user(role: Role) -> UserInDB:
    return UserInDB(email=f"{role.value}@trackflow.test", hashed_password="hash", role=role)


def app_with(role: Role | None, session: FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(rfp.router)
    app.dependency_overrides[get_db] = lambda: session
    if role is not None:
        app.dependency_overrides[get_current_user] = lambda: user(role)
    return app


def test_response_routes_require_admin_and_enqueue_without_running_worker(monkeypatch) -> None:
    ticket_id, run_id, job_id = uuid4(), uuid4(), uuid4()
    calls: list[object] = []
    monkeypatch.setattr(rfp, "ticket_exists", lambda *_args: True)
    monkeypatch.setattr(rfp, "accept_response_generation", lambda *_args, **_kwargs:
                        calls.append(_kwargs) or {"run_id": run_id, "job_id": job_id})

    assert ASGIClient(app_with(None, FakeSession())).request(
        "POST", f"/rfps/{ticket_id}/response").status_code == 401
    assert ASGIClient(app_with(Role.user, FakeSession())).request(
        "POST", f"/rfps/{ticket_id}/response").status_code == 403
    session = FakeSession()
    response = ASGIClient(app_with(Role.admin, session)).request(
        "POST", f"/rfps/{ticket_id}/response",
        json={"carrier_model": "customer_managed"})

    assert response.status_code == 202
    assert response.json() == {"ticket_id": str(ticket_id), "run_id": str(run_id),
                               "job_id": str(job_id), "status": "queued"}
    assert calls[0]["ticket_id"] == ticket_id
    assert calls[0]["carrier_model"] == "customer_managed"
    assert calls[0]["confirmation_source"] == "admin_response_start"
    assert isinstance(calls[0]["confirmation_actor"], str)
    assert session.commits == 1


def test_response_start_rejects_ineligible_ticket_and_missing_ticket(monkeypatch) -> None:
    ticket_id = uuid4()
    session = FakeSession()
    app = app_with(Role.admin, session)
    monkeypatch.setattr(rfp, "ticket_exists", lambda *_args: False)
    assert ASGIClient(app).request("POST", f"/rfps/{ticket_id}/response").status_code == 404

    monkeypatch.setattr(rfp, "ticket_exists", lambda *_args: True)
    monkeypatch.setattr(rfp, "accept_response_generation", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(ResponsePersistenceConflict("not eligible")))
    response = ASGIClient(app).request("POST", f"/rfps/{ticket_id}/response")
    assert response.status_code == 409 and session.rollbacks == 1


def test_response_start_rejects_unknown_carrier_model_before_persistence(monkeypatch) -> None:
    ticket_id = uuid4()
    monkeypatch.setattr(rfp, "ticket_exists", lambda *_args: True)
    monkeypatch.setattr(rfp, "accept_response_generation", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("store must not be called")))
    response = ASGIClient(app_with(Role.admin, FakeSession())).request(
        "POST", f"/rfps/{ticket_id}/response", json={"carrier_model": "guessed"})
    assert response.status_code == 422


def test_response_start_without_body_preserves_compatible_unknown_default(monkeypatch) -> None:
    ticket_id, run_id, job_id = uuid4(), uuid4(), uuid4()
    calls: list[object] = []
    monkeypatch.setattr(rfp, "ticket_exists", lambda *_args: True)
    monkeypatch.setattr(rfp, "accept_response_generation", lambda *_args, **_kwargs:
                        calls.append(_kwargs) or {"run_id": run_id, "job_id": job_id})

    response = ASGIClient(app_with(Role.admin, FakeSession())).request(
        "POST", f"/rfps/{ticket_id}/response")

    assert response.status_code == 202
    assert calls[0]["carrier_model"] == "unknown"


def test_response_status_separates_business_outcome_and_operational_failure(monkeypatch) -> None:
    ticket_id, run_id, job_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    projection = {
        "ticket_id": ticket_id, "run_id": run_id, "status": "technical_failure",
        "carrier_model": "trackflow_managed",
        "outcome": None, "technical_failure": True,
        "job": {"job_id": job_id, "status": "failed", "attempt_count": 3,
                "max_attempts": 3, "last_error_code": "response_technical_failure",
                "last_error_summary": "Response worker stopped"},
        "departments": [],
        "global_terms": {"status": "technical_failure",
                         "compliance_status": "technical_failure",
                         "compliance_pass": None, "evaluation_executed": False,
                         "iteration_count": 0,
                         "preview": None, "evaluation": None,
                         "pending_commercial_items": []},
        "operational_failures": [{"evaluator": "readability",
            "infrastructure_attempts": 3, "error_code": "dependency_unavailable",
            "terminal": True, "occurred_at": now}],
        "updated_at": now,
    }
    monkeypatch.setattr(rfp, "get_response_generation", lambda *_args, **_kwargs: projection)
    response = ASGIClient(app_with(Role.admin, FakeSession())).request(
        "GET", f"/rfps/{ticket_id}/response/{run_id}")
    body = response.json()
    assert response.status_code == 200
    assert body["technical_failure"] is True and body["outcome"] is None
    assert body["carrier_model"] == "trackflow_managed"
    assert "diagnostic_reference" not in response.text
    assert "carrier_confirmation_actor" not in response.text
    assert "carrier_confirmation_source" not in response.text
    assert "carrier_confirmed_at" not in response.text


@pytest.mark.parametrize(
    ("case", "run_status", "draft_present", "compliance_pass", "expected"),
    [
        ("department_needs_human", "needs_human_review", True, True, "passed"),
        ("all_departments_passed", "ready_for_part3", True, True, "passed"),
        ("global_business_rule_failed", "en_evaluación", True, False, "failed"),
        ("global_not_yet_evaluated", "en_evaluación", True, None, "en_evaluación"),
        ("terminal_technical_failure", "technical_failure", False, None, "technical_failure"),
    ],
)
def test_global_compliance_projection_is_independent_from_proposal_outcome(
        case: str, run_status: str, draft_present: bool,
        compliance_pass: bool | None, expected: str) -> None:
    assert case
    assert _global_evaluation_status(
        run_status=run_status, global_draft_present=draft_present,
        compliance_pass=compliance_pass) == expected


@pytest.mark.parametrize(
    ("status", "compliance_pass", "evaluation_executed", "overall_pass"),
    [
        ("passed", True, True, False),
        ("passed", True, True, True),
        ("failed", False, True, False),
        ("en_evaluación", None, False, None),
        ("technical_failure", None, False, None),
    ],
)
def test_global_http_contract_represents_compliance_independently(
        status: str, compliance_pass: bool | None,
        evaluation_executed: bool, overall_pass: bool | None) -> None:
    evaluation = None if overall_pass is None else {
        "evaluation_id": uuid4(), "overall_pass": overall_pass,
        "terminal_status": "ready_for_part3" if overall_pass else None,
        "actionable_feedback": [], "missing_aspect_ids": [],
        "contradicted_aspect_ids": [], "blocks_commercial_completeness": False,
        "requires_human_input": not overall_pass and compliance_pass is True,
        "blocking_rule_ids": [],
    }
    projection = ResponseGlobalPublic(
        status=status, compliance_status=status,
        compliance_pass=compliance_pass, evaluation_executed=evaluation_executed,
        iteration_count=1 if evaluation_executed else 0,
        preview=None, evaluation=evaluation, pending_commercial_items=[])

    assert projection.compliance_status == status
    assert projection.compliance_pass is compliance_pass
    assert projection.evaluation_executed is evaluation_executed


@pytest.mark.parametrize("gate_status", ["blocked", "indeterminate", "technical_error"])
def test_unverified_preview_discards_persisted_content_and_contract_fails_closed(
        gate_status: str) -> None:
    row = {"preview_id": uuid4(), "draft_id": uuid4(), "gate_status": gate_status,
           "safe_content": "UPS negotiated rate: USD 1.00", "reason_codes": ["NOT_SAFE"]}
    assert _public_preview(row) == {"gate_status": gate_status, "content": None,
                                    "reason_codes": ["NOT_SAFE"]}
    try:
        SafePreviewPublic(gate_status="indeterminate", content="unverified",
                          reason_codes=["UNVERIFIED"])
    except ValueError:
        pass
    else:
        raise AssertionError("an unverified preview exposed content")


def test_response_read_integrity_error_fails_closed(monkeypatch) -> None:
    ticket_id = uuid4()
    monkeypatch.setattr(rfp, "get_response_generation", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(ResponsePersistenceConflict("bad hash")))
    response = ASGIClient(app_with(Role.admin, FakeSession())).request(
        "GET", f"/rfps/{ticket_id}/response")
    assert response.status_code == 503
    assert "bad hash" not in response.text
