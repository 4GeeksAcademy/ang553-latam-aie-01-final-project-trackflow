"""HTTP/SSE parity for natural Support answers and guarded abstention."""

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr

from agents.customer_support.external_graph import ExternalSupportAgent
from agents.customer_support.guardrail_contracts import OutputValidationStatus
from agents.customer_support.output_guardrails import SAFE_OUTPUT_MESSAGE, validate_http_response
from services.api.auth_models import Role, UserInDB
from services.api.routes import support
from services.api.support_oidc import SupportIdentity
from services.rag.chunking import canonical_chunks_by_id
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import RAGSettings
from services.rag.contracts import RAGProviderError, RetrievalResult
from services.rag.knowledge_coverage import (
    INSUFFICIENT_KNOWLEDGE,
    interpret_evidence_selection,
)
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService


QUESTION = (
    "Explica detalladamente el proceso estándar de devoluciones y el tratamiento "
    "de devoluciones internacionales según las políticas de TrackFlow."
)
POLICY = {
    chunk.section: chunk
    for chunk in canonical_chunks_by_id().values()
    if chunk.source_document == "returns-policy"
}
STANDARD = POLICY["standard-return-process"]
NATURAL_ES = (
    "Solicita la devolución en el portal de la marca. Si el motivo cumple las "
    "reglas configuradas, TrackFlow genera la etiqueta y el almacén revisa el "
    "producto recibido. El plazo estándar es de 30 días desde la entrega, "
    "salvo que el contrato de la marca establezca otro."
)
NATURAL_EN = (
    "Yes. TrackFlow normally allows 30 days from delivery, unless the brand "
    "set a different period in its contract."
)


class ControlledSSE(httpx.AsyncByteStream):
    def __init__(self, segments, *, block=False):
        self.segments = segments
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for segment in self.segments:
            payload = {"choices": [{"delta": {"content": segment}}]}
            yield ("data: " + json.dumps(payload) + "\n\n").encode()
        if self.block:
            self.waiting.set()
            await asyncio.Future()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class VectorStore:
    def __init__(self, scores):
        self.scores = scores

    def search(self, *_args, **_kwargs):
        return [
            (POLICY[section].point_id, score, POLICY[section].payload())
            for section, score in self.scores
        ]


@pytest.fixture
def through_support(monkeypatch):
    def execute(
        segments, *, question=QUESTION, scores=None, block=False, finish_reason=None,
        provider_error=False,
    ):
        calls = []
        stream = ControlledSSE(segments, block=block)

        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if payload.get("stream"):
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream,
                )
            body = {
                "choices": [{
                    "message": {"content": "".join(segments)},
                    "finish_reason": finish_reason,
                }],
            }
            if provider_error:
                body["error"] = {"message": "Synthetic provider failure"}
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(respond)
        settings = RAGSettings(
            base_url="https://provider.example/v1",
            api_key="test-only",
            embedding_model="test-embedding",
            generation_model="test-generation",
            qdrant_url="http://qdrant.example",
            request_timeout_seconds=3,
        )

        @contextmanager
        def runtime(*, deadline):
            assert deadline.remaining() > 0
            with httpx.Client(transport=transport) as client:
                gateway = FourGeeksHTTPClient(
                    settings,
                    client,
                    async_client_factory=lambda: httpx.AsyncClient(transport=transport),
                )
                yield RAGService(
                    SimpleNamespace(embed=lambda _query: (1.0,)),
                    gateway,
                    VectorStore(scores if scores is not None else [
                        ("standard-return-process", 0.5919918),
                        ("return-window-and-costs", 0.518673),
                        ("international-returns", 0.51322764),
                    ]),
                    default_min_score=0.57,
                )

        monkeypatch.setattr(support, "rag_service_context", runtime)
        user = UserInDB(
            id="support-test", email="support@example.com",
            hashed_password="test-only", role=Role.admin,
        )
        identity = SupportIdentity(user, "subject", frozenset(), SecretStr("token"))
        session = SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            rollback=lambda: None,
        )
        memory = SimpleNamespace(
            bind_conversation=lambda *_args, **_kwargs: None,
            get_pending=lambda *_args: None,
        )
        conversation_id = uuid4()

        async def run(abort_event=None):
            request = SimpleNamespace(state=SimpleNamespace(support_abort_event=abort_event))
            return await support.query_support(
                support.SupportQueryRequest(question=question, conversation_id=conversation_id),
                request, Response(), identity, session, ExternalSupportAgent(),
                SimpleNamespace(lookup_ticket=None, lookup_inventory_product=None),
                memory,
            )

        return run, calls, stream

    return execute


