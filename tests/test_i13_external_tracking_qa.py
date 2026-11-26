"""QA-only external credential, MCP WebSocket and grant lifecycle checks."""

import asyncio
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from services.api.chat import external_tracking_qa as qa
from services.api.chat.models import chat_events
from services.api.inventory_models import CommercialOrder, SKU, StockExit
from tests.test_websocket_chat_postgres import chat_db  # noqa: F401


ISSUER = "http://127.0.0.1:18082/realms/trackflow-i13-external"


@pytest.fixture
def synthetic_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed(key, subject="business", **overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": subject, "iss": ISSUER, "aud": qa.QA_AUDIENCE, "azp": qa.QA_AUDIENCE,
        "typ": "Bearer", "trackflow_purpose": qa.QA_PURPOSE,
        "scope": "openid " + qa.QA_SCOPE, "iat": now, "exp": now + timedelta(minutes=5),
    }
    return jwt.encode({**claims, **overrides}, key, algorithm="RS256")


@pytest.mark.parametrize("change", [
    {"scope": "tracking:read inventory:read"},
    {"aud": [qa.QA_AUDIENCE, "trackflow-api"]},
    {"iss": "http://127.0.0.1:18082/realms/legacy"},
    {"azp": "manager"}, {"typ": "ID"}, {"trackflow_purpose": "legacy"},
    {"exp": 1},
])
def test_dedicated_jwt_rejects_wrong_credential(synthetic_key, change):
    token = signed(synthetic_key, **change)
    with pytest.raises(qa.ScopeDenied):
        qa._authenticate("Bearer " + token, qa.TrackingQASettings(ISSUER), lambda _: synthetic_key.public_key())


def test_dedicated_jwt_requires_matching_signature(synthetic_key):
    token = signed(synthetic_key)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(qa.ScopeDenied):
        qa._authenticate("Bearer " + token, qa.TrackingQASettings(ISSUER), lambda _: other.public_key())
    assert qa._authenticate(
        "Bearer " + token, qa.TrackingQASettings(ISSUER), lambda _: synthetic_key.public_key(),
    ) == qa.Principal(ISSUER, "business")


def seed(engine, issuer, business="business", recipient="recipient"):
    qa.external_tracking_qa_metadata.create_all(engine)
    for table in (SKU.__table__, CommercialOrder.__table__, StockExit.__table__):
        table.create(engine)
    with Session(engine) as db:
        dispatch_ids = []
        for client, number in (("client-a", "I13-OWN1"), ("client-a", "I13-SIB1"), ("client-b", "I13-OTHER1")):
            sku = SKU(name=number, sku=number, client_id=client, client_name=client, category="qa", warehouse="LA")
            order = CommercialOrder(order_id=uuid4(), client_id=client, warehouse="LA")
            db.add_all([sku, order])
            db.flush()
            dispatch_id = uuid4()
            db.add(StockExit(
                sku_id=sku.id, quantity=1, exit_type="dispatch", tracking_number=number,
                warehouse="LA", user_uuid="qa-operator", commercial_order_id=order.id, dispatch_id=dispatch_id,
            ))
            dispatch_ids.append(dispatch_id)
        db.execute(insert(qa.tracking_grants), [
            {"issuer": issuer, "subject": subject, "owner_id": kind + "-qa-owner", "kind": kind,
             "client_id": "client-a", "dispatch_id": dispatch, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
            for subject, kind, dispatch in ((business, "business", None), (recipient, "recipient", dispatch_ids[0]))
        ])
        db.commit()


def app_for(engine, issuer, key_resolver=None):
    @contextmanager
    def database():
        with Session(engine) as db:
            yield db

    return qa.create_external_tracking_qa_app(qa.TrackingQASettings(issuer), database, key_resolver=key_resolver)


def auth_headers(token):
    return {"Authorization": "Bearer " + token}


def open_chat(client, token):
    result = client.post("/chat/sessions", json={}, headers=auth_headers(token))
    assert result.status_code == 201, result.status_code
    assert result.json()["agent_id"] == "first_line_cx"
    return result.json()["session_id"]


def authenticate(socket, token, session_id):
    socket.send_json({"event": "authenticate", "data": {"session_id": session_id, "access_token": token}})


def ask(socket, session_id, tracking):
    socket.send_json({"event": "user_message", "data": {
        "session_id": session_id, "text": f"Track shipment {tracking}",
    }})


def answer(socket):
    event = socket.receive_json()
    assert event["event"] == "token_chunk"
    assert socket.receive_json()["event"] == "generation_completed"
    return event["data"]["token"]


def assert_tracking(client, business_token, recipient_token):
    # This credential and this app have no generic inventory/Support surface.
    assert client.get("/inventory/products", headers=auth_headers(recipient_token)).status_code == 404
    assert client.post("/support/query", json={}, headers=auth_headers(business_token)).status_code == 404
    assert client.post("/mcp", json={}, headers=auth_headers(business_token)).status_code == 404
    assert client.post("/chat/sessions", json={"client_id": "client-b"}, headers=auth_headers(business_token)).status_code == 422
    business_id, recipient_id = open_chat(client, business_token), open_chat(client, recipient_token)
    for token, session_id, sibling in ((business_token, business_id, qa.FOUND), (recipient_token, recipient_id, qa.DENIED)):
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket, token, session_id)
            assert socket.receive_json()["event"] == "session_snapshot"
            for code, expected in (("I13-OWN1", qa.FOUND), ("I13-SIB1", sibling), ("I13-OTHER1", qa.DENIED), ("I13-MISSING1", qa.DENIED)):
                ask(socket, session_id, code)
                assert answer(socket) == expected
    with client.websocket_connect("/chat/ws") as socket:
        authenticate(socket, recipient_token, recipient_id)
        snapshot = socket.receive_json()
        assert snapshot["event"] == "session_snapshot"
        assistant = [m["content"] for m in snapshot["data"]["messages"] if m["role"] == "assistant"]
        assert assistant == [qa.FOUND, qa.DENIED, qa.DENIED, qa.DENIED]
    with client.websocket_connect("/chat/ws") as socket:
        authenticate(socket, recipient_token, business_id)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403


