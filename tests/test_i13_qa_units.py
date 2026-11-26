from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import datetime, timezone
from io import StringIO
import json
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
from pydantic import SecretStr
import pytest

from services.api.access_control import AccessContext
from services.api.auth_models import Role, UserInDB
from services.api.chat import qa_units, router
from services.api.chat.contracts import TokenChunkData, TokenChunkEvent
from services.api.chat.coordinator import ProducerUnavailable
from services.api.chat.qa_units_scope import QaUnitScope, qa_unit_scope
from services.api.chat.store import _snapshot_message
from services.api.support_oidc import SupportIdentity
from services.rag.config import DEFAULT_BASE_URL, RAGSettings
from services.rag.contracts import RAGProviderError


USER = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fixture_env(monkeypatch):
    values = {
        "TRACKFLOW_MCP_QA": "1",
        "TRACKFLOW_I13_QA_UNITS": "1",
        "TRACKFLOW_I13_QA_UNITS_USER_ID": USER,
        "TRACKFLOW_I13_QA_UNITS_CLIENT_ID": CLIENT,
        "DATABASE_URL": "postgresql://trackflow_ws_qa@127.0.0.1:55440/trackflow_ws_qa",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _access(client=CLIENT):
    return AccessContext(USER, Role.manager, frozenset({client}), frozenset())


def _identity(role=Role.manager, user=USER):
    return SupportIdentity(
        UserInDB(id=user, role=role, email="qa@example.invalid", hashed_password="unused"),
        "fixture-subject",
        frozenset(),
        SecretStr("fixture-not-a-token"),
    )


async def _allow():
    pass


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, frames, *, wait=False, terminal=True):
        self.frames = frames
        self.wait = wait
        self.terminal = terminal
        self.closed = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.frames:
            choice = (
                {"delta": {"content": chunk[0]}, "finish_reason": chunk[1]}
                if isinstance(chunk, tuple) else {"delta": {"content": chunk}}
            )
            encoded = json.dumps({"choices": [choice]})
            yield f"data: {encoded}\n\n".encode()
        if self.wait:
            self.waiting.set()
            await self.release.wait()
        if self.terminal:
            yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed.set()


