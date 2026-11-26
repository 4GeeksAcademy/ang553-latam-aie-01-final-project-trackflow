import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlmodel import Session

from services.api.chat import coordinator, router, store
from services.api.chat.contracts import CoverageUpdatedEvent, UnitCoverage, UnitReference
from services.api.chat.models import chat_events
from services.api.chat.pubsub import SessionPubSub
from services.api.chat.qa_unit_catalog import authorized_unit, catalog
from tests.test_i13_qa_units import CLIENT, _access
from tests.test_i14_unit_durability import units_db
from tests.test_websocket_chat_postgres import chat_db


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_interruption_preserves_atomic_partial_coverage_and_revocation_removes_it(
    units_db, monkeypatch,
):
    access = _access()
    coverage = UnitCoverage(status="partial", missing_topics=("return_cost",))
    monkeypatch.setattr(coordinator, "open_db_session", lambda: Session(units_db))
    monkeypatch.setattr(router, "open_db_session", lambda: Session(units_db))
    with Session(units_db) as db:
        session_id = store.create_chat_session(db, access, client_id=CLIENT).session_id
        unit = authorized_unit(db, CLIENT, "DEV-PLAZO", "1.0")
    pubsub = SessionPubSub()
    subscriber = await pubsub.subscribe(session_id)
    running = coordinator.ChatCoordinator(coordinator.SQLGenerationRepository(access), pubsub)
    stopped = asyncio.Event()

    async def producer(_session_id, _text):
        try:
            yield coordinator.AnnotatedChunk(
                unit.render, publication_kind="authorized_unit",
                unit_ref=UnitReference(unit_id=unit.id, version=unit.version), unit_coverage=coverage,
            )
            await asyncio.Event().wait()
            yield coordinator.CoverageUpdate(UnitCoverage(status="complete"))
        finally:
            stopped.set()

    try:
        assert await running.start(session_id, "consulta de prueba", producer)
        published = await asyncio.wait_for(subscriber.receive(), 5)
        assert published.event == "token_chunk" and published.data.unit_coverage == coverage
        assert "unit_ref" not in published.model_dump(mode="json")["data"]
        assert await running.interrupt(session_id, None, producer)
        assert (await asyncio.wait_for(subscriber.receive(), 5)).event == "generation_interrupted"
        assert stopped.is_set()
        with Session(units_db) as db:
            snapshot = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
            assert snapshot.messages[-1].status == "interrupted"
            assert snapshot.messages[-1].content == unit.render
            assert snapshot.messages[-1].unit_coverage == coverage
            assert store.chat_snapshot(db, access, session_id).messages[-1].unit_coverage is None
            assert db.execute(select(chat_events.c.event_type).where(
                chat_events.c.session_id == session_id,
            ).order_by(chat_events.c.cursor)).scalars().all() == ["token_chunk", "generation_interrupted"]

            db.execute(catalog.update().where(catalog.c.unit_id == unit.id).values(status="revoked"))
            db.commit()
            snapshot = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
            assert snapshot.messages[-1].content == ""
            assert snapshot.messages[-1].unit_coverage is None

        async def valid_oidc(*_args):
            return True

        async def receive():
            return published

        async def unexpected_send(_payload):
            pytest.fail("revoked unit coverage reached the network")

        monkeypatch.setattr(router, "_has_current_support_identity", valid_oidc)
        with pytest.raises(HTTPException) as denied:
            await router._sender(
                SimpleNamespace(send_json=unexpected_send), SimpleNamespace(receive=receive),
                after_cursor=0, local=SimpleNamespace(revalidate=lambda _session_id: access),
                session_id=session_id, support_token="synthetic",
            )
        assert denied.value.status_code == 403
    finally:
        await running.close()


def test_coverage_only_snapshot_has_no_content_and_requires_qa_authorization(units_db, monkeypatch):
    access = _access()
    coverage = UnitCoverage(status="none", missing_topics=("return_inspection",))
    with Session(units_db) as db:
        session_id = store.create_chat_session(db, access, client_id=CLIENT).session_id
        generation = store.begin_generation(db, access, session_id, text="inspección de prueba")
        payload = store.record_unit_coverage(db, generation, coverage)
        event = CoverageUpdatedEvent(data=payload)
        assert event.data.coverage == coverage
        assert "token" not in event.model_dump(mode="json")["data"]
        assert store.qa_coverage_is_current(db, access, session_id, event.data.message_id, event.data.cursor)
        with pytest.raises(store.GenerationConflict):
            store.append_chunk(db, generation, "Respuesta sin referencia")
        db.rollback()
        store.complete_generation(db, generation)
        snapshot = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
        assert snapshot.messages[-1].status == "completed"
        assert snapshot.messages[-1].unit_coverage == coverage
        assert snapshot.messages[-1].content == ""
        assert snapshot.messages[-1].publication_kind is None
        assert store.chat_support_context(db, access, session_id).history_json is None
        assert store.chat_snapshot(db, access, session_id).messages[-1].unit_coverage is None
        monkeypatch.setenv("TRACKFLOW_I14_QA_UNITS", "0")
        assert store.chat_snapshot(db, access, session_id, include_authorized_units=True).messages[-1].unit_coverage is None
