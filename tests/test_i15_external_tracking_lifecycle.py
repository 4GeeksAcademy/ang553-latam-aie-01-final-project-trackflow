"""End-to-end isolated external MCP cancellation against QA PostgreSQL."""

import asyncio
from threading import Event

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from services.api.chat import external_tracking_qa as qa
from services.api.chat.models import chat_events
from tests.test_i13_external_tracking_qa import (
    ISSUER,
    app_for,
    ask,
    authenticate,
    chat_db,  # noqa: F401 - pytest fixture
    open_chat,
    seed,
    signed,
    synthetic_key,  # noqa: F401 - pytest fixture
)


def test_external_reconnect_interrupts_original_mcp_and_redirects_without_late_events(chat_db, synthetic_key, monkeypatch):
    seed(chat_db, ISSUER)
    entered, cancelled = Event(), Event()
    original = qa.ClientSession.call_tool

    async def hold_original(self, name, arguments):
        if arguments["tracking_number"] == "I13-OWN1":
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await original(self, name, arguments)

    monkeypatch.setattr(qa.ClientSession, "call_tool", hold_original)
    token = signed(synthetic_key)
    with TestClient(app_for(chat_db, ISSUER, lambda _: synthetic_key.public_key())) as client:
        session_id = open_chat(client, token)
        with client.websocket_connect("/chat/ws") as first:
            authenticate(first, token, session_id)
            assert first.receive_json()["event"] == "session_snapshot"
            ask(first, session_id, "I13-OWN1")
            assert entered.wait(5)
        with client.websocket_connect("/chat/ws") as second:
            authenticate(second, token, session_id)
            snapshot = second.receive_json()
            assert snapshot["data"]["messages"][-1]["status"] == "streaming"
            second.send_json({"event": "interrupt_requested", "data": {
                "session_id": session_id, "new_input": "Track shipment I13-SIB1",
            }})
            interrupted = second.receive_json()
            assert interrupted["event"] == "generation_interrupted"
            assert cancelled.is_set()
            new = second.receive_json()
            assert new["event"] == "token_chunk"
            assert new["data"]["token"] == qa.FOUND
            assert new["data"]["generation_id"] != interrupted["data"]["generation_id"]
            assert second.receive_json()["event"] == "generation_completed"
        with client.websocket_connect("/chat/ws") as replay:
            authenticate(replay, token, session_id)
            messages = replay.receive_json()["data"]["messages"]
            assert [(m["content"], m["status"]) for m in messages if m["role"] == "assistant"] == [
                ("", "interrupted"), (qa.FOUND, "completed"),
            ]
    with Session(chat_db) as db:
        assert db.execute(select(chat_events.c.event_type).where(
            chat_events.c.session_id == session_id,
        ).order_by(chat_events.c.cursor)).scalars().all() == [
            "generation_interrupted", "token_chunk", "generation_completed",
        ]


def test_external_replay_rejects_unapproved_stored_assistant_content(chat_db, synthetic_key):
    seed(chat_db, ISSUER)
    token = signed(synthetic_key)
    with TestClient(app_for(chat_db, ISSUER, lambda _: synthetic_key.public_key())) as client:
        session_id = open_chat(client, token)
        with Session(chat_db) as db:
            access, _ = qa._resolve_grant(db, qa.Principal(ISSUER, "business"), session_id)
            turn = qa.begin_generation(db, access, session_id, "Track shipment I13-OWN1")
            qa.append_chunk(db, turn, "Unexpected legacy assistant body fixture")
            qa.complete_generation(db, turn)
        with client.websocket_connect("/chat/ws") as replay:
            authenticate(replay, token, session_id)
            with pytest.raises(WebSocketDisconnect) as closed:
                replay.receive_json()
            assert closed.value.code == 4403
