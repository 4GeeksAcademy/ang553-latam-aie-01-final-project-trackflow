"""Opt-in PostgreSQL integration for durable WebSocket chat state."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
from threading import Event
from uuid import uuid4
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from pipelines.database_identity import sqlalchemy_postgresql_url
from services.api import auth_security
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.chat.models import chat_messages
from services.api.chat.store import (
    ChatNotFound,
    GenerationConflict,
    append_chunk,
    begin_generation,
    chat_snapshot,
    chat_support_context,
    complete_generation,
    create_chat_session,
    get_chat_session,
    fail_generation,
    interrupt_generation,
    reconcile_orphaned_generation,
)


@pytest.fixture
def chat_db():
    raw_url = os.getenv("TRACKFLOW_CHAT_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TRACKFLOW_CHAT_TEST_DATABASE_URL is not configured")
    if os.getenv("TRACKFLOW_CHAT_TEST_ALLOW_DDL") != "1":
        pytest.skip("TRACKFLOW_CHAT_TEST_ALLOW_DDL=1 is required for isolated DDL")
    if make_url(raw_url).get_backend_name() != "postgresql":
        pytest.fail("TRACKFLOW_CHAT_TEST_DATABASE_URL must target PostgreSQL")
    schema = "chat_test_" + uuid4().hex
    assert re.fullmatch(r"chat_test_[0-9a-f]{32}", schema)
    administrative = create_engine(sqlalchemy_postgresql_url(raw_url))
    with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        sqlalchemy_postgresql_url(raw_url),
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        migration = (
            Path(__file__).resolve().parents[1] / "infra/sql/chat_websocket.sql"
        ).read_text(encoding="utf-8")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
            connection.exec_driver_sql(
                "CREATE TABLE trackflow_client ("
                "client_id varchar(36) PRIMARY KEY, display_name text NOT NULL, "
                "is_active boolean NOT NULL DEFAULT true)"
            )
            connection.exec_driver_sql(
                "INSERT INTO trackflow_client(client_id, display_name, is_active) "
                "VALUES ('client-a','Client A',true),('client-b','Client B',true)"
            )
            connection.exec_driver_sql(migration)
        yield engine
    finally:
        engine.dispose()
        with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        administrative.dispose()


def test_postgres_history_interrupt_completion_and_isolation(chat_db):
    owner = AccessContext(
        "user-a", Role.manager, frozenset({"client-a"}), frozenset()
    )
    other_user = AccessContext(
        "user-b", Role.manager, frozenset({"client-a"}), frozenset()
    )
    other_client = AccessContext(
        "user-a", Role.manager, frozenset({"client-b"}), frozenset()
    )
    with Session(chat_db) as db:
        created = create_chat_session(db, owner, client_id="client-a")
        assert UUID(created.session_id).version == 4
        first = begin_generation(db, owner, created.session_id, text="primera")
        with pytest.raises(GenerationConflict):
            begin_generation(db, owner, created.session_id, text="duplicada")
        one = append_chunk(db, first, "par")
        two = append_chunk(db, first, "cial")
        interrupted = interrupt_generation(db, first)
        assert (one["sequence"], two["sequence"]) == (1, 2)
        assert (one["cursor"], two["cursor"], interrupted["cursor"]) == (1, 2, 3)
        snapshot = chat_snapshot(db, owner, created.session_id)
        assert snapshot.cursor == 3
        assert [message.content for message in snapshot.messages] == ["primera", "parcial"]
        assert snapshot.messages[-1].status == "interrupted"

        second = begin_generation(db, owner, created.session_id, text="segunda")
        chunk = append_chunk(db, second, "completa")
        completed = complete_generation(db, second)
        assert (chunk["sequence"], chunk["cursor"], completed["cursor"]) == (1, 4, 5)
        assert chat_snapshot(db, owner, created.session_id).messages[-1].status == "completed"

        failed = begin_generation(db, owner, created.session_id, text="turno fallido")
        fail_generation(db, failed)
        bounded = chat_support_context(db, owner, created.session_id)
        assert bounded.client_id == "client-a"
        assert bounded.history_json is not None
        assert '"trust":"untrusted_data_only"' in bounded.history_json
        assert "completa" in bounded.history_json
        assert '"content":"parcial"' not in bounded.history_json
        assert "turno fallido" not in bounded.history_json
        for denied in (other_user, other_client):
            with pytest.raises(ChatNotFound):
                chat_support_context(db, denied, created.session_id)

        orphan = begin_generation(db, owner, created.session_id, text="antes de reinicio")
        orphan_chunk = append_chunk(db, orphan, "parcial persistido")
        reconciled = reconcile_orphaned_generation(db, owner, created.session_id)
        assert reconciled is not None
        assert reconciled["cursor"] == orphan_chunk["cursor"] + 1
        assert reconcile_orphaned_generation(db, owner, created.session_id) is None
        recovered = chat_snapshot(db, owner, created.session_id)
        assert recovered.messages[-1].content == "parcial persistido"
        assert recovered.messages[-1].status == "interrupted"
        after_restart = begin_generation(db, owner, created.session_id, text="turno nuevo")
        append_chunk(db, after_restart, "respuesta nueva")
        complete_generation(db, after_restart)
        assert chat_snapshot(db, owner, created.session_id).messages[-1].status == "completed"

        # Historical messages may predate the current guard. Keep the previous
        # content in storage without returning the ambiguous output in a snapshot.
        original = "Legacy proposal followed by INSUFFICIENT_CONTEXT"
        db.execute(chat_messages.update().where(
            chat_messages.c.message_id == after_restart.assistant_message_id,
        ).values(content=original))
        db.commit()
        history = chat_snapshot(db, owner, created.session_id).messages[-1]
        assert history.message_id == after_restart.assistant_message_id
        assert history.status == "completed"
        assert history.answer_kind == "insufficient_context"
        assert "INSUFFICIENT_CONTEXT" not in history.content
        assert db.execute(chat_messages.select().where(
            chat_messages.c.message_id == after_restart.assistant_message_id,
        )).mappings().one().content == original

        for denied in (other_user, other_client):
            with pytest.raises(ChatNotFound):
                get_chat_session(db, denied, created.session_id)


def test_ws_qa_units_commit_abort_reconnect_and_revalidate(chat_db, monkeypatch):
    from services.api.agent_memory import models as memory
    from services.api import support_oidc
    from services.api.auth_security import create_access_token
    from services.api.chat import coordinator, producer, qa_units, router
    from services.api.chat.contracts import TokenChunkEvent
    from services.api.chat.models import chat_events
    from services.api.chat.pubsub import SessionPubSub
    from services.incidents.models import user_client_memberships
    from services.rag.config import DEFAULT_BASE_URL, RAGSettings
    from tests.test_i13_qa_units import CLIENT, USER, FixtureStream, _fixture_env, _identity

    with Session(chat_db) as db:
        if tuple(db.execute(text("SELECT current_database(), current_user")).one()) != (
            "trackflow_ws_qa", "trackflow_ws_qa",
        ):
            pytest.skip("I13 requires a disposable database and user named trackflow_ws_qa")

    _fixture_env(monkeypatch)
    actor = _identity()
    user_client_memberships.create(chat_db)
    memory.operational_security_metadata.create_all(chat_db, tables=[
        memory.memory_conversations, memory.memory_proposals,
        memory.memory_decisions, memory.memory_entries,
    ])
    with Session(chat_db) as db:
        db.execute(text(
            "INSERT INTO trackflow_client(client_id, display_name) VALUES (:client, 'QA I13')"
        ), {"client": CLIENT})
        db.execute(user_client_memberships.insert().values(
            user_id=USER, client_id=CLIENT, assigned_by_user_id="qa-admin",
        ))
        db.commit()

    provider_waiting = Event()
    provider_closed = Event()
    provider_cancelled = Event()
    oidc_live = Event()
    oidc_live.set()

    class ControlledSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            for delta in ("U", "1\nU"):
                payload = {"choices": [{"delta": {"content": delta}}]}
                yield b"data: " + json.dumps(payload).encode() + b"\n\n"
            provider_waiting.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise

        async def aclose(self):
            provider_closed.set()

    published = []

    class ObservedPubSub(SessionPubSub):
        async def publish(self, session_id, event):
            if isinstance(event, TokenChunkEvent):
                with Session(chat_db) as db:
                    row = db.execute(chat_events.select().where(
                        chat_events.c.session_id == session_id,
                        chat_events.c.cursor == event.data.cursor,
                    )).mappings().one()
                    assert row.payload == event.data.model_dump(mode="json", exclude_none=True)
                published.append(event.data)
            return await super().publish(session_id, event)

    def respond(request):
        assert qa_units.QA_UNITS_QUESTION not in request.content.decode()
        if provider_cancelled.is_set():
            stream = FixtureStream(["U1\n", "U2\n", "U3", ("", "stop")], terminal=False)
        else:
            stream = ControlledSSE()
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream,
        )

    def gateway(settings):
        return original_gateway(
            settings, async_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
            ),
        )

    oidc_checks = []

    def verify(credentials):
        assert credentials.credentials == actor._token.get_secret_value()
        oidc_checks.append(True)
        if not oidc_live.is_set():
            raise HTTPException(status_code=401)
        return actor

    def open_session():
        return Session(chat_db)

    def db_dependency():
        with open_session() as db:
            yield db

    make_producer = producer.authentic_support_producer

    def producer_for(identity, admission=None):
        actual = make_producer(identity, admission)

        async def run(session_id, question):
            if question == "turno nuevo":
                assert provider_closed.is_set() and provider_cancelled.is_set()
                yield "respuesta validada de redirección"
            else:
                async for value in actual.run(session_id, question, None):
                    yield value

        return run

    original_gateway = qa_units._QaUnitGateway
    monkeypatch.setattr(qa_units, "_QaUnitGateway", gateway)
    monkeypatch.setattr(qa_units, "load_settings", lambda: RAGSettings(
        DEFAULT_BASE_URL, "fixture", "unused", "fixture", "unused",
    ))
    for module in (coordinator, producer, qa_units, router):
        monkeypatch.setattr(module, "open_db_session", open_session)
    for module in (support_oidc, router):
        monkeypatch.setattr(module, "authenticate_support_identity", verify)
    monkeypatch.setattr(router, "authentic_support_producer", producer_for)
    monkeypatch.setattr(router, "_PUBSUB", ObservedPubSub())
    monkeypatch.setattr(auth_security, "_find_user_by_id", lambda user_id: actor.user if user_id == USER else None)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_current_user] = lambda: actor.user
    app.dependency_overrides[router.get_db] = db_dependency

    with TestClient(app) as client:
        response = client.post("/chat/sessions", json={"client_id": CLIENT})
        assert response.status_code == 201
        session_id = response.json()["session_id"]

        def send(socket, event, **data):
            socket.send_json({"event": event, "data": {"session_id": session_id, **data}})

        def authenticate(socket, *, oidc=True):
            support = {"support_token": actor._token.get_secret_value()} if oidc else {}
            send(socket, "authenticate", access_token=create_access_token(USER), **support)
            return socket.receive_json()["data"]

        with client.websocket_connect("/chat/ws") as socket, client.websocket_connect("/chat/ws") as legacy:
            assert authenticate(socket)["messages"] == authenticate(legacy, oidc=False)["messages"] == []
            send(socket, "user_message", text=qa_units.QA_UNITS_QUESTION)
            first = socket.receive_json()["data"]
            assert first["publication_kind"] == "authorized_unit"
            assert first["token"] == qa_units._PLAN[0].render
            with pytest.raises(WebSocketDisconnect) as denied:
                legacy.receive_json()
            assert denied.value.code == 4401
            assert provider_waiting.wait(3)
            assert not provider_closed.is_set()
            send(socket, "interrupt_requested", new_input="turno nuevo")
            stopped = socket.receive_json()
            assert stopped["event"] == "generation_interrupted"
            assert stopped["data"]["message_id"] == first["message_id"]
            assert stopped["data"]["generation_id"] == first["generation_id"]
            redirected = socket.receive_json()["data"]
            assert redirected["generation_id"] != first["generation_id"]
            assert "publication_kind" not in redirected
            assert socket.receive_json()["event"] == "generation_completed"
            assert provider_closed.is_set() and provider_cancelled.is_set()

        def recovered(snapshot):
            return next(message for message in snapshot["messages"] if message["message_id"] == first["message_id"])

        with client.websocket_connect("/chat/ws") as reconnected:
            partial = recovered(authenticate(reconnected))
            assert partial["publication_kind"] == "authorized_unit"
            assert partial["status"] == "interrupted"
            assert partial["content"] == first["token"]
            send(reconnected, "user_message", text=qa_units.QA_UNITS_QUESTION)
            completed_units = []
            for unit in qa_units._PLAN:
                event = reconnected.receive_json()
                assert event["event"] == "token_chunk"
                data = event["data"]
                assert data["publication_kind"] == "authorized_unit"
                assert data["token"] == ("" if not completed_units else "\n\n") + unit.render
                completed_units.append(data)
            terminal_event = reconnected.receive_json()
            assert terminal_event["event"] == "generation_completed"
            assert terminal_event["data"]["generation_id"] != first["generation_id"]
            assert all(row["generation_id"] == terminal_event["data"]["generation_id"] for row in completed_units)
        with client.websocket_connect("/chat/ws") as reconnected:
            completed = next(
                message for message in authenticate(reconnected)["messages"]
                if message["message_id"] == terminal_event["data"]["message_id"]
            )
            assert completed["status"] == "completed"
            assert completed["content"] == "".join(row["token"] for row in completed_units)
        with client.websocket_connect("/chat/ws") as legacy:
            assert recovered(authenticate(legacy, oidc=False))["content"] == ""
        assert recovered(client.get(f"/chat/sessions/{session_id}").json()["data"])["content"] == ""
        oidc_checks_before_revocation = len(oidc_checks)
        oidc_live.clear()
        with client.websocket_connect("/chat/ws") as revoked:
            with pytest.raises(WebSocketDisconnect) as denied:
                authenticate(revoked)
            assert denied.value.code == 4403

    assert len(oidc_checks) > oidc_checks_before_revocation
    assert [data.token for data in published] == [
        first["token"], "respuesta validada de redirección", *(row["token"] for row in completed_units),
    ]
    with Session(chat_db) as db:
        for table in (memory.memory_proposals, memory.memory_entries, memory.memory_decisions):
            assert db.execute(table.select()).first() is None


def test_websocket_knowledge_replays_only_to_current_authorized_owner(chat_db, monkeypatch):
    from services.api.auth_security import create_access_token
    from services.api.chat import coordinator, router
    from services.api.chat.contracts import ChatKnowledge
    from services.api.chat.coordinator import AnnotatedChunk
    from services.api.chat.models import chat_events
    from services.api.chat.pubsub import SessionPubSub
    from services.incidents.models import user_client_memberships
    from tests.test_support_memory_api import identity

    owner = identity("manager-qa", Role.manager)
    outsider = identity("outsider-qa", Role.manager)
    identities = {actor.user.id: actor for actor in (owner, outsider)}
    user_client_memberships.create(chat_db)
    with Session(chat_db) as db:
        db.execute(user_client_memberships.insert(), [
            {
                "user_id": actor.user.id, "client_id": "client-a",
                "can_view_sensitive": False, "assigned_by_user_id": "qa-admin",
            }
            for actor in (owner, outsider)
        ])
        session_id = create_chat_session(
            db, router.build_access_context(db, owner.user), client_id="client-a",
        ).session_id
        db.commit()

    source = {
        "source_document": "returns-policy",
        "section": "standard-return-process",
        "retrieval_kind": "primary",
    }
    evidence_metadata = {"sources": [source]}
    with pytest.raises(ValidationError):
        ChatKnowledge.model_validate({"coverage": "partial", "sources": [{**source, "section": "invented"}]})
    with pytest.raises(ValidationError):
        ChatKnowledge.model_validate({"coverage": "insufficient", "sources": [source]})
    with pytest.raises(ValidationError):
        ChatKnowledge.model_validate({"coverage": "complete", "sources": [source, source]})
    complementary = {**source, "retrieval_kind": "complementary"}
    for coverage_level in (None, "complete", "partial"):
        with pytest.raises(ValidationError):
            ChatKnowledge.model_validate({"coverage": coverage_level, "sources": [complementary]})
    with pytest.raises(ValidationError):
        ChatKnowledge.model_validate({"sources": []})
    knowledge = ChatKnowledge.model_validate(evidence_metadata)

    live_oidc = Event()
    live_oidc.set()
    waiting_to_publish = Event()
    allow_publish = Event()

    def verify_test_oidc(credentials):
        if not live_oidc.is_set():
            raise HTTPException(401)
        return identities[credentials.credentials]

    def fake_producer(_identity, _admission=None):
        async def produce(_session_id, question):
            if question == "revocation in progress":
                waiting_to_publish.set()
                assert await asyncio.to_thread(allow_publish.wait, 3)
            yield AnnotatedChunk("Public policy evidence", knowledge=knowledge)
        return produce

    def open_session():
        return Session(chat_db)

    def db_dependency():
        with open_session() as db:
            yield db

    monkeypatch.setattr(coordinator, "open_db_session", open_session)
    monkeypatch.setattr(router, "open_db_session", open_session)
    monkeypatch.setattr(router, "_PUBSUB", SessionPubSub(queue_size=16))
    monkeypatch.setattr(auth_security, "_find_user_by_id", lambda user_id: identities[user_id].user)
    monkeypatch.setattr(router, "authenticate_support_identity", verify_test_oidc)
    monkeypatch.setattr(router, "authentic_support_producer", fake_producer)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_current_user] = lambda: owner.user
    app.dependency_overrides[router.get_db] = db_dependency

    def send(socket, event, **data):
        socket.send_json({"event": event, "data": {"session_id": session_id, **data}})

    def authenticate(socket, actor=owner, *, oidc=True):
        token = {"support_token": actor.user.id} if oidc else {}
        send(socket, "authenticate", access_token=create_access_token(actor.user.id), **token)
        return socket.receive_json()

    with TestClient(app) as client:
        with client.websocket_connect("/chat/ws") as socket, client.websocket_connect("/chat/ws") as passive:
            assert authenticate(socket)["data"]["messages"] == []
            assert authenticate(passive, oidc=False)["data"]["messages"] == []
            send(socket, "user_message", text="qualified policy request")
            chunk = socket.receive_json()
            passive_chunk = passive.receive_json()
            assert chunk["event"] == passive_chunk["event"] == "token_chunk"
            assert chunk["data"]["knowledge"] == evidence_metadata
            assert "knowledge" not in passive_chunk["data"]
            completed = socket.receive_json()
            assert completed["event"] == "generation_completed"
            assert passive.receive_json() == completed
            message_id = chunk["data"]["message_id"]
            assert completed["data"]["message_id"] == message_id

            with client.websocket_connect("/chat/ws") as reconnected:
                snapshot = authenticate(reconnected)["data"]
                assert snapshot["cursor"] == completed["data"]["cursor"]
                by_id = {message["message_id"]: message for message in snapshot["messages"]}
                assert len(by_id) == len(snapshot["messages"]) == 2
                assert by_id[message_id]["knowledge"] == evidence_metadata
            with client.websocket_connect("/chat/ws") as same_owner_without_oidc:
                snapshot = authenticate(same_owner_without_oidc, oidc=False)["data"]
                assert all("knowledge" not in message for message in snapshot["messages"])
            http_history = client.get(f"/chat/sessions/{session_id}")
            assert http_history.status_code == 200
            assert all("knowledge" not in message for message in http_history.json()["data"]["messages"])

            send(socket, "user_message", text="revocation in progress")
            assert waiting_to_publish.wait(3)
            live_oidc.clear()
            allow_publish.set()
            post_revocation = socket.receive_json()
            assert post_revocation["event"] == "token_chunk"
            assert "knowledge" not in post_revocation["data"]
            assert socket.receive_json()["event"] == "generation_completed"
        live_oidc.set()
        with client.websocket_connect("/chat/ws") as socket:
            with pytest.raises(WebSocketDisconnect) as denied:
                authenticate(socket, actor=outsider)
            assert denied.value.code == 4404

        with Session(chat_db) as db:
            persisted = db.execute(chat_events.select().where(
                chat_events.c.payload["message_id"].astext == message_id,
                chat_events.c.event_type == "token_chunk",
            )).mappings().one()
            assert persisted.payload["knowledge"] == evidence_metadata
            legacy_metadata = {"coverage": "partial", **evidence_metadata}
            db.execute(chat_events.update().where(
                chat_events.c.session_id == session_id, chat_events.c.cursor == persisted.cursor,
            ).values(payload={**persisted.payload, "knowledge": legacy_metadata}))
            db.commit()
        with client.websocket_connect("/chat/ws") as socket:
            snapshot = authenticate(socket)["data"]
            first_answer = next(message for message in snapshot["messages"] if message["message_id"] == message_id)
            assert first_answer["knowledge"] == legacy_metadata
        for invalid_source in ({**source, "section": "invented"}, complementary):
            with Session(chat_db) as db:
                db.execute(chat_events.update().where(
                    chat_events.c.session_id == session_id, chat_events.c.cursor == persisted.cursor,
                ).values(payload={
                    **persisted.payload, "knowledge": {"sources": [invalid_source]},
                }))
                db.commit()
            with client.websocket_connect("/chat/ws") as socket:
                snapshot = authenticate(socket)["data"]
                first_answer = next(message for message in snapshot["messages"] if message["message_id"] == message_id)
                assert "knowledge" not in first_answer
                assert first_answer["content"] == "Public policy evidence"

        with Session(chat_db) as db:
            db.execute(user_client_memberships.update().where(
                user_client_memberships.c.user_id == owner.user.id,
            ).values(client_id="client-b"))
            db.commit()
        with client.websocket_connect("/chat/ws") as socket:
            with pytest.raises(WebSocketDisconnect) as denied:
                authenticate(socket)
            assert denied.value.code == 4404


def test_websocket_agent_abort_and_reconnect_with_postgres_and_controlled_sse(
    chat_db, monkeypatch,
):
    from agents.customer_support.external_graph import ExternalSupportAgent
    from agents.customer_support.output_guardrails import SAFE_OUTPUT_MESSAGE
    from services.api.agent_memory import models as memory
    from services.api.auth_security import create_access_token
    from services.api.chat import coordinator, producer, router
    from services.api.chat.pubsub import SessionPubSub
    from services.api.routes import support
    from services.incidents.models import user_client_memberships
    from services.rag.clients import FourGeeksHTTPClient
    from services.rag.chunking import load_knowledge_chunks
    from services.rag.config import RAGSettings
    from services.rag.contracts import RetrievalResult
    from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService
    from tests.pipelines.test_customer_support_external_tools import StubRAGService
    from tests.test_support_memory_api import MCPTools, identity

    support_identity = identity("manager-qa", Role.manager)
    user = support_identity.user
    user_client_memberships.create(chat_db)
    memory.operational_security_metadata.create_all(chat_db, tables=[
        memory.memory_conversations, memory.memory_proposals,
        memory.memory_decisions, memory.memory_entries,
    ])
    with Session(chat_db) as db:
        db.execute(user_client_memberships.insert().values(
            user_id=user.id, client_id="client-a", can_view_sensitive=False,
            assigned_by_user_id="qa-admin",
        ))
        db.commit()

    await_abort = Event()
    stream_closed = Event()
    requests_seen = []
    retrieval_queries = []
    anchor_question = "¿Cuál es el plazo estándar de devolución según la política de TrackFlow?"
    slow_question = "Explain the returns policy in detail."
    redirect_question = "¿En qué circunstancias puede variar ese plazo?"
    sensitive_question = "Can you summarize the returns policy?"
    abstention_question = "Which carriers cover Zaragoza?"
    parity_question = (
        "Explica detalladamente el proceso estándar de devoluciones y el tratamiento de "
        "devoluciones internacionales según las políticas de TrackFlow."
    )
    parity_passages = tuple(
        chunk for chunk in load_knowledge_chunks()
        if chunk.source_document == "returns-policy"
        and chunk.section in {"standard-return-process", "international-returns"}
    )
    parity_requests = []
    natural_answer = (
        "Para una devolución estándar, el cliente inicia la solicitud en el portal de la marca. "
        "TrackFlow genera la etiqueta si el motivo cumple las reglas de la marca. Al recibir el "
        "producto, el almacén lo inspecciona y determina si se puede revender, reacondicionar o "
        "si debe desecharse. Las devoluciones entre países quedan fuera de ese circuito automático "
        "y las tramita manualmente el equipo de devoluciones."
    )

    class SyntheticSSE(httpx.AsyncByteStream):
        def __init__(self, parts, *, block=False):
            self.parts = parts
            self.block = block

        async def __aiter__(self):
            for part in self.parts:
                yield b"data: " + json.dumps({
                    "choices": [{"delta": {"content": part}}],
                }).encode() + b"\n\n"
            if self.block:
                await_abort.set()
                await asyncio.Event().wait()
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            if self.block:
                stream_closed.set()

    def respond(request):
        body = json.loads(request.content)
        prompt = body["messages"][1]["content"]
        question = prompt.split("\n", 3)
        if parity_question in question[:3]:
            parity_requests.append(body)
            assert "Output only the JSON" not in body["messages"][0]["content"]
            assert all(passage.text in prompt.replace("\\n", "\n") for passage in parity_passages)
            if not body.get("stream"):
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": natural_answer}, "finish_reason": "stop"}],
                })
            stream = SyntheticSSE([natural_answer[:147], natural_answer[147:]])
        elif slow_question in question[:3]:
            stream = SyntheticSSE(["Unvalidated draft"], block=True)
        elif sensitive_question in question[:3]:
            assert "Conversation JSON:" not in prompt
            stream = SyntheticSSE(["Bearer ", "abcdefghijkl"])
        elif redirect_question in question[:3]:
            has_context = (
                anchor_question in prompt
                and "The standard window is thirty days." not in prompt
                and slow_question not in prompt
                and "Unvalidated draft" not in prompt
            )
            answer = "The previously discussed window is thirty days." if has_context else "No prior window found."
            stream = SyntheticSSE([answer])
        else:
            stream = SyntheticSSE(["The standard window ", "is thirty days."])
        assert body["stream"] is True
        requests_seen.append(prompt)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream,
        )

    class SyntheticRetrieval(RAGService):
        def retrieve(self, question, *args, **kwargs):
            retrieval_queries.append(question)
            assert args[1] == 0.57
            if question in {abstention_question, redirect_question}:
                return ()
            if question == parity_question:
                return tuple(RetrievalResult(passage, 0.65) for passage in parity_passages)
            return StubRAGService().retrieve(question, *args, **kwargs)

    @contextmanager
    def rag_context(**_kwargs):
        gateway = FourGeeksHTTPClient(
            RAGSettings(
                base_url="https://gateway.example.test/v1", api_key="fixture",
                embedding_model="unused", generation_model="fixture",
                qdrant_url="unused", request_timeout_seconds=5,
            ),
            httpx.Client(transport=httpx.MockTransport(respond)),
            async_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
            ),
        )
        try:
            yield SyntheticRetrieval(
                None, gateway, None, default_min_score=0.57,
            )
        finally:
            gateway.close()

    def open_session():
        return Session(chat_db)

    def db_dependency():
        with open_session() as db:
            yield db

    def verify_test_oidc(credentials):
        assert credentials.credentials == "fixture-oidc"
        return support_identity

    for module in (coordinator, producer, router):
        monkeypatch.setattr(module, "open_db_session", open_session)
    monkeypatch.setattr(router, "_PUBSUB", SessionPubSub(queue_size=16))
    monkeypatch.setattr(auth_security, "_find_user_by_id", lambda user_id: user if user_id == user.id else None)
    monkeypatch.setattr(router, "authenticate_support_identity", verify_test_oidc)
    monkeypatch.setattr(producer, "get_external_support_agent", lambda: agent)
    monkeypatch.setattr(producer, "get_support_mcp_tools", lambda _identity: MCPTools())
    monkeypatch.setattr(support, "rag_service_context", rag_context)
    agent = ExternalSupportAgent()
    app = FastAPI()
    app.include_router(router.router)
    app.include_router(support.router)
    app.dependency_overrides[router.get_current_user] = lambda: user
    app.dependency_overrides[router.get_db] = db_dependency
    app.dependency_overrides[support.require_support_reader] = lambda: support_identity
    app.dependency_overrides[support.get_external_support_agent] = lambda: agent
    app.dependency_overrides[support.get_support_mcp_tools] = lambda: MCPTools()

    with TestClient(app) as client:
        created = client.post("/chat/sessions", json={"client_id": "client-a"})
        assert created.status_code == 201, created.json()
        session_id = created.json()["session_id"]

        def send(socket, event, **data):
            socket.send_json({"event": event, "data": {"session_id": session_id, **data}})

        def authenticate(socket, *, progress=False):
            send(socket, "authenticate", access_token=create_access_token(user.id),
                 support_token="fixture-oidc", progress_events=progress)
            snapshot = socket.receive_json()
            assert snapshot["event"] == "session_snapshot", snapshot
            return snapshot["data"]

        def completed_turn(socket, expected, *, abstained=False, clarification=False):
            chunk = socket.receive_json()
            assert chunk["event"] == "token_chunk", chunk
            assert chunk["data"]["token"] == expected
            if abstained:
                assert chunk["data"]["answer_kind"] == "insufficient_context"
                assert chunk["data"]["knowledge"] == {"coverage": "insufficient", "sources": []}
            elif clarification:
                assert chunk["data"]["answer_kind"] == "clarification"
            else:
                assert "answer_kind" not in chunk["data"]
            completed = socket.receive_json()
            assert completed["event"] == "generation_completed", completed
            assert completed["data"]["generation_id"] == chunk["data"]["generation_id"]
            return completed["data"]

        with client.websocket_connect("/chat/ws") as socket:
            assert authenticate(socket)["messages"] == []
            send(socket, "user_message", text=anchor_question)
            first = completed_turn(socket, "The standard window is thirty days.")
            with client.websocket_connect("/chat/ws") as observer:
                assert "progress" not in authenticate(observer, progress=True)
                send(socket, "user_message", text=slow_question)
                assert await_abort.wait(3), "controlled provider did not start the second turn"
                while True:
                    progress = observer.receive_json()
                    assert progress["event"] == "generation_progress"
                    assert set(progress["data"]) == {"session_id", "generation_id", "stage", "cursor"}
                    if progress["data"]["stage"] == "preparing_response":
                        break
                with client.websocket_connect("/chat/ws") as reconnected:
                    assert authenticate(reconnected, progress=True)["progress"] == progress["data"]
                send(socket, "interrupt_requested", new_input=redirect_question)
                interrupted = socket.receive_json()
                assert interrupted["data"]["generation_id"] == progress["data"]["generation_id"]
            assert interrupted["event"] == "generation_interrupted", interrupted
            assert stream_closed.is_set()
            assert interrupted["data"]["generation_id"] != first["generation_id"]
            redirected = completed_turn(socket, "The previously discussed window is thirty days.")
            assert redirected["generation_id"] != interrupted["data"]["generation_id"]
            assert first["cursor"] < interrupted["data"]["cursor"] < redirected["cursor"]

        with client.websocket_connect("/chat/ws") as socket:
            snapshot = authenticate(socket)
            assert snapshot["cursor"] == redirected["cursor"]
            assert snapshot["session_id"] == session_id
            by_id = {message["message_id"]: message for message in snapshot["messages"]}
            cancelled_message = by_id[interrupted["data"]["message_id"]]
            assert cancelled_message["status"] == "interrupted"
            assert cancelled_message["content"] == ""
            assert "Unvalidated draft" not in json.dumps(snapshot)
            send(socket, "user_message", text=redirect_question)
            completed_turn(socket, "The previously discussed window is thirty days.")
            send(socket, "user_message", text=sensitive_question)
            completed_turn(socket, SAFE_OUTPUT_MESSAGE)
            send(socket, "user_message", text=abstention_question)
            abstained = completed_turn(socket, INSUFFICIENT_INFORMATION_ANSWER, abstained=True)
        with client.websocket_connect("/chat/ws") as socket:
            snapshot = authenticate(socket, progress=True)
            assert "progress" not in snapshot
            messages = snapshot["messages"]
            row = next(message for message in messages if message["message_id"] == abstained["message_id"])
            assert row["answer_kind"] == "insufficient_context"
            assert row["content"] == INSUFFICIENT_INFORMATION_ANSWER
            assert row["knowledge"] == {"coverage": "insufficient", "sources": []}
        final_snapshot = client.get(f"/chat/sessions/{session_id}")
        assert final_snapshot.status_code == 200
        assert "abcdefghijkl" not in final_snapshot.text
        assert len(requests_seen) == 5
        assert retrieval_queries == [
            anchor_question, slow_question, anchor_question, anchor_question,
            sensitive_question, abstention_question,
        ]

        # A separate session for the same authorized client has no antecedent.
        fresh = client.post("/chat/sessions", json={"client_id": "client-a"})
        assert fresh.status_code == 201
        assert fresh.json()["session_id"] != session_id
        session_id = fresh.json()["session_id"]
        with client.websocket_connect("/chat/ws") as socket:
            assert authenticate(socket)["messages"] == []
            send(socket, "user_message", text=redirect_question)
            completed_turn(socket, "¿A qué te refieres? Indica el tema o la política que quieres consultar.", clarification=True)
        assert len(retrieval_queries) == 6
        assert len(requests_seen) == 5

        # Deliver the same synthesis in JSON and SSE, while keeping
        # distinct client conversations and using the real HTTP/WS boundaries.
        http = client.post("/support/query", json={"question": parity_question})
        assert http.status_code == 200, http.json()
        result = http.json()
        assert result["answer"] == natural_answer
        assert result["partial"] is False
        assert "coverage" not in result["knowledge"]
        expected_knowledge = {"sources": result["knowledge"]["sources"]}
        assert expected_knowledge["sources"] == [
            {
                "source_document": passage.source_document,
                "section": passage.section,
                "retrieval_kind": "primary",
            }
            for passage in parity_passages
        ]
        with client.websocket_connect("/chat/ws") as socket:
            authenticate(socket)
            send(socket, "user_message", text=parity_question)
            chunk = socket.receive_json()
            assert chunk["event"] == "token_chunk", chunk
            assert chunk["data"]["token"] == result["answer"]
            assert chunk["data"]["knowledge"] == expected_knowledge
            completed = socket.receive_json()
            assert completed["event"] == "generation_completed"
        with client.websocket_connect("/chat/ws") as socket:
            snapshot = authenticate(socket)
            assert snapshot["cursor"] == completed["data"]["cursor"]
            row = next(
                message for message in snapshot["messages"]
                if message["message_id"] == chunk["data"]["message_id"]
            )
            assert row["knowledge"] == expected_knowledge
        assert len(parity_requests) == 2
        assert parity_requests[1].pop("stream") is True
        assert parity_requests[0] == parity_requests[1]
        assert retrieval_queries[-2:] == [parity_question, parity_question]


def test_experimental_original_deltas_persist_before_fanout_and_survive_abort(
    chat_db, monkeypatch,
):
    from services.api.agent_memory import models as memory
    from services.api.auth_security import create_access_token
    from services.api.chat import coordinator, router
    from services.api.chat.contracts import TokenChunkEvent
    from services.api.chat.models import chat_events
    from services.api.chat.pubsub import SessionPubSub
    from services.api.routes.support import (
        SupportKnowledgeResponse,
        SupportKnowledgeSourceResponse,
        SupportQueryResponse,
    )
    from services.incidents.models import user_client_memberships
    from services.rag.clients import FourGeeksHTTPClient
    from services.rag.config import RAGSettings
    from tests.test_support_memory_api import identity
    from tests.ws_incremental_experiment import incremental_producer

    owner = identity("incremental-qa", Role.manager)
    outsider = identity("incremental-foreign-qa", Role.manager)
    identities = {actor.user.id: actor for actor in (owner, outsider)}
    user_client_memberships.create(chat_db)
    memory.operational_security_metadata.create_all(chat_db, tables=[
        memory.memory_conversations, memory.memory_proposals,
        memory.memory_decisions, memory.memory_entries,
    ])
    with Session(chat_db) as db:
        db.execute(user_client_memberships.insert(), [
            {
                "user_id": actor.user.id, "client_id": client_id,
                "can_view_sensitive": False, "assigned_by_user_id": "qa-admin",
            }
            for actor, client_id in ((owner, "client-a"), (outsider, "client-b"))
        ])
        db.commit()

    original_deltas = ("La solicitud de devolución ", "se inicia en el portal de la marca.")
    confirmed_prefix = "".join(original_deltas)
    withheld = " La excepción se aplica únicamente si"
    redirected_answer = "El operador inspecciona el producto al recibirlo."
    provider_waiting = Event()
    provider_closed = Event()
    provider_cancelled = Event()
    turns = []
    fanout_calls = []
    source = SupportKnowledgeSourceResponse(
        source_document="returns-policy", section="standard-return-process",
        retrieval_kind="primary",
    )

    class ControlledSSE(httpx.AsyncByteStream):
        def __init__(self, first_turn):
            self.first_turn = first_turn

        async def __aiter__(self):
            parts = (*original_deltas, withheld) if self.first_turn else (redirected_answer,)
            for part in parts:
                payload = {"choices": [{"delta": {"content": part}}]}
                yield b"data: " + json.dumps(payload).encode() + b"\n\n"
            if self.first_turn:
                provider_waiting.set()
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=8)
                except asyncio.CancelledError:
                    provider_cancelled.set()
                    raise
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            if self.first_turn:
                provider_closed.set()

    class ObservePersistenceBeforeFanout(SessionPubSub):
        async def publish(self, session_id, event):
            if isinstance(event, TokenChunkEvent):
                with Session(chat_db) as db:
                    payload = db.execute(chat_events.select().where(
                        chat_events.c.session_id == session_id,
                        chat_events.c.cursor == event.data.cursor,
                    )).mappings().one().payload
                    assert payload == event.data.model_dump(mode="json", exclude_none=True)
                fanout_calls.append(event.data.token)
            return await super().publish(session_id, event)

    async def execute(session_id, question, observe_delta):
        first_turn = question == "Describe cómo iniciar la devolución."
        if not first_turn:
            assert question == "¿Qué hace el operador al recibir el producto?"
            assert provider_cancelled.is_set() and provider_closed.is_set()
        turns.append(question)

        def respond(request):
            body = json.loads(request.content)
            assert body["stream"] is True
            assert question in body["messages"][1]["content"]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ControlledSSE(first_turn),
            )

        gw = FourGeeksHTTPClient(
            RAGSettings(
                base_url="https://gateway.example.test/v1", api_key="fixture",
                embedding_model="unused", generation_model="fixture",
                qdrant_url="unused", request_timeout_seconds=10,
            ),
            async_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
            ),
        )
        try:
            answer = await gw.generate_async(
                question, "Synthetic returns-policy fixture", delta_observer=observe_delta,
            )
        finally:
            gw.close()
        return SupportQueryResponse(
            answer=answer, route="knowledge", partial=False, needs_clarification=False,
            conversation_id=UUID(session_id),
            knowledge=SupportKnowledgeResponse(status="ok", answer=answer, sources=[source]),
        )

    def open_session():
        return Session(chat_db)

    def db_dependency():
        with open_session() as db:
            yield db

    def experimental_producer(actor, _admission=None):
        async def authorize(session_id):
            with open_session() as db:
                access = router.build_access_context(db, actor.user)
                try:
                    get_chat_session(db, access, session_id)
                except ChatNotFound:
                    return False
                return access.allowed_client_ids == frozenset({"client-a"})

        # This controlled proof is a fixture for testing delivery and abort
        # mechanics. It is not a verifier for natural prose.
        def fixture_certificate(prefix):
            return prefix in {confirmed_prefix, redirected_answer}

        return incremental_producer(execute, authorize, certify=fixture_certificate, sources=(source,))

    monkeypatch.setattr(coordinator, "open_db_session", open_session)
    monkeypatch.setattr(router, "open_db_session", open_session)
    monkeypatch.setattr(router, "_PUBSUB", ObservePersistenceBeforeFanout())
    monkeypatch.setattr(auth_security, "_find_user_by_id", lambda user_id: identities[user_id].user)
    monkeypatch.setattr(router, "authenticate_support_identity", lambda credentials: identities[credentials.credentials])
    monkeypatch.setattr(router, "authentic_support_producer", experimental_producer)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_current_user] = lambda: owner.user
    app.dependency_overrides[router.get_db] = db_dependency

    with TestClient(app) as client:
        new_session = client.post("/chat/sessions", json={"client_id": "client-a"})
        assert new_session.status_code == 201, new_session.json()
        session_id = new_session.json()["session_id"]

        def send(socket, event, **data):
            socket.send_json({"event": event, "data": {"session_id": session_id, **data}})

        def authenticate(socket, actor=owner):
            send(
                socket, "authenticate", access_token=create_access_token(actor.user.id),
                support_token=actor.user.id,
            )
            snapshot = socket.receive_json()
            assert snapshot["event"] == "session_snapshot", snapshot
            return snapshot["data"]

        with client.websocket_connect("/chat/ws") as socket, client.websocket_connect("/chat/ws") as observer:
            assert authenticate(socket)["messages"] == authenticate(observer)["messages"] == []
            send(socket, "user_message", text="Describe cómo iniciar la devolución.")
            first = [socket.receive_json(), socket.receive_json()]
            assert [item["event"] for item in first] == ["token_chunk", "token_chunk"]
            assert [item["data"]["token"] for item in first] == list(original_deltas)
            assert [item["data"]["sequence"] for item in first] == [1, 2]
            assert first[0]["data"]["generation_id"] == first[1]["data"]["generation_id"]
            assert first[0]["data"]["cursor"] < first[1]["data"]["cursor"]
            assert [observer.receive_json(), observer.receive_json()] == first
            assert provider_waiting.wait(3)
            assert not provider_closed.is_set()
            assert turns == ["Describe cómo iniciar la devolución."]

            send(socket, "interrupt_requested", new_input="¿Qué hace el operador al recibir el producto?")
            interrupted = socket.receive_json()
            assert interrupted["event"] == "generation_interrupted"
            assert interrupted["data"]["generation_id"] == first[0]["data"]["generation_id"]
            assert interrupted["data"]["message_id"] == first[0]["data"]["message_id"]
            assert first[1]["data"]["cursor"] < interrupted["data"]["cursor"]
            assert provider_cancelled.is_set() and provider_closed.is_set()
            assert observer.receive_json() == interrupted
            redirected = socket.receive_json()
            assert redirected["event"] == "token_chunk"
            assert redirected["data"]["token"] == redirected_answer
            assert redirected["data"]["generation_id"] != interrupted["data"]["generation_id"]
            assert redirected["data"]["message_id"] != interrupted["data"]["message_id"]
            assert redirected["data"]["sequence"] == 1
            assert observer.receive_json() == redirected
            completed = socket.receive_json()
            assert completed["event"] == "generation_completed"
            assert completed["data"]["generation_id"] == redirected["data"]["generation_id"]
            assert observer.receive_json() == completed

        with client.websocket_connect("/chat/ws") as reconnected:
            snapshot = authenticate(reconnected)
            assert snapshot["cursor"] == completed["data"]["cursor"]
            by_id = {item["message_id"]: item for item in snapshot["messages"]}
            partial = by_id[interrupted["data"]["message_id"]]
            assert partial["status"] == "interrupted"
            assert partial["content"] == confirmed_prefix
            assert by_id[redirected["data"]["message_id"]]["content"] == redirected_answer
            assert withheld not in json.dumps(snapshot, ensure_ascii=False)
        with client.websocket_connect("/chat/ws") as foreign:
            with pytest.raises(WebSocketDisconnect) as denied:
                authenticate(foreign, outsider)
            assert denied.value.code == 4404

    assert fanout_calls == [*original_deltas, redirected_answer]
    assert len(turns) == 2
    with Session(chat_db) as db:
        for table in (memory.memory_proposals, memory.memory_decisions, memory.memory_entries):
            assert db.execute(table.select()).first() is None
