"""Publish the First-line CX Agent's validated response at the chat boundary."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException, Response

from agents.customer_support.external_graph import AnchorRetrieval, KnowledgeProjectionSelected, get_external_support_agent
from agents.customer_support.guardrail_contracts import OutputValidationStatus
from agents.customer_support.output_guardrails import validate_http_response
from services.api.access_control import build_access_context
from services.api.agent_memory.contracts import MemoryScope
from services.api.chat.contracts import ChatKnowledge
from services.api.chat.coordinator import AnnotatedChunk, CoverageUpdate, ProducerUnavailable, ProgressProducer, ProgressReporter
from services.api.chat.qa_units import (
    dynamic_qa_unit_scope,
    publish_qa_units,
    requested_qa_unit_scope,
    select_coverage_plan,
)
from services.api.chat.store import chat_support_context, get_chat_session
from services.api.database import open_db_session
from services.api.routes.support import (
    SupportQueryRequest,
    get_memory_store,
    get_support_mcp_tools,
    query_support,
)
from services.api.support_oidc import SupportIdentity
from services.rag.contracts import RetrievalResult

async def unavailable_producer(_session_id: str, _text: str) -> AsyncIterator[str]:
    if False:  # pragma: no cover - preserves the async-generator interface
        yield ""
    raise ProducerUnavailable("safe support generation is not configured")


async def validated_complete_response(answer: str) -> AsyncIterator[str]:
    """Emit one authentic, already-validated complete response.

    Callers must apply ``validate_external_result`` and
    ``validate_http_response`` before invoking this adapter.
    """
    if not answer:
        raise ProducerUnavailable("validated response is empty")
    yield answer


def authentic_support_producer(identity: SupportIdentity):
    """Bind the WebSocket turn to Support with an isolated fixture opt-in."""

    async def produce(
        session_id: str, text: str, report: ProgressReporter | None,
    ) -> AsyncIterator[str | AnnotatedChunk | CoverageUpdate]:
        abort_event = asyncio.Event()
        try:
            conversation_id = _support_conversation_id(session_id)
        except ValueError as exc:
            raise ProducerUnavailable("chat session is not a support conversation") from exc

        unit_scope = requested_qa_unit_scope(identity, text)
        if unit_scope is not None:
            def can_try_qa_units() -> bool:
                with open_db_session() as db:
                    access = build_access_context(db, identity.user)
                    if not unit_scope.allows(access):
                        raise ProducerUnavailable("QA unit scope changed")
                    get_chat_session(db, access, session_id)
                    return get_memory_store().get_pending(db, conversation_id, access) is None

            if await asyncio.to_thread(can_try_qa_units):
                async with aclosing(publish_qa_units(identity, unit_scope, session_id)) as units:
                    async for unit in units:
                        yield unit
                return

        dynamic_scope = dynamic_qa_unit_scope(identity)
        selected_plan = None

        async def select_projection(
            question: str, evidence: tuple[RetrievalResult, ...], retrieve_anchor: AnchorRetrieval,
        ) -> bool:
            nonlocal selected_plan
            if dynamic_scope is None:
                return False
            selection = asyncio.create_task(
                asyncio.to_thread(select_coverage_plan, question, evidence, dynamic_scope, retrieve_anchor)
            )
            try:
                selected_plan = await asyncio.shield(selection)
            except asyncio.CancelledError:
                await asyncio.gather(selection, return_exceptions=True)
                raise
            return selected_plan is not None

        async def execute_support_query():
            with open_db_session() as db:
                access = build_access_context(db, identity.user)
                context = chat_support_context(db, access, session_id)
                # The existing MCP bearer is not client-bound. Until the MCP
                # surface can enforce the chat client itself, permit authentic
                # WS execution only for a manager whose live membership is
                # exactly this one client. Admin/global and multi-client
                # identities fail closed to prevent cross-client persistence.
                if access.global_access or access.allowed_client_ids != frozenset(
                    {context.client_id}
                ):
                    raise ProducerUnavailable(
                        "support client scope cannot be guaranteed"
                    )
                memory_store = get_memory_store()
                # New UUID sessions are bound atomically at creation. Legacy
                # ``chat_<hex>`` sessions predate that binding; create/verify
                # the same 128-bit UUID only after chat ownership succeeds.
                memory_store.bind_conversation(
                    db, conversation_id, access, create=True
                )
                request = SimpleNamespace(
                    state=SimpleNamespace(
                        request_id=str(uuid4()),
                        support_conversation_context=context.history_json,
                        support_abort_event=abort_event,
                        support_progress=report,
                        support_knowledge_projection=(
                            select_projection
                            if dynamic_scope is not None
                            and dynamic_scope.client_id == context.client_id
                            and dynamic_scope.allows(access)
                            else None
                        ),
                    )
                )
                result = await query_support(
                    SupportQueryRequest(
                        question=text,
                        conversation_id=conversation_id,
                        client_id=context.client_id,
                        memory_scope=MemoryScope.client,
                    ),
                    request,
                    Response(),
                    identity,
                    db,
                    get_external_support_agent(),
                    get_support_mcp_tools(identity),
                    memory_store,
                )
                return result, context.client_id

        if report is not None:
            await report("processing")
        task = asyncio.create_task(execute_support_query())
        try:
            result, client_id = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Signal the asynchronous provider while allowing synchronous work
            # to unwind before closing its SQL session.
            abort_event.set()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except KnowledgeProjectionSelected:
            if dynamic_scope is None or selected_plan is None:
                raise ProducerUnavailable("QA knowledge projection did not select a plan") from None
            async with aclosing(
                publish_qa_units(identity, dynamic_scope, session_id, coverage_plan=selected_plan)
            ) as units:
                async for unit in units:
                    yield unit
            return
        except HTTPException as exc:
            raise ProducerUnavailable("authenticated support query unavailable") from exc
        except ProducerUnavailable:
            raise
        except Exception as exc:
            raise ProducerUnavailable("authenticated support query unavailable") from exc
        # ``query_support`` returns only after graph assembly, consent handling,
        # output guardrails and complete HTTP projection validation. Publishing
        # one chunk here is authentic terminal delivery, not simulated streaming.
        if report is not None:
            await report("validating_response")
        if validate_http_response(result).status is OutputValidationStatus.blocked:
            raise ProducerUnavailable("support response failed final validation")
        # Membership may be revoked while the synchronous graph/provider is in
        # flight. Re-read it before making the answer available to the chat
        # coordinator for durable append/publication.
        try:
            with open_db_session() as db:
                refreshed_access = build_access_context(db, identity.user)
                get_chat_session(db, refreshed_access, session_id)
                if (
                    refreshed_access.global_access
                    or refreshed_access.allowed_client_ids
                    != frozenset({client_id})
                ):
                    raise ProducerUnavailable(
                        "support client scope cannot be guaranteed"
                    )
        except ProducerUnavailable:
            raise
        except Exception as exc:
            raise ProducerUnavailable("support membership could not be refreshed") from exc
        insufficient_context = (
            result.route == "knowledge"
            and result.memory_read_status != "used"
            and result.memory_consent is None
            and result.knowledge is not None
            and result.knowledge.status == "not_found"
            and result.knowledge.insufficient_context
        )
        knowledge = None
        if insufficient_context:
            knowledge = ChatKnowledge(coverage="insufficient", sources=())
        elif result.knowledge is not None and result.knowledge.sources:
            knowledge = ChatKnowledge.model_validate({
                "sources": [source.model_dump() for source in result.knowledge.sources],
            })
        async for chunk in validated_complete_response(result.answer):
            if insufficient_context:
                yield AnnotatedChunk(chunk, "insufficient_context", knowledge)
            elif result.needs_clarification and result.memory_consent is None:
                yield AnnotatedChunk(chunk, "clarification", knowledge)
            elif knowledge is not None:
                yield AnnotatedChunk(chunk, knowledge=knowledge)
            else:
                yield chunk

    return ProgressProducer(produce)


def _support_conversation_id(session_id: str) -> UUID:
    try:
        return UUID(session_id)
    except ValueError:
        match = re.fullmatch(r"chat_([0-9a-fA-F]{32})", session_id)
        if match is None:
            raise
        return UUID(hex=match.group(1))
