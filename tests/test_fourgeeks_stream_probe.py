"""Controlled transport checks for the opt-in provider diagnostic."""

import asyncio
import json

import httpx
import pytest

from scripts import fourgeeks_stream_probe


DELTA_ONE = b'data: {"choices":[{"delta":{"content":"apple"}}]}\r\n\r\n'
DELTA_TWO = b'data: {"choices":[{"delta":{"content":"pear"}}]}\n\n'
TERMINAL = b'data: [DONE]\n\n'


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, batches, *, block=False):
        self.batches = batches
        self.block = block
        self.closed = False
        self.cancelled_during_read = False

    async def __aiter__(self):
        for batch in self.batches:
            yield batch
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled_during_read = True
                raise

    async def aclose(self):
        self.closed = True


def _run(stream, *, media_type="text/event-stream", status=200):
    calls = []

    def respond(request):
        calls.append(request.url)
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["max_tokens"] == 96
        return httpx.Response(status, headers={"content-type": media_type}, stream=stream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fourgeeks_stream_probe.probe(
                client, "https://gateway.example.test/v1/chat/completions", "fixture", "fixture"
            )

    observation = asyncio.run(run())
    assert len(calls) == 1
    assert stream.closed
    return observation


def test_cancel_waiting_for_more_data_closes_response_without_more_deltas():
    stream = ControlledStream([DELTA_ONE[:14], DELTA_ONE[14:], DELTA_TWO], block=True)
    observation = _run(stream)
    assert observation.status == "local_cancellation_observed"
    assert observation.content_deltas == observation.reads_with_content == 2
    assert observation.task_cancelled and observation.response_closed
    assert stream.cancelled_during_read
    assert observation.deltas_after_cancel_request == 0
    assert not observation.terminal_seen


@pytest.mark.parametrize("batches,read_count", [
    ([DELTA_ONE + DELTA_TWO + TERMINAL], 1),
    ([DELTA_ONE, DELTA_TWO + TERMINAL], 2),
])
def test_terminal_in_same_read_as_second_delta_is_not_cancellation(batches, read_count):
    observation = _run(ControlledStream(batches))
    assert observation.content_deltas == 2
    assert observation.reads_with_content == read_count
    assert observation.terminal_seen and observation.response_closed
    assert not observation.task_cancelled
    assert observation.status == "terminal_before_cancellation"


@pytest.mark.parametrize("status,media,expected", [
    (401, "application/json", "http_error"),
    (200, "application/json", "not_event_stream"),
])
def test_rejects_status_or_response_format_before_consuming_content(status, media, expected):
    observation = _run(ControlledStream([DELTA_ONE]), media_type=media, status=status)
    assert observation.status == expected
    assert observation.http_status == status
    assert observation.content_deltas == 0


@pytest.mark.parametrize("args,key,base,expected", [
    (["--execute"], None, None, "missing_credential"),
    ([], "synthetic-secret", None, "ready"),
    (["--execute"], "synthetic-secret", "http://example.test/v1", "invalid_configuration"),
])
def test_cli_preflight_discloses_only_status_and_never_calls_network(
    args, key, base, expected, monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", ["probe", *args])
    for name, value in (("FOUR_GEEKS_API_KEY", key), ("FOUR_GEEKS_BASE_URL", base)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(fourgeeks_stream_probe, "_live", lambda *_: pytest.fail("network called"))
    assert fourgeeks_stream_probe.main() == (0 if expected == "ready" else 2)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": expected, "requests": 0}
    assert captured.err == ""
