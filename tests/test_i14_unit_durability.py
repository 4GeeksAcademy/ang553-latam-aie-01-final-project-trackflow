from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlmodel import Session

from services.api.chat import router, store
from services.api.chat.contracts import TokenChunkData, TokenChunkEvent, UnitReference
from services.api.chat.models import chat_events, chat_messages
from services.api.chat.qa_unit_catalog import authorized_unit, catalog
from services.incidents.models import user_client_memberships
from tests.test_i13_qa_units import CLIENT, USER, _access, _fixture_env
from tests.test_websocket_chat_postgres import chat_db


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def units_db(chat_db, monkeypatch):
    with Session(chat_db) as db:
        if tuple(db.execute(text("SELECT current_database(), current_user")).one()) != (
            "trackflow_ws_qa", "trackflow_ws_qa",
        ):
            pytest.skip("I14 requires disposable WS-07 PostgreSQL")
    _fixture_env(monkeypatch)
    monkeypatch.setenv("TRACKFLOW_I14_QA_UNITS", "1")
    user_client_memberships.create(chat_db)
    catalog.create(chat_db)
    with Session(chat_db) as db:
        db.execute(text(
            "INSERT INTO trackflow_client(client_id, display_name) VALUES (:client, 'QA I14')"
        ), {"client": CLIENT})
        db.execute(user_client_memberships.insert().values(
            user_id=USER, client_id=CLIENT, assigned_by_user_id="qa-admin",
        ))
        db.execute(catalog.insert(), [
            {
                "unit_id": unit_id, "version": "1.0", "client_id": CLIENT,
                "approved_text": content, "language": "es", "source_document": "qa-test",
                "source_section": "politica", "anchor_document": "returns-policy",
                "anchor_section": "return-window-and-costs", "anchor_sha256": "a" * 64,
                "topic_aliases": ["devolucion"], "qualifiers": ["ficticia"],
                "composition_group": "qa", "status": "published",
            }
            for unit_id, content in (
                ("DEV-PLAZO", "Política ficticia con plazo de prueba."),
                ("DEV-COSTO", "Política ficticia con coste de prueba."),
            )
        ])
        db.commit()
    return chat_db


def _append(db, generation, unit_id, *, prefix=""):
    reference = UnitReference(unit_id=unit_id, version="1.0")
    unit = authorized_unit(db, CLIENT, unit_id, "1.0")
    payload = store.append_chunk(
        db, generation, prefix + unit.render, publication_kind="authorized_unit",
        unit_ref=reference,
    )
    return TokenChunkEvent(data=TokenChunkData.model_validate(payload))


def test_interrupted_dynamic_content_replays_exactly_with_current_grant(units_db):
    access = _access()
    with Session(units_db) as db:
        session_id = store.create_chat_session(db, access, client_id=CLIENT).session_id
        current = store.begin_generation(db, access, session_id, text="consulta dinámica")
        first = _append(db, current, "DEV-PLAZO")
        store.interrupt_generation(db, current)
        following = store.begin_generation(db, access, session_id, text="consulta independiente")
        store.append_chunk(db, following, "Respuesta natural validada")
        store.complete_generation(db, following)
        snapshot = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
        assert snapshot.messages[1].status == "interrupted"
        assert snapshot.messages[1].content == first.data.token
        assert snapshot.messages[3].status == "completed"
        assert snapshot.messages[3].content == "Respuesta natural validada"
        assert snapshot.messages[1].message_id != snapshot.messages[3].message_id


def test_revoked_catalog_version_blocks_append_and_snapshot_but_preserves_audit(units_db, monkeypatch):
    access = _access()
    with Session(units_db) as db:
        session_id = store.create_chat_session(db, access, client_id=CLIENT).session_id
        legacy = store.begin_generation(db, access, session_id, text="fixture legacy")
        store.append_chunk(db, legacy, "Texto de fixture I13", publication_kind="authorized_unit")
        store.complete_generation(db, legacy)
        current = store.begin_generation(db, access, session_id, text="consulta dinámica")
        first = _append(db, current, "DEV-PLAZO")
        assert "unit_ref" not in first.model_dump(mode="json")["data"]
        with pytest.raises(store.GenerationConflict):
            store.append_chunk(db, current, "Texto ajeno al catálogo")
        db.rollback()
        authorized = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
        assert authorized.messages[-1].content == first.data.token
        monkeypatch.setenv("TRACKFLOW_I14_QA_UNITS", "0")
        assert store.chat_snapshot(db, access, session_id, include_authorized_units=True).messages[-1].content == ""
        monkeypatch.setenv("TRACKFLOW_I14_QA_UNITS", "1")
        assert store.chat_snapshot(db, access, session_id).messages[-1].content == ""
        with pytest.raises(store.ChatNotFound):
            store.chat_snapshot(db, _access("client-b"), session_id, include_authorized_units=True)

        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-COSTO").values(composition_group="other"))
        db.commit()
        with pytest.raises(store.GenerationConflict):
            _append(db, current, "DEV-COSTO", prefix="\n\n")
        db.rollback()
        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-COSTO").values(composition_group="qa"))
        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-PLAZO").values(status="revoked"))
        db.commit()
        with pytest.raises(store.GenerationConflict):
            _append(db, current, "DEV-COSTO", prefix="\n\n")
        db.rollback()
        store.interrupt_generation(db, current)
        withheld = store.chat_snapshot(db, access, session_id, include_authorized_units=True)
        assert withheld.messages[1].content == "Texto de fixture I13"
        assert withheld.messages[-1].content == "" and withheld.messages[-1].knowledge is None
        assert withheld.messages[-1].publication_kind is None
        assert db.execute(select(chat_messages.c.content).where(
            chat_messages.c.message_id == current.assistant_message_id,
        )).scalar_one() == first.data.token
        payload = db.execute(select(chat_events.c.payload).where(
            chat_events.c.session_id == session_id, chat_events.c.cursor == first.data.cursor,
        )).scalar_one()
        assert payload["unit_ref"] == {"unit_id": "DEV-PLAZO", "version": "1.0"}
        monkeypatch.setenv("TRACKFLOW_I13_QA_UNITS", "0")
        another = store.begin_generation(db, access, session_id, text="consulta dinámica")
        with pytest.raises(store.GenerationConflict):
            store.append_chunk(db, another, "Unidad sin referencia", publication_kind="authorized_unit")


@pytest.mark.anyio
async def test_live_delivery_rejects_catalog_content_drift_after_commit(units_db, monkeypatch):
    access = _access()
    with Session(units_db) as db:
        session_id = store.create_chat_session(db, access, client_id=CLIENT).session_id
        current = store.begin_generation(db, access, session_id, text="consulta dinámica")
        event = _append(db, current, "DEV-PLAZO")
        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-PLAZO").values(
            approved_text="Política ficticia cambiada dentro de la misma versión.",
        ))
        db.commit()

    async def valid_oidc(*_args):
        return True

    async def receive():
        return event

    async def unexpected_send(_payload):
        pytest.fail("a stale unit body reached the network")

    monkeypatch.setattr(router, "open_db_session", lambda: Session(units_db))
    monkeypatch.setattr(router, "_has_current_support_identity", valid_oidc)
    with pytest.raises(HTTPException) as denied:
        await router._sender(
            SimpleNamespace(send_json=unexpected_send), SimpleNamespace(receive=receive),
            after_cursor=0, local=SimpleNamespace(revalidate=lambda _session: access),
            session_id=session_id, support_token="synthetic",
        )
    assert denied.value.status_code == 403
