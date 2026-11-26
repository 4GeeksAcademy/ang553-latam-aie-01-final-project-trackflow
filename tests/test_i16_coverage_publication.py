import asyncio
from contextlib import aclosing

import httpx
import pytest

from services.api.chat import qa_units
from services.api.chat.coordinator import CoverageUpdate
from services.api.chat.qa_unit_catalog import CatalogUnit, CoveragePlan
from services.rag.config import DEFAULT_BASE_URL, RAGSettings
from tests.test_i13_qa_units import FixtureStream, _allow


COST = CatalogUnit("DEV-COSTE", "1.0", "Coste simulado si se aprueba la etiqueta.", "qa-returns", "coste", "es", "qa")
WINDOW = CatalogUnit("DEV-PLAZO", "1.0", "21 días solo en QA.", "qa-returns", "plazo", "es", "qa")


def plan(requested, units, required=None):
    mapping = {"DEV-COSTE": "return_cost", "DEV-PLAZO": "return_window"}
    topics = frozenset(mapping[unit.id] for unit in units)
    return CoveragePlan(
        requested=requested, evidence_topics=topics, authorized_topics=topics, units=units,
        unit_topics=tuple((unit.id, mapping[unit.id]) for unit in units),
        required=tuple(unit.id for unit in units) if required is None else required,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


def gateway_for(source, selected, started):
    def make():
        started.append(True)
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=source,
        ))
        settings = RAGSettings(DEFAULT_BASE_URL, "synthetic", "unused", "fixture", "unused")
        return qa_units._DynamicQaUnitGateway(
            settings, selected.units, async_client_factory=lambda: httpx.AsyncClient(transport=transport),
        )
    return make


@pytest.mark.anyio
@pytest.mark.parametrize("omits", [False, True])
async def test_composed_coverage_completes_only_from_confirmed_units(omits):
    selected = plan(("return_cost", "return_window"), (COST, WINDOW))
    frames = ["DEV-COSTE\n"] if omits else ["DEV-COSTE\n", "DEV-PLAZO"]
    source = FixtureStream([*frames, ("", "stop")], terminal=False)
    started = []
    async with aclosing(qa_units._publish_coverage_plan(selected, _allow, gateway_for(source, selected, started))) as chunks:
        first = await anext(chunks)
        assert first.unit_coverage.status == "partial"
        assert first.unit_coverage.missing_topics == ("return_window",)
        second = await anext(chunks)
        assert second.text == "\n\n" + WINDOW.render
        assert second.unit_coverage.status == "complete"
        assert source.closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await anext(chunks)
    assert started == [True]


@pytest.mark.anyio
@pytest.mark.parametrize(("requested", "units", "status", "missing"), [
    (("return_window",), (WINDOW,), "complete", ()),
    (("return_window", "return_inspection"), (WINDOW,), "partial", ("return_inspection",)),
    (("return_inspection",), (), "none", ("return_inspection",)),
    (None, (), "indeterminate", ()),
])
async def test_coverage_projection_without_provider(requested, units, status, missing):
    selected = plan(requested, units)

    def fail_gateway():
        pytest.fail("local coverage must never open a selector request")

    async with aclosing(qa_units._publish_coverage_plan(selected, _allow, fail_gateway)) as chunks:
        update = await anext(chunks)
        coverage = update.coverage if isinstance(update, CoverageUpdate) else update.unit_coverage
        assert coverage.status == status and coverage.missing_topics == missing
        with pytest.raises(StopAsyncIteration):
            await anext(chunks)


@pytest.mark.anyio
async def test_omitted_non_unique_alternative_does_not_get_supplemented():
    selected = plan(("return_cost", "return_window"), (COST, WINDOW), required=("DEV-COSTE",))
    source = FixtureStream(["DEV-COSTE\n", ("", "stop")], terminal=False)
    async with aclosing(qa_units._publish_coverage_plan(selected, _allow, gateway_for(source, selected, []))) as chunks:
        first = await anext(chunks)
        assert first.unit_coverage.status == "partial"
        with pytest.raises(StopAsyncIteration):
            await anext(chunks)


@pytest.mark.anyio
async def test_revocation_and_interrupt_never_supplement_pending_coverage():
    selected = plan(("return_cost", "return_window"), (COST, WINDOW))
    source = FixtureStream(["DEV-COSTE\n", ("", "stop")], terminal=False)

    async def authorize():
        if source.closed.is_set():
            raise qa_units.QaUnitRejected("catalog_grant_unavailable")

    async with aclosing(qa_units._publish_coverage_plan(selected, authorize, gateway_for(source, selected, []))) as chunks:
        first = await anext(chunks)
        assert first.unit_coverage.status == "partial"
        with pytest.raises(qa_units.QaUnitRejected) as denied:
            await anext(chunks)
        assert denied.value.reason == "catalog_grant_unavailable"

    active = FixtureStream(["DEV-COSTE\n"], wait=True)
    async with aclosing(qa_units._publish_coverage_plan(selected, _allow, gateway_for(active, selected, []))) as chunks:
        first = await anext(chunks)
        assert first.unit_coverage.status == "partial"
        pending = asyncio.create_task(anext(chunks))
        await asyncio.wait_for(active.waiting.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert active.closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await anext(chunks)
