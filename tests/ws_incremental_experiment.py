"""Test-only experimental publication boundary. No production caller."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from agents.customer_support.external_graph import route_support_query
from agents.customer_support.guardrail_contracts import OutputValidationStatus
from agents.customer_support.memory_evaluation import prefilter_memory_candidate
from agents.customer_support.output_guardrails import validate_http_response
from services.api.chat.coordinator import Producer, ProducerUnavailable
from services.api.routes.support import (
    SupportKnowledgeResponse,
    SupportKnowledgeSourceResponse,
    SupportQueryResponse,
)


DeltaObserver = Callable[[str], Awaitable[None]]
Execute = Callable[[str, str, DeltaObserver], Awaitable[SupportQueryResponse]]
Authorize = Callable[[str], Awaitable[bool]]
PrefixProof = Callable[[str], bool]
_END = object()


def _knowledge_projection(
    answer: str, sources: Sequence[SupportKnowledgeSourceResponse],
) -> SupportQueryResponse:
    return SupportQueryResponse(
        answer=answer,
        route="knowledge",
        partial=False,
        needs_clarification=False,
        knowledge=SupportKnowledgeResponse(status="ok", answer=answer, sources=list(sources)),
    )


def _passes_guardrails(result: SupportQueryResponse) -> bool:
    return validate_http_response(result).status is OutputValidationStatus.safe


def _validate_terminal(result: SupportQueryResponse) -> None:
    knowledge = result.knowledge
    if (
        result.route != "knowledge"
        or result.partial
        or result.needs_clarification
        or result.ticket is not None
        or result.inventory is not None
        or result.memory_consent is not None
        or result.memory_proposal is not None
        or result.memory_read_status not in (None, "skipped", "empty")
        or knowledge is None
        or knowledge.answer != result.answer
        or not _passes_guardrails(result)
    ):
        raise ProducerUnavailable("experimental terminal result was rejected")


def incremental_producer(
    execute: Execute,
    authorize: Authorize,
    certify: PrefixProof | None = None,
    *,
    sources: Sequence[SupportKnowledgeSourceResponse] = (),
) -> Producer:
    """Hold by default. An injected test proof exercises transport only.

    A current guardrail PASS is necessary at release and is never treated as
    proof of factual support, qualifier completeness or safety across future
    deltas. No certifier for free prose is supplied.
    """
    evidence = tuple(sources)

    async def produce(session_id: str, question: str):
        if (
            route_support_query(question).route != "knowledge"
            or prefilter_memory_candidate(question) is not None
        ):
            raise ProducerUnavailable("experimental route is not eligible")
        events: asyncio.Queue = asyncio.Queue()
        pending: list[str] = []
        provider_text = ""
        confirmed_count = 0

        async def require_authorization() -> None:
            if not await authorize(session_id):
                raise ProducerUnavailable("experimental session authorization was lost")

        async def observe(delta: str) -> None:
            nonlocal provider_text, confirmed_count
            provider_text += delta
            pending.append(delta)
            if certify is None or not certify(provider_text):
                return
            if (not any(source.retrieval_kind == "primary" for source in evidence)
                    or not _passes_guardrails(_knowledge_projection(provider_text, evidence))):
                raise ProducerUnavailable("experimental prefix failed prepublication checks")
            await require_authorization()
            for approved in pending:
                events.put_nowait(approved)
            confirmed_count += len(pending)
            pending.clear()

        async def run():
            try:
                return await execute(session_id, question, observe)
            finally:
                events.put_nowait(_END)

        await require_authorization()
        task = asyncio.create_task(run())
        try:
            while (item := await events.get()) is not _END:
                await require_authorization()
                yield item
            result = await task
            _validate_terminal(result)
            await require_authorization()
            if confirmed_count == 0:
                # Ordinary terminal delivery. Do not replay retained deltas.
                yield result.answer
                return
            if (
                provider_text != result.answer
                or result.knowledge is None
                or tuple(result.knowledge.sources) != evidence
            ):
                raise ProducerUnavailable("experimental final text or evidence changed")
            for delta in pending:
                await require_authorization()
                yield delta
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return produce
