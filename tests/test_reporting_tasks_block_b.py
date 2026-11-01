from __future__ import annotations

from contextlib import contextmanager
import asyncio
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
import httpx

import services.task_queue.router as tasks_router
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.reporting.router import get_reporting_engine


class Engine:
    @contextmanager
    def connect(self):
        yield object()


def _user(role: Role) -> UserInDB:
    return UserInDB(
        id=str(uuid4()),
        email=f"{role.value}@example.com",
        hashed_password="unused",
        role=role,
    )


class ASGIClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def get(self, url: str):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as session:
                return await session.get(url)

        return asyncio.run(send())


def _client(role: Role | None) -> ASGIClient:
    app = FastAPI()
    app.include_router(tasks_router.router)
    app.dependency_overrides[get_reporting_engine] = lambda: Engine()
    if role is not None:
        app.dependency_overrides[get_current_user] = lambda: _user(role)
    return ASGIClient(app)


def _row(execution_state: str, **overrides):
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    row = {
        "task_id": uuid4(),
        "run_id": uuid4(),
        "task_name": "trackflow.run_weekly_pipeline",
        "week_start": date(2026, 10, 19),
        "celery_state": None,
        "execution_state": execution_state,
        "dispatch_status": "pending",
        "pipeline_status": None,
        "attempt_count": 0,
        "result_summary": None,
        "error_code": None,
        "error_message": None,
        "published": False,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("durable", "public"),
    [
        ("accepted", "pending"),
        ("queued", "pending"),
        ("retry_wait", "pending"),
        ("claimed", "started"),
        ("running", "started"),
        ("succeeded", "success"),
        ("failed", "failure"),
        ("dead_lettered", "failure"),
        ("reconciliation_required", "failure"),
    ],
)
def test_public_status_projection(durable, public):
    assert tasks_router.public_task_status(durable) == public


def test_get_task_uses_durable_result_when_redis_state_is_absent(monkeypatch):
    row = _row(
        "succeeded",
        celery_state=None,
        pipeline_status="blocked_quality",
        attempt_count=1,
        result_summary={"errors": ["coverage_incomplete"]},
        published=False,
    )
    monkeypatch.setattr(tasks_router, "get_async_task", lambda *_args: row)

    response = _client(Role.admin).get(f"/tasks/{row['task_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["celery_state"] is None
    assert body["pipeline_status"] == "blocked_quality"
    assert body["attempt_count"] == 1
    assert body["published"] is False


@pytest.mark.parametrize("role", [Role.manager, Role.user])
def test_get_task_forbids_non_admin(role):
    assert _client(role).get(f"/tasks/{uuid4()}").status_code == 403


def test_get_task_requires_authentication():
    assert _client(None).get(f"/tasks/{uuid4()}").status_code == 401


def test_get_task_unknown_uuid_is_404(monkeypatch):
    monkeypatch.setattr(tasks_router, "get_async_task", lambda *_args: None)
    assert _client(Role.admin).get(f"/tasks/{uuid4()}").status_code == 404


def test_commit_uncertain_preserves_nullable_publication(monkeypatch):
    row = _row(
        "reconciliation_required",
        pipeline_status="commit_uncertain",
        published=None,
        error_code="commit_uncertain",
        error_message="publication outcome requires reconciliation",
    )
    monkeypatch.setattr(tasks_router, "get_async_task", lambda *_args: row)

    body = _client(Role.admin).get(f"/tasks/{row['task_id']}").json()

    assert body["status"] == "failure"
    assert body["published"] is None
    assert body["pipeline_status"] == "commit_uncertain"