def _gateway(source):
    def respond(request):
        assert str(request.url) == f"{DEFAULT_BASE_URL}/chat/completions"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert "U1\nU2\nU3" in payload["messages"][0]["content"]
        assert "Fuente sint" not in request.content.decode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    return qa_units._QaUnitGateway(
        RAGSettings(DEFAULT_BASE_URL, "fixture", "embed", "generate", "unused"),
        async_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


def test_qa_opt_in_rejects_off_profile_actor_and_other_questions(monkeypatch):
    _fixture_env(monkeypatch)
    assert qa_units.requested_qa_unit_scope(_identity(), qa_units.QA_UNITS_QUESTION) == QaUnitScope(
        USER, CLIENT,
    )
    assert qa_units.requested_qa_unit_scope(_identity(Role.user), qa_units.QA_UNITS_QUESTION) is None
    assert qa_units.requested_qa_unit_scope(_identity(user=OTHER), qa_units.QA_UNITS_QUESTION) is None
    assert qa_units.requested_qa_unit_scope(_identity(), "another question") is None
    assert not qa_unit_scope().allows(_access(OTHER))
    monkeypatch.setenv("TRACKFLOW_I13_QA_UNITS", "0")
    assert qa_unit_scope() is None
    monkeypatch.setenv("TRACKFLOW_I13_QA_UNITS", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://trackflow_ws_qa@127.0.0.1:5441/trackflow_ws_qa")
    assert qa_unit_scope() is None


@pytest.mark.anyio
async def test_fourgeeks_four_frame_selection_finishes_with_stop_without_done():
    source = FixtureStream(["U1\n", "U2\n", "U3", ("", "stop")], terminal=False)
    gateway = _gateway(source)
    async with aclosing(qa_units._selection_units(gateway, _allow)) as stream:
        assert (await anext(stream)).text == qa_units._PLAN[0].render
        assert (await anext(stream)).text == "\n\n" + qa_units._PLAN[1].render
        assert not source.closed.is_set()
        assert (await anext(stream)).text == "\n\n" + qa_units._PLAN[2].render
        assert source.closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert gateway.last_generation_status_code == 200
    assert gateway.generation_request_count == 1


@pytest.mark.anyio
async def test_unit_completed_in_a_stop_frame_is_not_released_before_stream_close():
    source = FixtureStream(["U", ("1\nU2\nU3\n", "stop")])
    async with aclosing(qa_units._selection_units(_gateway(source), _allow)) as stream:
        assert (await anext(stream)).text == qa_units._PLAN[0].render
        assert source.closed.is_set()
        assert len([unit async for unit in stream]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("final_frame", ["3", ("3", "stop")])
async def test_valid_terminal_can_delimit_the_exact_final_id(final_frame, monkeypatch):
    source = FixtureStream(["U1\n", "U2\nU", final_frame])
    output = StringIO()
    monkeypatch.setattr(qa_units._LOGGER.handlers[0], "stream", output)
    async with aclosing(qa_units._selection_units(_gateway(source), _allow, session_id=USER)) as stream:
        for unit in qa_units._PLAN[:2]:
            assert (await anext(stream)).text == ("" if unit.id == "U1" else "\n\n") + unit.render
            assert not source.closed.is_set()
        assert (await anext(stream)).text == "\n\n" + qa_units._PLAN[-1].render
        assert source.closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    entries = output.getvalue().splitlines()
    assert [line.split("stage=", 1)[1].split(" |", 1)[0] for line in entries] == [
        "provider_request_started", "fanout_completed", "fanout_completed",
        "provider_terminal_and_closed", "fanout_completed",
    ]
    assert all("provider_terminal=False" in line for line in entries[:3])
    assert all("provider_terminal=True" in line for line in entries[3:])
    assert entries[0].endswith("http_status=None")
    assert all(line.endswith("http_status=200") for line in entries[1:])


@pytest.mark.anyio
async def test_qa_trace_distinguishes_preflight_from_rejected_provider_content(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(qa_units._LOGGER.handlers[0], "stream", output)
    source = FixtureStream(["untrusted provider content\n"])
    gateway = _gateway(source)

    async def deny():
        raise ProducerUnavailable("synthetic auth diagnostic")

    async with aclosing(qa_units._selection_units(gateway, deny, session_id=USER)) as stream:
        with pytest.raises(ProducerUnavailable):
            await anext(stream)
    preflight = output.getvalue()
    assert "stage=access_rejected" in preflight
    assert "provider_request_started" not in preflight
    assert "synthetic auth diagnostic" not in preflight

    output.seek(0)
    output.truncate()
    async with aclosing(qa_units._selection_units(gateway, _allow, session_id=USER)) as stream:
        with pytest.raises(ProducerUnavailable):
            await anext(stream)
    rejected = output.getvalue()
    assert "stage=provider_request_started" in rejected
    assert "stage=rejected" in rejected
    assert "reason=unexpected_unit | http_status=200" in rejected
    assert "untrusted provider content" not in rejected
    assert source.closed.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("frames", "terminal", "error"),
    [
        (["U1\n", "U2\n"], True, ProducerUnavailable),
        (["U1\n", "U2\n", "U3 extra"], True, ProducerUnavailable),
        (["U1\n", "U2\n", "U3"], False, RAGProviderError),
        (["U1\n", "U2\n", "U3", ("", "length")], False, RAGProviderError),
        (["U1\n", "U2\n", "U4", ("", "stop")], False, ProducerUnavailable),
    ],
)
async def test_partial_survives_but_invalid_or_unterminated_final_id_is_never_released(frames, terminal, error):
    source = FixtureStream(frames, terminal=terminal)
    async with aclosing(qa_units._selection_units(_gateway(source), _allow)) as stream:
        assert "QA-I13-DEV/U1" in (await anext(stream)).text
        assert "QA-I13-DEV/U2" in (await anext(stream)).text
        with pytest.raises(error):
            await anext(stream)
        assert source.closed.is_set()


@pytest.mark.anyio
async def test_original_selection_is_incremental_and_abort_closes_the_http_stream():
    source = FixtureStream(["U", "1\nU"], wait=True)
    gateway = _gateway(source)
    try:
        async with aclosing(qa_units._selection_units(gateway, _allow)) as units:
            first = await asyncio.wait_for(anext(units), 2)
            assert first.publication_kind == "authorized_unit"
            assert first.text == qa_units._PLAN[0].render
            assert not source.closed.is_set()
            next_unit = asyncio.create_task(anext(units))
            await asyncio.wait_for(source.waiting.wait(), 2)
            assert not next_unit.done()
            next_unit.cancel()
            with pytest.raises(asyncio.CancelledError):
                await next_unit
            assert source.closed.is_set()
            with pytest.raises(StopAsyncIteration):
                await anext(units)
    finally:
        gateway.close()


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["U1\n", "U99\n", "U2", "U3\n"])
async def test_late_invalid_unit_does_not_erase_the_first_unit(suffix):
    source = FixtureStream(["U1\n", suffix])
    gateway = _gateway(source)
    try:
        async with aclosing(qa_units._selection_units(gateway, _allow)) as units:
            assert (await anext(units)).text == qa_units._PLAN[0].render
            with pytest.raises(ProducerUnavailable):
                await anext(units)
        assert source.closed.is_set()
    finally:
        gateway.close()


@pytest.mark.anyio
async def test_complete_stream_checks_each_unit_and_combination_before_release(monkeypatch):
    source = FixtureStream(["U1\nU2\nU3\n"])
    gateway = _gateway(source)
    real_check = qa_units._validate_text
    checked = []

    def validate(text):
        real_check(text)
        checked.append(text)

    monkeypatch.setattr(qa_units, "_validate_text", validate)
    try:
        async with aclosing(qa_units._selection_units(gateway, _allow)) as units:
            received = ""
            async for chunk in units:
                received += chunk.text
                assert checked[-1] == received
                assert checked[-2] == chunk.text.removeprefix("\n\n")
        assert len(checked) == 6
        assert source.closed.is_set()
    finally:
        gateway.close()


@pytest.mark.anyio
async def test_qa_unit_live_delivery_does_not_expose_body_to_an_hs256_only_sink(monkeypatch):
    _fixture_env(monkeypatch)
    event = TokenChunkEvent(data=TokenChunkData(
        session_id="fixture", generation_id="gen", message_id="msg",
        sequence=1, cursor=1, token=qa_units._PLAN[0].render,
        publication_kind="authorized_unit",
    ))
    sink = SimpleNamespace()

    async def unexpected_send(_payload):
        pytest.fail("unauthorized body was sent")

    async def receive():
        return event

    sink.send_json = unexpected_send
    with pytest.raises(HTTPException) as failure:
        await router._sender(
            sink, SimpleNamespace(receive=receive), after_cursor=0,
            local=SimpleNamespace(revalidate=lambda _session: _access()), session_id="fixture",
        )
    assert failure.value.status_code == 403


def test_snapshot_only_recovers_unit_body_with_explicit_support_authorization():
    row = {
        "message_id": "msg",
        "role": "assistant",
        "content": qa_units._PLAN[0].render,
        "status": "interrupted",
        "sequence": 1,
        "created_at": datetime.now(timezone.utc),
        "answer_kind": None,
        "publication_kind": "authorized_unit",
    }

    class Row(dict):
        __getattr__ = dict.__getitem__

    denied = _snapshot_message(Row(row))
    assert denied.content == ""
    assert denied.publication_kind is None
    allowed = _snapshot_message(Row(row), include_authorized_units=True)
    assert allowed.content == row["content"]
    assert allowed.publication_kind == "authorized_unit"