def test_scoped_mcp_and_websocket_tracking(chat_db, synthetic_key):
    seed(chat_db, ISSUER)
    with TestClient(app_for(chat_db, ISSUER, lambda _: synthetic_key.public_key())) as client:
        assert_tracking(client, signed(synthetic_key), signed(synthetic_key, "recipient"))


def test_revocation_between_mcp_result_and_commit_denies_response_and_replay(chat_db, synthetic_key, monkeypatch):
    seed(chat_db, ISSUER)
    received_tool, continue_commit = Event(), Event()
    original_call = qa.ClientSession.call_tool

    async def pause_after_tool(self, *args, **kwargs):
        result = await original_call(self, *args, **kwargs)
        assert result.structuredContent == {"status": "found", "dispatch_state": "recorded_dispatch"}
        received_tool.set()
        assert await asyncio.to_thread(continue_commit.wait, 5)
        return result

    monkeypatch.setattr(qa.ClientSession, "call_tool", pause_after_tool)
    token = signed(synthetic_key, "recipient")
    with TestClient(app_for(chat_db, ISSUER, lambda _: synthetic_key.public_key())) as client:
        session_id = open_chat(client, token)
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket, token, session_id)
            assert socket.receive_json()["event"] == "session_snapshot"
            ask(socket, session_id, "I13-OWN1")
            try:
                assert received_tool.wait(5), "tracking tool did not return"
                with Session(chat_db) as db:
                    db.execute(update(qa.tracking_grants).where(
                        qa.tracking_grants.c.subject == "recipient",
                    ).values(revoked_at=datetime.now(timezone.utc)))
                    db.commit()
            finally:
                continue_commit.set()
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4403
        with Session(chat_db) as db:
            assert db.execute(select(func.count()).select_from(chat_events).where(
                chat_events.c.session_id == session_id,
            )).scalar_one() == 0
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket, token, session_id)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4403


def test_replay_checks_current_dispatch_tenant(chat_db, synthetic_key):
    seed(chat_db, ISSUER)
    token = signed(synthetic_key)
    with TestClient(app_for(chat_db, ISSUER, lambda _: synthetic_key.public_key())) as client:
        session_id = open_chat(client, token)
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket, token, session_id)
            assert socket.receive_json()["event"] == "session_snapshot"
            ask(socket, session_id, "I13-OWN1")
            assert answer(socket) == qa.FOUND
        # Direct SQL mutation of synthetic QA records represents changed eligibility.
        with Session(chat_db) as db:
            db.execute(update(CommercialOrder).where(
                CommercialOrder.id.in_(select(StockExit.commercial_order_id).where(
                    StockExit.tracking_number == "I13-OWN1",
                )),
            ).values(client_id="client-b"))
            db.commit()
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket, token, session_id)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4403


def test_live_i13_keycloak_issued_tokens(chat_db):
    variables = (
        "TRACKFLOW_I13_KEYCLOAK_ISSUER", "TRACKFLOW_I13_KEYCLOAK_B2B_TOKEN",
        "TRACKFLOW_I13_KEYCLOAK_B2C_TOKEN", "TRACKFLOW_I13_KEYCLOAK_B2B_SUB",
        "TRACKFLOW_I13_KEYCLOAK_B2C_SUB",
    )
    if not all(os.getenv(key) for key in variables):
        pytest.skip("dedicated temporary QA realm credentials are not configured")
    issuer, business_token, recipient_token, business_sub, recipient_sub = (os.environ[key] for key in variables)
    seed(chat_db, issuer, business_sub, recipient_sub)
    with TestClient(app_for(chat_db, issuer)) as client:
        assert_tracking(client, business_token, recipient_token)
