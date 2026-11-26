"""QA-only publication of checked synthetic units from a closed SSE selection."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from agents.customer_support.external_graph import route_support_query
from agents.customer_support.guardrail_contracts import OutputValidationStatus
from agents.customer_support.output_guardrails import validate_http_response
from services.api.access_control import build_access_context
from services.api.auth_models import Role
from services.api.chat.contracts import UnitCoverage, UnitReference
from services.api.chat.coordinator import AnnotatedChunk, CoverageUpdate, ProducerUnavailable
from services.api.chat.qa_unit_catalog import (
    CatalogUnit, CoveragePlan, authorized_unit, catalog_anchor_query, coverage_anchor_query,
    eligible_units, plan_coverage,
)
from services.api.chat.qa_units_scope import QaUnitScope, qa_unit_scope
from services.api.chat.store import get_chat_session
from services.api.database import open_db_session
from services.api.routes.support import SupportKnowledgeResponse, SupportQueryResponse
from services.api.support_oidc import SupportIdentity
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import DEFAULT_BASE_URL, load_settings
from services.rag.contracts import RAGConfigurationError, RAGProviderError, RetrievalResult


QA_UNITS_QUESTION = "Describe la devolución de prueba de TrackFlow."
_END = object()
_LOGGER = logging.getLogger("trackflow.chat.i13")
_LOGGER.setLevel(logging.INFO)
if not _LOGGER.handlers:
    _LOGGER.addHandler(logging.StreamHandler())
_LOGGER.propagate = False
_WIRE_ID = re.compile(r"[A-Z][A-Z0-9-]{0,31}")


class QaUnitRejected(ProducerUnavailable):
    """A safe, locally assigned rejection reason. Never includes provider content."""

    def __init__(self, reason: str) -> None:
        super().__init__("QA unit selection or authorization rejected")
        self.reason = reason


@dataclass(frozen=True)
class _QaUnit:
    id: str
    text: str
    reference: str

    @property
    def render(self) -> str:
        return f"{self.text} [Fuente sintética: {self.reference}]"


_PLAN = (
    _QaUnit(
        "U1",
        "En el escenario ficticio QA I13, la marca recibe la solicitud a través de su portal de prueba.",
        "QA-I13-DEV/U1",
    ),
    _QaUnit(
        "U2",
        "En el escenario ficticio QA I13, solo se simula la emisión de una etiqueta si el motivo cumple las reglas de la marca de prueba.",
        "QA-I13-DEV/U2",
    ),
    _QaUnit(
        "U3",
        "En el escenario ficticio QA I13, el almacén simula la inspección del producto devuelto antes de clasificarlo.",
        "QA-I13-DEV/U3",
    ),
)


class _QaUnitGateway(FourGeeksHTTPClient):
    def _configured_generation_payload(
        self, _question: str, _context: str, *, stream: bool = False,
    ) -> dict[str, object]:
        ids = "\n".join(unit.id for unit in _PLAN)
        return {
            "model": self.settings.generation_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Emit exactly these synthetic QA unit IDs in the given order, "
                        "one ID per line. Add a newline after each ID except the last. "
                        "Do not add any other content.\n" + ids
                    ),
                },
                {"role": "user", "content": "Run the synthetic QA unit selection."},
            ],
            "temperature": 0,
            "max_tokens": 32,
            "stream": stream,
        }


class _DynamicQaUnitGateway(_QaUnitGateway):
    def __init__(self, settings, plan: tuple[CatalogUnit, ...], **kwargs):
        super().__init__(settings, **kwargs)
        self.plan = plan

    def _configured_generation_payload(
        self, question: str, context: str, *, stream: bool = False,
    ) -> dict[str, object]:
        payload = super()._configured_generation_payload(question, context, stream=stream)
        return {
            **payload,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Select one or more IDs from the following authorized QA list, "
                        "retaining the listed order. Choose distinct IDs. Return one ID per "
                        "line and no other content. Include IDs for each requested topic.\n"
                        + "\n".join(unit.id for unit in self.plan)
                    ),
                },
                {"role": "user", "content": "Select the IDs for the QA request."},
            ],
            "max_tokens": 96,
        }


def requested_qa_unit_scope(identity: SupportIdentity, question: str) -> QaUnitScope | None:
    scope = qa_unit_scope()
    if (
        os.getenv("TRACKFLOW_I13_QA_UNITS") != "1"
        or scope is None
        or identity.user.id != scope.user_id
        or identity.user.role is not Role.manager
        or question != QA_UNITS_QUESTION
        or route_support_query(question).route != "knowledge"
    ):
        return None
    return scope


def dynamic_qa_unit_scope(identity: SupportIdentity) -> QaUnitScope | None:
    if os.getenv("TRACKFLOW_I14_QA_UNITS") != "1":
        return None
    scope = qa_unit_scope()
    if scope and identity.user.id == scope.user_id and identity.user.role is Role.manager:
        return scope
    return None


def select_dynamic_units(
    question: str, evidence: tuple[RetrievalResult, ...], scope: QaUnitScope,
    retrieve_anchor: Callable[[str], tuple[RetrievalResult, ...]] | None = None,
) -> tuple[CatalogUnit, ...]:
    try:
        with open_db_session() as db:
            plan = eligible_units(db, scope.client_id, question, evidence)
            if plan or retrieve_anchor is None:
                return plan
            lookup = catalog_anchor_query(db, scope.client_id, question)
        if lookup is not None:
            retrieved = retrieve_anchor(lookup)
            with open_db_session() as db:
                return eligible_units(db, scope.client_id, question, retrieved)
    except (RAGConfigurationError, RAGProviderError):
        _LOGGER.warning("I14 QA units | stage=anchor_retrieval_unavailable")
    except SQLAlchemyError:
        # The optional QA catalog may not be provisioned. Natural Support
        # continues through the current graph with the same retrieved context.
        _LOGGER.warning("I14 QA units | stage=catalog_unavailable")
    return ()


def select_coverage_plan(
    question: str, evidence: tuple[RetrievalResult, ...], scope: QaUnitScope,
    retrieve_anchor: Callable[[str], tuple[RetrievalResult, ...]] | None = None,
) -> CoveragePlan | None:
    if not evidence and retrieve_anchor is None:
        return None
    plan = None
    try:
        with open_db_session() as db:
            plan = plan_coverage(db, scope.client_id, question, evidence)
            if plan is None or plan.requested is None or retrieve_anchor is None:
                return plan
            if set(plan.requested) <= plan.authorized_topics:
                return plan
            lookup = coverage_anchor_query(db, scope.client_id, question)
        if lookup is not None:
            resolved = retrieve_anchor(lookup)
            with open_db_session() as db:
                return plan_coverage(db, scope.client_id, question, (*evidence, *resolved))
        return plan
    except (RAGConfigurationError, RAGProviderError):
        _LOGGER.warning("I16 QA units | stage=anchor_retrieval_unavailable")
        return plan
    except SQLAlchemyError:
        _LOGGER.warning("I16 QA units | stage=catalog_unavailable")
        return None


def assess_coverage(plan: CoveragePlan, delivered: set[str]) -> UnitCoverage:
    if plan.requested is None:
        return UnitCoverage(status="indeterminate")
    covered = {topic for unit_id, topic in plan.unit_topics if unit_id in delivered}
    missing = tuple(topic for topic in plan.requested if topic not in covered)
    if not missing:
        return UnitCoverage(status="complete")
    return UnitCoverage(
        status="partial" if len(missing) < len(plan.requested) else "none", missing_topics=missing,
    )


def _validate_text(text: str) -> None:
    result = SupportQueryResponse(
        answer=text,
        route="knowledge",
        partial=False,
        needs_clarification=False,
        knowledge=SupportKnowledgeResponse(status="ok", answer=text, sources=[]),
    )
    if validate_http_response(result).status is not OutputValidationStatus.safe:
        raise QaUnitRejected("guardrail_rejected")


def _approved_chunk(
    rendered: str, accumulated: str, reference: UnitReference | None,
) -> AnnotatedChunk:
    chunk = ("\n\n" if accumulated else "") + rendered
    _validate_text(rendered)
    _validate_text(accumulated + chunk)
    return AnnotatedChunk(chunk, publication_kind="authorized_unit", unit_ref=reference)


async def _selection_units(
    gateway: FourGeeksHTTPClient, authorize: Callable[[], Awaitable[None]],
    *, session_id: str | None = None, plan: tuple[CatalogUnit, ...] | None = None,
) -> AsyncIterator[AnnotatedChunk]:
    queue: asyncio.Queue = asyncio.Queue()
    advance = asyncio.Event()
    pending = ""
    accumulated = ""
    accepted = 0
    observed_length = 0
    provider_terminal = False
    remaining = {unit.id: (index, unit) for index, unit in enumerate(plan or ())}
    last_position = -1
    accepted_ids: set[str] = set()

    def trace(stage: str, *, reason: str = "-") -> None:
        if session_id is not None:
            _LOGGER.info(
                "%s QA units | at=%s | session=%s | stage=%s | accepted=%d | "
                "provider_terminal=%s | reason=%s | http_status=%s",
                "I13" if plan is None else "I14",
                datetime.now(UTC).isoformat(timespec="milliseconds"), session_id,
                stage, accepted, provider_terminal, reason, gateway.last_generation_status_code,
            )

    def reject(reason: str) -> QaUnitRejected:
        trace("rejected", reason=reason)
        return QaUnitRejected(reason)

    async def release(unit_id: str) -> None:
        nonlocal accepted, accumulated, last_position
        reference = None
        if plan is None:
            if accepted >= len(_PLAN) or unit_id != _PLAN[accepted].id:
                raise reject("unexpected_unit")
            unit = _PLAN[accepted].render
        else:
            if not unit_id:
                raise reject("empty_identifier_line")
            if not _WIRE_ID.fullmatch(unit_id):
                raise reject("invalid_identifier_format")
            choice = remaining.get(unit_id)
            if choice is None:
                raise reject("identifier_not_in_plan")
            if unit_id in accepted_ids:
                raise reject("duplicate_identifier")
            if choice[0] <= last_position:
                raise reject("identifier_out_of_order")
            last_position, selected = choice
            unit = selected.render
            reference = UnitReference(unit_id=selected.id, version=selected.version)
        try:
            chunk = _approved_chunk(unit, accumulated, reference)
        except QaUnitRejected as exc:
            trace("guardrail_rejected", reason=exc.reason)
            raise
        await check_access()
        accumulated += chunk.text
        accepted_ids.add(unit_id)
        accepted += 1
        advance.clear()
        queue.put_nowait(chunk)
        # The coordinator commits and publishes before requesting another unit.
        await advance.wait()
        trace("fanout_completed")

    async def observe(delta: str) -> None:
        nonlocal pending, observed_length
        observed_length += len(delta)
        pending += delta
        if len(pending) > (32 if plan is None else 170):
            raise reject("oversized_frame")
        while "\n" in pending:
            unit_id, _, pending = pending.partition("\n")
            await release(unit_id.removesuffix("\r"))

    async def read() -> None:
        nonlocal provider_terminal
        try:
            trace("provider_request_started")
            complete = await gateway.generate_async(
                "", "", timeout_seconds=20, delta_observer=observe, observe_terminal_delta=False,
            )
            provider_terminal = True
            trace("provider_terminal_and_closed")
            # Content in a combined stop frame is already terminal.
            if terminal_content := complete[observed_length:]:
                await observe(terminal_content)
            if pending:
                # A valid SSE terminal can delimit only the exact last ID.
                if plan is None and (accepted != len(_PLAN) - 1 or pending != _PLAN[-1].id):
                    raise reject("invalid_terminal_tail")
                await release(pending)
            if (plan is None and accepted != len(_PLAN)) or (plan is not None and accepted == 0):
                raise reject("incomplete_plan")
        except RAGProviderError as exc:
            trace("provider_failed", reason=exc.category)
            raise
        except asyncio.CancelledError:
            trace("reader_cancelled")
            raise
        finally:
            queue.put_nowait(_END)

    async def check_access() -> None:
        try:
            await authorize()
        except QaUnitRejected as exc:
            trace("access_rejected", reason=exc.reason)
            raise
        except ProducerUnavailable:
            trace("access_rejected")
            raise

    await check_access()
    task = asyncio.create_task(read())
    try:
        while (chunk := await queue.get()) is not _END:
            await check_access()
            yield chunk
            advance.set()
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _publish_dynamic_plan(
    plan: tuple[CatalogUnit, ...],
    authorize: Callable[[], Awaitable[None]],
    gateway_factory: Callable[[], FourGeeksHTTPClient],
    *, session_id: str | None = None,
) -> AsyncIterator[AnnotatedChunk]:
    if not plan:
        raise QaUnitRejected("empty_plan")
    if len(plan) == 1:
        # The catalog and primary RAG evidence already resolved the only ID.
        # This is a single validated publication, with no model stream.
        unit = plan[0]
        try:
            reference = UnitReference(unit_id=unit.id, version=unit.version)
            chunk = _approved_chunk(unit.render, "", reference)
            await authorize()
        except QaUnitRejected as exc:
            if session_id is not None:
                _LOGGER.info("I14 QA units | session=%s | stage=singleton_rejected | reason=%s",
                             session_id, exc.reason)
            raise
        if session_id is not None:
            _LOGGER.info("I14 QA units | session=%s | stage=deterministic_singleton", session_id)
        yield chunk
        return

    gateway = gateway_factory()
    try:
        async with aclosing(_selection_units(gateway, authorize, session_id=session_id, plan=plan)) as stream:
            async for chunk in stream:
                yield chunk
    finally:
        gateway.close()


async def _publish_coverage_plan(
    plan: CoveragePlan, authorize: Callable[[], Awaitable[None]],
    gateway_factory: Callable[[], FourGeeksHTTPClient],
    *, session_id: str | None = None,
) -> AsyncIterator[AnnotatedChunk | CoverageUpdate]:
    if not plan.units:
        await authorize()
        yield CoverageUpdate(assess_coverage(plan, set()))
        return

    delivered: set[str] = set()
    accumulated = ""
    async with aclosing(_publish_dynamic_plan(
        plan.units, authorize, gateway_factory, session_id=session_id,
    )) as stream:
        async for chunk in stream:
            reference = chunk.unit_ref
            if reference is None:
                raise QaUnitRejected("missing_coverage_unit_reference")
            delivered.add(reference.unit_id)
            accumulated += chunk.text
            yield replace(chunk, unit_coverage=assess_coverage(plan, delivered))

    # Runs only after a valid completion. An interrupted or invalid stream
    # cannot reach here. Required IDs are unique matches decided by the plan.
    missing = set(assess_coverage(plan, delivered).missing_topics)
    by_topic = dict(plan.unit_topics)
    for unit in plan.units:
        if unit.id not in plan.required or by_topic.get(unit.id) not in missing:
            continue
        reference = UnitReference(unit_id=unit.id, version=unit.version)
        chunk = _approved_chunk(unit.render, accumulated, reference)
        await authorize()
        delivered.add(unit.id)
        accumulated += chunk.text
        coverage = assess_coverage(plan, delivered)
        missing = set(coverage.missing_topics)
        yield replace(chunk, unit_coverage=coverage)


async def publish_qa_units(
    identity: SupportIdentity, scope: QaUnitScope, session_id: str,
    *, plan: tuple[CatalogUnit, ...] | None = None, coverage_plan: CoveragePlan | None = None,
) -> AsyncIterator[AnnotatedChunk | CoverageUpdate]:
    if coverage_plan is not None:
        plan = coverage_plan.units
    def require_access() -> None:
        try:
            current = identity.revalidate()
            current_scope = qa_unit_scope() if plan is None else dynamic_qa_unit_scope(current)
            if current_scope != scope:
                raise QaUnitRejected("scope_changed")
            with open_db_session() as db:
                access = build_access_context(db, current.user)
                if not scope.allows(access):
                    raise QaUnitRejected("scope_changed")
                get_chat_session(db, access, session_id)
                if plan is not None and (coverage_plan is not None or len(plan) == 1):
                    if any(authorized_unit(db, scope.client_id, unit.id, unit.version) != unit for unit in plan):
                        raise QaUnitRejected("catalog_grant_unavailable")
        except QaUnitRejected:
            raise
        except Exception as exc:
            raise QaUnitRejected("access_revalidation_failed") from exc

    async def authorize() -> None:
        await asyncio.to_thread(require_access)

    def settings_for_gateway():
        settings = load_settings()
        if settings.base_url != DEFAULT_BASE_URL:
            raise QaUnitRejected("gateway_configuration_rejected")
        return settings

    if plan is not None:
        def gateway_factory():
            return _DynamicQaUnitGateway(settings_for_gateway(), plan)

        stream = (
            _publish_dynamic_plan(plan, authorize, gateway_factory, session_id=session_id)
            if coverage_plan is None else _publish_coverage_plan(
                coverage_plan, authorize, gateway_factory, session_id=session_id,
            )
        )
        async with aclosing(stream) as units:
            async for chunk in units:
                yield chunk
        return

    try:
        gateway = _QaUnitGateway(settings_for_gateway())
    except (ProducerUnavailable, RAGConfigurationError):
        _LOGGER.info("I13 QA units | session=%s | stage=configuration_rejected", session_id)
        raise
    stream = _selection_units(gateway, authorize, session_id=session_id)
    try:
        async for chunk in stream:
            yield chunk
    finally:
        try:
            await stream.aclose()
        finally:
            gateway.close()
