import asyncio
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

import httpx
import pytest
from sqlmodel import Session

from services.api.chat import qa_units
from services.api.chat.qa_unit_catalog import CatalogUnit, authorized_unit, catalog, catalog_anchor_query, eligible_units
from services.api.chat.qa_units_scope import QaUnitScope
from services.rag.config import DEFAULT_BASE_URL, RAGSettings
from services.rag.contracts import KnowledgeChunk, RetrievalResult
from tests.test_i13_qa_units import FixtureStream, _allow
from tests.test_websocket_chat_postgres import chat_db  # noqa: F401 - pytest fixture


CLIENT = "tenant-a"
OTHER = "tenant-b"
ANCHOR = KnowledgeChunk("qa-test-anchor", "returns-policy", "return-window-and-costs", 0, "Test anchor")
EVIDENCE = (RetrievalResult(ANCHOR, 0.63),)


def test_catalog_selects_by_query_evidence_scope_and_publication_status(chat_db, monkeypatch):
    catalog.create(chat_db)
    base = {
        "version": "1.0",
        "client_id": CLIENT,
        "language": "es",
        "source_document": "qa-i14-devoluciones",
        "anchor_document": ANCHOR.source_document,
        "anchor_section": ANCHOR.section,
        "anchor_sha256": sha256(ANCHOR.text.encode()).hexdigest(),
        "qualifiers": ["solo en QA"],
        "composition_group": "retornos-qa",
        "status": "published",
    }
    plazo = dict(base, unit_id="DEV-PLAZO", source_section="plazo", approved_text="21 días solo en QA.",
                 topic_aliases=["plazo", "limite"])
    coste = dict(base, unit_id="DEV-COSTE", source_section="coste", approved_text="La marca paga solo en QA.",
                 topic_aliases=["coste", "costo"])
    with Session(chat_db) as db:
        for row in (plazo, coste, dict(plazo, client_id=OTHER, status="draft")):
            db.execute(catalog.insert().values(**row))
        db.commit()

        def ids(question, tenant=CLIENT, evidence=EVIDENCE):
            return tuple(unit.id for unit in eligible_units(db, tenant, question, evidence))

        assert ids("¿Cuál es el plazo de devolución de prueba?") == ("DEV-PLAZO",)
        assert ids("¿Cuál es el límite de la devolución simulada?") == ("DEV-PLAZO",)
        assert ids("¿Cuál es el coste de devolución de prueba?") == ("DEV-COSTE",)
        assert ids("¿Cuál es el plazo y el costo de devolución de prueba?") == ("DEV-COSTE", "DEV-PLAZO")
        assert not ids("¿Cuál es el plazo de devolución?")
        assert not ids("¿Por qué hay un plazo de devolución de prueba?")
        assert not ids("¿Cuál es el plazo de devolución de prueba y cómo se compara con la política internacional?")
        assert not ids("¿Cuál es el plazo de devolución de prueba?", OTHER)
        assert not ids("¿Cuál es el plazo de devolución de prueba?", evidence=())
        assert not ids("¿Cuál es el plazo de devolución de prueba?", evidence=(RetrievalResult(ANCHOR, 0.56),))
        assert not ids("¿Cuál es el plazo de devolución de prueba?",
                       evidence=(RetrievalResult(ANCHOR, 0.80, "complementary"),))
        amended = KnowledgeChunk(ANCHOR.point_id, ANCHOR.source_document, ANCHOR.section, 0, "New version")
        assert not ids("¿Cuál es el plazo de devolución de prueba?", evidence=(RetrievalResult(amended, 0.70),))

        monkeypatch.setattr(qa_units, "open_db_session", lambda: Session(chat_db))
        lookups = []

        def resolve_anchor(key):
            lookups.append(key)
            return EVIDENCE

        scope = QaUnitScope("manager", CLIENT)
        ordinary = "¿Cuál es el plazo de devolución?"
        qualified = "¿Cuál es el plazo y el costo de devolución de prueba?"
        assert not qa_units.select_dynamic_units(ordinary, (), scope, resolve_anchor)
        assert catalog_anchor_query(db, OTHER, qualified) is None
        assert not lookups
        plan = qa_units.select_dynamic_units(qualified, (), scope, resolve_anchor)
        assert tuple(unit.id for unit in plan) == ("DEV-COSTE", "DEV-PLAZO")
        assert lookups == ["TrackFlow returns policy: return window and costs"]
        assert not qa_units.select_dynamic_units(
            qualified, (), scope, lambda _key: (RetrievalResult(ANCHOR, 0.5699),),
        )

        unit = authorized_unit(db, CLIENT, "DEV-PLAZO", "1.0", lock=True)
        assert unit is not None and "solo en QA" in unit.render
        assert authorized_unit(db, CLIENT, "DEV-PLAZO", "2.0") is None
        assert authorized_unit(db, OTHER, "DEV-PLAZO", "1.0") is None
        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-PLAZO").values(status="revoked"))
        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-COSTE").values(
            valid_until=datetime.now(UTC) - timedelta(seconds=1),
        ))
        db.commit()
        assert authorized_unit(db, CLIENT, "DEV-PLAZO", "1.0") is None
        assert authorized_unit(db, CLIENT, "DEV-COSTE", "1.0") is None
        assert not ids("¿Cuál es el plazo y el costo de devolución de prueba?")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("frames, count, reason", [
    (["DEV-PLAZO", ("", "stop")], 1, None),
    (["DEV-COSTE\n", "DEV-PLAZO", ("", "stop")], 2, None),
    (["DEV-COSTE\n", "DEV-OTRO\n"], 1, "identifier_not_in_plan"),
    (["DEV-PLAZO\n", "DEV-COSTE\n"], 1, "identifier_out_of_order"),
    (["DEV-COSTE\n", "DEV-COSTE\n"], 1, "duplicate_identifier"),
    (['"DEV-PLAZO"', ("", "stop")], 0, "invalid_identifier_format"),
    (["\n", ("", "stop")], 0, "empty_identifier_line"),
    ([("", "stop")], 0, "incomplete_plan"),
])
async def test_dynamic_gateway_accepts_plan_subset_and_rejects_invented_or_incompatible_ids(frames, count, reason):
    plan = (
        CatalogUnit("DEV-COSTE", "1.0", "La marca paga solo en QA.", "qa-i14-devoluciones", "coste", "es", "QA"),
        CatalogUnit("DEV-PLAZO", "1.0", "21 días solo en QA.", "qa-i14-devoluciones", "plazo", "es", "QA"),
    )
    source = FixtureStream(frames, terminal=False)

    def response(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert "DEV-COSTE\nDEV-PLAZO" in payload["messages"][0]["content"]
        assert "La marca paga" not in payload["messages"][0]["content"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    settings = RAGSettings(DEFAULT_BASE_URL, "synthetic", "unused", "fixture", "unused")
    gateway = qa_units._DynamicQaUnitGateway(
        settings, plan, async_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    async with aclosing(qa_units._selection_units(gateway, _allow, plan=plan)) as selection:
        chunks = [await anext(selection) for _ in range(count)]
        assert all(chunk.publication_kind == "authorized_unit" and chunk.unit_ref for chunk in chunks)
        if reason is None:
            with pytest.raises(StopAsyncIteration):
                await anext(selection)
        else:
            with pytest.raises(qa_units.QaUnitRejected) as rejected:
                await anext(selection)
            assert rejected.value.reason == reason
    assert source.closed.is_set()
    assert gateway.last_generation_status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("revoked", [False, True])
async def test_single_eligible_unit_requires_current_grant_and_never_starts_a_gateway(revoked):
    unit = CatalogUnit("DEV-PLAZO", "1.0", "21 días solo en QA.", "qa-i14-devoluciones", "plazo", "es", "qa")

    def unexpected_gateway():
        pytest.fail("a singleton must not invoke FourGeeks selection")

    async def authorize():
        if revoked:
            raise qa_units.QaUnitRejected("catalog_grant_unavailable")

    async with aclosing(qa_units._publish_dynamic_plan((unit,), authorize, unexpected_gateway)) as stream:
        if revoked:
            with pytest.raises(qa_units.QaUnitRejected) as caught:
                await anext(stream)
            assert caught.value.reason == "catalog_grant_unavailable"
        else:
            chunk = await anext(stream)
            assert chunk.text == unit.render
            assert chunk.publication_kind == "authorized_unit"
            assert chunk.unit_ref.unit_id == "DEV-PLAZO"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)


@pytest.mark.anyio
async def test_dynamic_cancellation_closes_the_stream_and_drops_unconfirmed_id():
    plan = (
        CatalogUnit("DEV-COSTE", "1.0", "La marca paga solo en QA.", "qa-i14-devoluciones", "coste", "es", "qa"),
        CatalogUnit("DEV-PLAZO", "1.0", "21 días solo en QA.", "qa-i14-devoluciones", "plazo", "es", "qa"),
    )
    source = FixtureStream(["DEV-COSTE\n", "DEV-PL"], wait=True)
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=source,
    ))
    settings = RAGSettings(DEFAULT_BASE_URL, "synthetic", "unused", "fixture", "unused")
    gateway = qa_units._DynamicQaUnitGateway(
        settings, plan, async_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    async with aclosing(qa_units._publish_dynamic_plan(plan, _allow, lambda: gateway)) as selection:
        first = await anext(selection)
        assert first.unit_ref.unit_id == "DEV-COSTE" and not source.closed.is_set()
        pending = asyncio.create_task(anext(selection))
        await asyncio.wait_for(source.waiting.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert source.closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await anext(selection)
