"""Offline contracts for cancellable FourGeeks SSE generation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agents.customer_support import graph as support_graph
from agents.customer_support.guardrail_contracts import OutputValidationStatus
from agents.customer_support.output_guardrails import validate_http_response
from agents.customer_support.graph import SupportTurnAborted
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import RAGSettings
from services.rag.contracts import RAGProviderError


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, batches: list[bytes], *, block: bool = False) -> None:
        self.batches = batches
        self.block = block
        self.closed = False
        self.cancelled = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for batch in self.batches:
            yield batch
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def aclose(self) -> None:
        self.closed = True


def _client(stream: ControlledStream, observed: dict) -> FourGeeksHTTPClient:
    settings = RAGSettings(
        base_url="https://gateway.example/v1",
        api_key="synthetic-test-key",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant:6333",
        request_timeout_seconds=2,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=stream,
        )

    return FourGeeksHTTPClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
        async_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ),
    )


def test_async_generation_reuses_prompt_payload_and_returns_complete_sse_answer():
    stream = ControlledStream([
        b'data: {"choices":[{"delta":{"content":"safe "}}]}\r\n\r\n'
        b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n',
    ])
    observed = {}
    client = _client(stream, observed)

    answer = asyncio.run(client.generate_async("question", "context"))

    assert answer == "safe answer"
    assert stream.closed
    payload = observed["payload"]
    assert payload["stream"] is True
    assert payload["temperature"] == 0
    assert payload["model"] == "generation-model"
    assert "untrusted data" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "Question:\nquestion\n\ncontext"


def test_private_delta_observer_sees_original_content_before_terminal():
    class AwaitingTerminal(ControlledStream):
        def __init__(self):
            super().__init__([
                b'data: {"choices":[{"delta":{"content":"  Este es"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":""}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":" un env\\u00edo.  "}}]}\n\n',
            ])
            self.release = asyncio.Event()

        async def __aiter__(self):
            async for batch in super().__aiter__():
                yield batch
            await self.release.wait()
            yield b"data: [DONE]\n\n"

    async def scenario():
        stream = AwaitingTerminal()
        received = []
        observed = asyncio.Event()

        async def observe(delta):
            received.append(delta)
            if len(received) == 2:
                observed.set()

        task = asyncio.create_task(
            _client(stream, {}).generate_async("question", "context", delta_observer=observe)
        )
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert not task.done()
        assert not stream.closed
        assert received == ["  Este es", " un envío.  "]
        stream.release.set()
        assert await task == "  Este es un envío.  "
        assert stream.closed

    asyncio.run(scenario())


def test_private_delta_observer_is_cancelled_and_response_closes():
    async def scenario():
        stream = ControlledStream([
            b'data: {"choices":[{"delta":{"content":"prefix"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"unused"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])
        entered = asyncio.Event()
        received = []

        async def observe(delta):
            received.append(delta)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            _client(stream, {}).generate_async("question", "context", delta_observer=observe)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert received == ["prefix"]
        assert stream.closed

    asyncio.run(scenario())


def test_private_delta_observer_does_not_receive_content_over_limit():
    stream = ControlledStream([
        b'data: {"choices":[{"delta":{"content":"' + b"x" * 16001 + b'"}}]}\n\n',
    ])
    received = []

    async def observe(delta):
        received.append(delta)

    with pytest.raises(RAGProviderError) as caught:
        asyncio.run(_client(stream, {}).generate_async(
            "question", "context", delta_observer=observe,
        ))
    assert caught.value.category == "validation_error"
    assert received == []
    assert stream.closed


def test_async_generation_rejects_truncated_sse_without_terminal():
    stream = ControlledStream([
        b'data: {"choices":[{"delta":{"content":"must not publish"}}]}',
    ])
    client = _client(stream, {})

    with pytest.raises(RAGProviderError) as caught:
        asyncio.run(client.generate_async("question", "context"))

    assert caught.value.category == "validation_error"
    assert stream.closed


def test_guardrail_blocks_sensitive_pattern_split_across_real_parser_frames():
    stream = ControlledStream([
        b'data: {"choices":[{"delta":{"content":"Bearer "}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"abcdefghijkl"}}]}\n\n',
        b"data: [DONE]\n\n",
    ])
    answer = asyncio.run(_client(stream, {}).generate_async("question", "context"))

    def validate(content):
        return validate_http_response({
            "answer": content, "route": "clarification", "partial": False,
            "needs_clarification": False,
        }).status

    assert validate("Bearer ") is OutputValidationStatus.safe
    assert validate("abcdefghijkl") is OutputValidationStatus.safe
    assert validate(answer) is OutputValidationStatus.blocked
    assert stream.closed


def test_async_generation_closes_oversize_frame_without_waiting_for_newline():
    stream = ControlledStream([b"data: " + b"x" * 65536], block=True)
    client = _client(stream, {})

    with pytest.raises(RAGProviderError) as caught:
        asyncio.run(client.generate_async("question", "context"))

    assert caught.value.category == "validation_error"
    assert stream.closed


def test_async_generation_cancel_closes_response_and_propagates_cancel():
    stream = ControlledStream([
        b'data: {"choices":[{"delta":{"content":"prefix"}}]}\n\n',
    ], block=True)
    client = _client(stream, {})

    async def scenario():
        task = asyncio.create_task(client.generate_async("question", "context"))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert stream.cancelled
    assert stream.closed


def test_knowledge_node_abort_cancels_and_awaits_generation():
    abort = asyncio.Event()
    state = {"question": "question", "context": ("reference",)}
    observed = {"closed": False}

    class Service:
        async def generate_answer_async(self, *_args):
            try:
                await asyncio.Event().wait()
            finally:
                observed["closed"] = True

    runtime = SimpleNamespace(
        service=Service(),
        conversation_context=None,
        abort_event=abort,
    )

    async def scenario():
        task = asyncio.create_task(
            support_graph.generate_answer_async(state, runtime)
        )
        await asyncio.sleep(0)
        abort.set()
        with pytest.raises(SupportTurnAborted):
            await task

    asyncio.run(scenario())
    assert observed["closed"] is True


def test_http_mode_without_abort_event_preserves_sync_generation():
    observed = {"sync": 0}

    class Service:
        def generate_answer(self, *_args):
            observed["sync"] += 1
            return "sync answer"

        async def generate_answer_async(self, *_args):
            raise AssertionError("HTTP compatibility path must remain synchronous")

    runtime = SimpleNamespace(
        service=Service(),
        conversation_context=None,
        abort_event=None,
    )
    updates, transition = asyncio.run(
        support_graph.generate_answer_async(
            {"question": "question", "context": ("reference",)}, runtime
        )
    )
    assert updates == {"answer": "sync answer"}
    assert transition == "finalize_answer"
    assert observed == {"sync": 1}