@pytest.mark.parametrize(("question", "answer", "scores", "sections"), [
    (
        "¿Cómo funciona la devolución y cuál es el plazo estándar?",
        NATURAL_ES,
        [("standard-return-process", 0.72), ("return-window-and-costs", 0.68)],
        ["standard-return-process", "return-window-and-costs"],
    ),
    (
        "Does TrackFlow have a standard return window of 30 days?",
        NATURAL_EN,
        [("return-window-and-costs", 0.74)],
        ["return-window-and-costs"],
    ),
])
def test_natural_prose_and_retrieved_sources_match_in_json_and_sse(
    through_support, question, answer, scores, sections,
):
    run, calls, stream = through_support(
        [answer[:20], answer[20:]],
        question=question,
        scores=[*scores, ("international-returns", 0.51)],
    )

    async def compare():
        return await run(), await run(asyncio.Event())

    http, ws = asyncio.run(compare())
    assert http.model_dump() == ws.model_dump()
    assert http.route == "knowledge"
    assert http.partial is False
    assert "coverage" not in http.knowledge.model_dump()
    assert http.knowledge.status == "ok"
    assert http.knowledge.insufficient_context is False
    assert http.answer == http.knowledge.answer == answer
    assert all(answer != POLICY[section].text for section in sections)
    assert [source.section for source in http.knowledge.sources] == sections
    assert "response_format" not in calls[0]
    assert "international-returns" not in calls[0]["messages"][1]["content"]
    system = calls[0]["messages"][0]["content"]
    assert "Answer in the language of the current user question" in system
    assert "begin with an unambiguous Yes or No" in system
    assert "material qualifier" in system
    assert "return a single JSON object" not in system
    assert calls[1] == {**calls[0], "stream": True}
    assert stream.closed


@pytest.mark.parametrize("segments", [
    [NATURAL_ES + " INSUFFICIENT_", "CONTEXT"],
    [NATURAL_ES + " pass", "word = SyntheticValue123"],
    [NATURAL_ES, "\x00"],
])
def test_unsafe_prose_fails_closed_in_both_transports(
    through_support, segments,
):
    run, calls, stream = through_support(segments)

    async def compare():
        for event in (None, asyncio.Event()):
            result = await run(event)
            assert result.answer == SAFE_OUTPUT_MESSAGE
            assert result.knowledge is None

    asyncio.run(compare())
    assert len(calls) == 2 and stream.closed


def test_existing_source_identity_does_not_authorize_forged_claims():
    poisoned = replace(STANDARD, text="Refunds have a one-day deadline.")
    for hit in (RetrievalResult(poisoned, 0.9), RetrievalResult(STANDARD, 0.9, "complementary")):
        with pytest.raises(RAGProviderError, match="invalid evidence selection"):
            interpret_evidence_selection(
                '{"coverage":"partial","evidence":[0]}', [hit], language="es",
            )

    answer = "Refunds have a one-day deadline."
    fabricated = support.SupportQueryResponse(
        answer=answer,
        route="knowledge",
        partial=False,
        needs_clarification=False,
        knowledge=support.SupportKnowledgeResponse(
            status="ok",
            answer=answer,
            coverage="complete",
            sources=[support.SupportKnowledgeSourceResponse(
                source_document=STANDARD.source_document,
                section=STANDARD.section,
                retrieval_kind="primary",
            )],
        ),
    )
    assert validate_http_response(fabricated).status is OutputValidationStatus.blocked


def test_no_retrieved_evidence_abstains_without_calling_model(through_support):
    run, calls, _stream = through_support([], scores=[])
    result = asyncio.run(run())
    assert result.knowledge.insufficient_context
    assert result.knowledge.sources == []
    assert result.answer == INSUFFICIENT_KNOWLEDGE
    assert not calls


@pytest.mark.parametrize("termination", [{"finish_reason": "length"}, {"provider_error": True}])
def test_unfinished_json_completion_is_not_accepted_even_with_valid_prose(
    through_support, termination,
):
    run, _, _ = through_support(
        [NATURAL_ES], **termination,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 503


@pytest.mark.parametrize(("question", "answer"), [
    (QUESTION, INSUFFICIENT_KNOWLEDGE),
    ("How does TrackFlow handle returns for an unsupported marketplace?", INSUFFICIENT_INFORMATION_ANSWER),
])
def test_marker_only_abstention_is_localized_in_both_transports(through_support, question, answer):
    run, _, _ = through_support(
        ["INSUFFICIENT_", "CONTEXT"],
        question=question,
    )
    async def compare():
        for event in (None, asyncio.Event()):
            result = await run(event)
            assert result.knowledge.insufficient_context
            assert result.knowledge.sources == []
            assert result.answer == answer

    asyncio.run(compare())


def test_aborting_before_terminal_discards_even_valid_prefix(through_support):
    run, _, stream = through_support(
        [NATURAL_ES], block=True,
    )

    async def abort():
        event = asyncio.Event()
        task = asyncio.create_task(run(event))
        await asyncio.wait_for(stream.waiting.wait(), timeout=2)
        assert not task.done()
        event.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(abort())
