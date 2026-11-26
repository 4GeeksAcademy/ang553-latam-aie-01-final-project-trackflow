"""Opt-in QA of LangGraph abort against the real gateway with synthetic retrieval."""

import asyncio
from contextlib import contextmanager
import os
from uuid import uuid4

import httpx
import pytest

from agents.customer_support.external_graph import ExternalSupportAgent
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import DEFAULT_BASE_URL, DEFAULT_GENERATION_MODEL, RAGSettings
from services.rag.resilience import BudgetedGenerationProvider, QueryDeadline
from services.rag.service import RAGService
from tests.pipelines.test_customer_support_external_tools import StubRAGService, access


@pytest.mark.parametrize("live", [False, True], ids=["controlled", "live"])
def test_fourgeeks_agent_abort(live):
    if live and os.getenv("TRACKFLOW_FOURGEEKS_AGENT_QA") != "1":
        pytest.skip("Live agent gateway QA is opt-in")
    key = os.getenv("FOUR_GEEKS_API_KEY", "").strip() if live else "fixture"
    if not key:
        pytest.fail("FOUR_GEEKS_API_KEY is missing from the QA process")
    try:
        base = httpx.URL(
            os.getenv("FOUR_GEEKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
            if live else "https://gateway.example.test/v1"
        )
    except httpx.InvalidURL:
        pytest.fail("A valid HTTPS FourGeeks base URL is required", pytrace=False)
    if base.scheme != "https" or not base.host or base.userinfo or base.query or base.fragment:
        pytest.fail("A valid HTTPS FourGeeks base URL is required")

    async def scenario():
        first_read = asyncio.Event()
        abort = asyncio.Event()
        observed: list[httpx.Response] = []
        deadline = QueryDeadline(22)

        class ReadObserver(httpx.AsyncByteStream):
            def __init__(self, source):
                self.source = source

            async def __aiter__(self):
                async for batch in self.source:
                    if batch:
                        first_read.set()
                    yield batch

            async def aclose(self):
                await self.source.aclose()

        async def observe(response):
            observed.append(response)
            if response.status_code == 200:
                response.stream = ReadObserver(response.stream)

        class ControlledSSE(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n'
                await asyncio.Event().wait()

        def respond(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ControlledSSE()
            )

        class BoundedGateway(FourGeeksHTTPClient):
            def _configured_generation_payload(self, *args, **kwargs):
                return {**super()._configured_generation_payload(*args, **kwargs), "max_tokens": 96}

        gateway = BoundedGateway(
            RAGSettings(
                base_url=str(base), api_key=key, embedding_model="unused",
                generation_model=(
                    os.getenv("RAG_GENERATION_MODEL", DEFAULT_GENERATION_MODEL) if live else "fixture"
                ),
                qdrant_url="unused",
            ),
            async_client_factory=lambda: httpx.AsyncClient(
                follow_redirects=False, trust_env=False, event_hooks={"response": [observe]},
                transport=None if live else httpx.MockTransport(respond),
            ),
        )

        class SyntheticRetrieval(RAGService):
            def retrieve(self, question, k, min_score, *, request_id=None):
                return StubRAGService().retrieve(question, k, min_score, request_id=request_id)

            def generate_answer(self, *_args):
                pytest.fail("Agent used synchronous generation")

        rag = SyntheticRetrieval(
            embedding_provider=None,
            generation_provider=BudgetedGenerationProvider(gateway, deadline, 18, 2),
            vector_store=None,
        )

        @contextmanager
        def rag_factory():
            try:
                yield rag
            finally:
                gateway.close()

        agent = ExternalSupportAgent()
        run_id = str(uuid4())
        operation = asyncio.create_task(agent.execute(
            "Explain the standard returns policy in several numbered steps.",
            session=object(), access=access(), deadline=deadline,
            rag_factory=rag_factory, abort_event=abort, run_id=run_id,
        ))
        reading = asyncio.create_task(first_read.wait())
        try:
            await asyncio.wait({operation, reading}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
            if not observed or observed[0].status_code != 200:
                status = observed[0].status_code if observed else None
                pytest.fail(f"Gateway did not open an HTTP 200 stream (status={status})")
            if not first_read.is_set() or operation.done() or observed[0].is_closed:
                pytest.fail("Inconclusive: no open response remained when abort could be requested")
            abort.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 3)
            assert observed[0].is_closed
            assert gateway.generation_request_count == len(observed) == 1
            assert agent.tracer.get(run_id)["outcome"] == "cancelled"
            assert await agent.checkpointer.aget_tuple(
                {"configurable": {"thread_id": run_id}}
            ) is None
        finally:
            abort.set()
            reading.cancel()
            await asyncio.gather(operation, reading, return_exceptions=True)

    asyncio.run(scenario())
