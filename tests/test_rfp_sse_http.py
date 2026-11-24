"""Opt-in F2 wire-level tests via loopback Uvicorn and disposable PostgreSQL."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
from threading import Condition, Thread
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlmodel import Session
import uvicorn

from services.api import database
from services.api.auth_models import Role
from services.api.auth_security import create_access_token
from services.api.main import app
from services.api.routes import rfp_notifications
from services.rfp_approval.models import rfp_identity_bindings, rfp_principals
from services.rfp_intake.intake_events import record_intake_decision, rfp_sse_events
from services.rfp_intake.models import rfp_tickets
from services.rfp_intake.notifications import purge_event_prefix, rfp_sse_commercial_grants
from tests.test_rfp_sse_postgres import _claim, _decision, sse_db


@contextmanager
def _server():
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started and thread.is_alive()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(6)
            assert not thread.is_alive(), "Uvicorn did not shut down cleanly"


@pytest.fixture
def sessions(sse_db, isolated_auth_db, monkeypatch):
    migrations = Path(__file__).resolve().parents[1] / "infra/sql"
    with sse_db.connect().execution_options(isolation_level="AUTOCOMMIT", no_parameters=True) as conn:
        assert conn.execute(text("SELECT current_schema()")).scalar_one().startswith("rfp_sse_test_")
        for name in ("rfp_response_generation.sql", "rfp_approval.sql", "rfp_sse_commercial.sql"):
            conn.exec_driver_sql((migrations / name).read_text(encoding="utf-8"))
    monkeypatch.setattr(database, "_engine", sse_db)
    monkeypatch.setattr(rfp_notifications, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(rfp_notifications, "_HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setenv("TRACKFLOW_ENVIRONMENT", "qa")

    def add_user(role: Role, *, bound: bool = False, granted: bool = False):
        sub = str(uuid4())
        isolated_auth_db.users.insert({
            "id": sub, "email": f"{sub}@example.test",
            "hashed_password": "not-used-in-this-test", "is_active": True, "role": role.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        principal = uuid4()
        if bound:
            with Session(sse_db) as session:
                session.execute(insert(rfp_principals).values(
                    principal_id=principal, environment="qa", trackflow_user_id=sub, synthetic=True,
                ))
                session.execute(insert(rfp_identity_bindings).values(
                    binding_id=uuid4(), principal_id=principal, environment="qa", auth_source="local",
                    issuer="urn:trackflow:qa:local", subject=sub,
                ))
                if granted:
                    session.execute(insert(rfp_sse_commercial_grants).values(
                        principal_id=principal, environment="qa",
                    ))
                session.commit()
        return sub, principal

    admin, _ = add_user(Role.admin)
    commercial, principal = add_user(Role.user, bound=True, granted=True)
    ungranted, _ = add_user(Role.manager, bound=True)
    return admin, commercial, ungranted, principal


def _headers(subject, after=None, *, expiry=None):
    result = {"Authorization": f"Bearer {create_access_token(subject, expiry)}"}
    if after is not None:
        result["Last-Event-ID"] = str(after)
    return result


def _publish(db):
    job = _claim(db)
    with Session(db) as session:
        record_intake_decision(session, job, _decision(job))
        session.commit()
    return job


def _frame(lines):
    frame = []
    for line in lines:
        if not line and frame:
            return frame
        if line:
            frame.append(line)
    pytest.fail("SSE ended before delivering a full frame")


def _assert_event(frame, cursor, ticket_id):
    assert frame[:2] == [f"id: {cursor}", "event: rfp_ticket_created"]
    assert len(frame) == 3
    payload = json.loads(frame[2].removeprefix("data: "))
    assert payload["ticket_id"] == str(ticket_id)
    assert payload["status"] == "analyzing"
    assert payload["services_requested"] == ["warehouse", "reverse"]
    assert payload["created_at"].endswith("Z")
    assert set(payload) == {
        "ticket_id", "rfp_id", "client_name", "client_country",
        "services_requested", "status", "created_at",
    }


def _assert_pool_idle_between_polls(db):
    condition = Condition()
    checkouts = 0

    def checkout(*_args):
        nonlocal checkouts
        with condition:
            checkouts += 1
            condition.notify_all()

    event.listen(db.pool, "checkout", checkout)
    try:
        deadline = time.monotonic() + 3
        with condition:
            while time.monotonic() < deadline:
                if db.pool.checkedout() == 0:
                    previous = checkouts
                    if not condition.wait_for(lambda: checkouts != previous, timeout=0.05):
                        return
                condition.wait(0.01)
        pytest.fail("Both connected SSE clients never released SQL sessions between polls")
    finally:
        event.remove(db.pool, "checkout", checkout)


def test_snapshot_race_two_clients_reconnect_restart_and_resource_release(sse_db, sessions, monkeypatch):
    monkeypatch.setattr(rfp_notifications, "_POLL_SECONDS", 0.3)
    admin, commercial, _, _ = sessions
    with _server() as client:
        snapshot = client.get("/rfps/notifications/snapshot", headers=_headers(commercial)).json()
        assert snapshot == {"items": [], "total": 0, "cursor": "0"}
        first = _publish(sse_db)
        with (client.stream("GET", "/rfps/notifications/stream", headers=_headers(admin, "0")) as one,
              client.stream("GET", "/rfps/notifications/stream", headers=_headers(commercial, "0")) as two):
            for response in (one, two):
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "no-store" in response.headers["cache-control"]
                assert response.headers["x-accel-buffering"] == "no"
            first_client = one.iter_lines()
            second_client = two.iter_lines()
            for lines in (first_client, second_client):
                assert _frame(lines) == [": keep-alive"]
                _assert_event(_frame(lines), 1, first["ticket_id"])
            # An idle stream must neither reserve a SQL session nor block new writes.
            assert _frame(first_client) == [": keep-alive"]
            _assert_pool_idle_between_polls(sse_db)
            second = _publish(sse_db)
        # Exit closes both sockets, then starts a new Uvicorn server on the same DB.
    assert sse_db.pool.checkedout() == 0
    with _server() as client:
        with client.stream("GET", "/rfps/notifications/stream", headers=_headers(commercial, "1")) as resumed:
            lines = resumed.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            _assert_event(_frame(lines), 2, second["ticket_id"])
        snapshot = client.get("/rfps/notifications/snapshot", headers=_headers(commercial)).json()
        assert snapshot["cursor"] == "2" and snapshot["total"] == 2
    assert sse_db.pool.checkedout() == 0


def test_access_scope_token_and_least_privilege(sse_db, sessions):
    admin, commercial, ungranted, _ = sessions
    waiting = _claim(sse_db)
    valid = _publish(sse_db)
    with Session(sse_db) as session:
        session.execute(update(rfp_tickets).where(rfp_tickets.c.ticket_id == waiting["ticket_id"])
                        .values(status="descartado", discard_reason_code="not_rfp"))
        session.commit()
    with _server() as client:
        for path in ("/rfps/notifications/snapshot", f"/rfps/notifications/{valid['ticket_id']}",
                     "/rfps/notifications/stream"):
            for headers in ({}, {"Authorization": "Bearer corrupt"},
                            _headers(commercial, expiry=timedelta(seconds=-10))):
                assert client.get(path, headers=headers).status_code == 401
            assert client.get(path, headers=_headers(ungranted, "0")).status_code == 403
        headers = _headers(commercial)
        row = client.get(f"/rfps/notifications/{valid['ticket_id']}", headers=headers).json()
        assert row["status"] == "analizando" and row["client_name"] == "ModaViva"
        assert set(row) == {
            "ticket_id", "rfp_id", "client_name", "client_country", "services_requested",
            "status", "job_status", "created_at", "updated_at",
        }
        assert client.get(f"/rfps/notifications/{waiting['ticket_id']}", headers=headers).status_code == 404
        assert client.get("/rfps/notifications/snapshot", headers=headers).json()["items"] == [row]
        assert client.get(f"/rfps/{valid['ticket_id']}", headers=headers).status_code == 403
        assert client.get("/rfps", headers=headers).status_code == 403
        assert client.post("/rfps", headers=headers, files={"file": ("qa.pdf", b"Not opened")}).status_code == 403
        assert client.post(f"/rfps/{valid['ticket_id']}/response", headers=headers).status_code == 403
        assert client.get(f"/rfps/{valid['ticket_id']}", headers=_headers(admin)).status_code == 200


def test_running_stream_stops_on_expiry_revocation_or_deactivated_user(sse_db, sessions, isolated_auth_db):
    admin, commercial, _, principal = sessions
    with _server() as client:
        with client.stream("GET", "/rfps/notifications/stream",
                           headers=_headers(admin, "0", expiry=timedelta(seconds=3))) as response:
            lines = response.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            assert all(line in {"", ": keep-alive"} for line in lines)
        assert client.get("/rfps/notifications/snapshot", headers=_headers(
            admin, expiry=timedelta(seconds=-1))).status_code == 401
        with client.stream("GET", "/rfps/notifications/stream", headers=_headers(commercial, "0")) as response:
            lines = response.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            with Session(sse_db) as session:
                session.execute(update(rfp_sse_commercial_grants)
                    .where(rfp_sse_commercial_grants.c.principal_id == principal)
                    .values(revoked_at=func.clock_timestamp()))
                session.commit()
            assert all(line in {"", ": keep-alive"} for line in lines)
        assert client.get("/rfps/notifications/snapshot", headers=_headers(commercial)).status_code == 403
        from tinydb import Query
        with client.stream("GET", "/rfps/notifications/stream", headers=_headers(admin, "0")) as response:
            lines = response.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            isolated_auth_db.users.update({"is_active": False}, Query().id == admin)
            assert all(line in {"", ": keep-alive"} for line in lines)
        assert client.get("/rfps/notifications/snapshot", headers=_headers(admin)).status_code == 401


def test_retention_prefix_and_invalid_expired_or_future_cursors(sse_db, sessions):
    _, commercial, _, _ = sessions
    for _ in range(3):
        _publish(sse_db)
    with Session(sse_db) as session:
        session.execute(update(rfp_sse_events).where(rfp_sse_events.c.cursor.in_([1, 3]))
                        .values(recorded_at=func.clock_timestamp() - text("interval '8 days'")))
        session.commit()
        assert purge_event_prefix(session, days=7, limit=10) == 1
        session.commit()
        assert session.execute(select(rfp_sse_events.c.cursor).order_by(rfp_sse_events.c.cursor)
                               ).scalars().all() == [2, 3]
    with _server() as client:
        for cursor in ("0", "4", "01", "-1", "9223372036854775808", None):
            response = client.get("/rfps/notifications/stream", headers=_headers(commercial, cursor))
            assert response.status_code == 409 and response.json()["detail"]["code"] == "rfp_cursor_reset"
        snapshot = client.get("/rfps/notifications/snapshot", headers=_headers(commercial)).json()
        assert snapshot["cursor"] == "3" and snapshot["total"] == 3


def test_transient_auth_storage_failure_closes_stream_and_replay_recovers(
    sse_db, sessions, isolated_auth_db, monkeypatch,
):
    from threading import Event

    _, commercial, ungranted, _ = sessions
    storage = isolated_auth_db.auth_db.storage
    real_read = storage.read
    outage = Event()

    def read():
        if outage.is_set():
            raise json.JSONDecodeError("private test marker", "private test auth-file content", 0)
        return real_read()

    monkeypatch.setattr(storage, "read", read)
    with _server() as client:
        snapshot_path = "/rfps/notifications/snapshot"
        stream_path = "/rfps/notifications/stream"
        headers = _headers(commercial, "0")
        with client.stream("GET", stream_path, headers=headers) as opened:
            lines = opened.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            outage.set()
            assert all(line in {"", ": keep-alive"} for line in lines)
        for path in (snapshot_path, stream_path, "/auth/me"):
            response = client.get(path, headers=headers)
            assert response.status_code == 503
            assert response.json() == {"detail": "Authentication storage is temporarily unavailable"}
            assert "WWW-Authenticate" not in response.headers
        outage.clear()
        ticket = _publish(sse_db)
        assert client.get(snapshot_path, headers=_headers(ungranted)).status_code == 403
        assert client.get(stream_path, headers=_headers(ungranted, "0")).status_code == 403
        assert client.get(stream_path, headers=_headers(commercial, "999")).status_code == 409
        with client.stream("GET", stream_path, headers=headers) as resumed:
            assert resumed.status_code == 200
            lines = resumed.iter_lines()
            assert _frame(lines) == [": keep-alive"]
            _assert_event(_frame(lines), 1, ticket["ticket_id"])
