"""Restricted entry point to the existing First-line CX graph for external QA."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from uuid import uuid4

from sqlmodel import Session

from agents.customer_support.external_graph import get_external_support_agent, route_support_query
from agents.customer_support.guardrail_contracts import GuardrailAction, GuardrailInput, OutputValidationStatus
from agents.customer_support.guardrail_observability import record_input_decision
from agents.customer_support.input_guardrails import classify_support_input
from agents.customer_support.output_guardrails import validate_external_result
from agents.customer_support.tools import ToolStatus, TrackingLookupInput, TrackingLookupResult
from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.rag.contracts import RAGConfigurationError
from services.rag.resilience import QueryDeadline


def _deny_knowledge():
    raise RAGConfigurationError("external QA grant has no knowledge access")


async def external_tracking_answer(
    question: str, *, access: AccessContext,
    database: Callable[[], AbstractContextManager[Session]],
    lookup: Callable[[str], Awaitable[TrackingLookupResult]],
) -> bool:
    """Execute the scoped tracking route and expose only the approved projection."""
    if (
        access.role is not Role.user or access.global_access
        or len(access.allowed_client_ids) != 1 or access.sensitive_client_ids
    ):
        return False
    run_id = str(uuid4())
    decision = classify_support_input(GuardrailInput(user_text=question))
    if decision.action is not GuardrailAction.allow_support:
        record_input_decision(decision, run_id)
        return False
    route = route_support_query(question)
    if (
        route.route != "tracking" or not route.tracking_number
        or route.needs_ticket or route.needs_inventory or route.needs_knowledge
    ):
        return False
    # External grants require one unambiguous identifier, including when the
    # general router recognized a prefix or only the first explicit reference.
    references = {
        value.rstrip(".").upper()
        for value in re.findall(r"(?<![\w./-])[A-Za-z0-9][A-Za-z0-9._/-]*", question)
        if any(char.isdigit() for char in value)
    }
    if references != {route.tracking_number}:
        return False

    called = False

    async def tracking_tool(request: TrackingLookupInput, deadline: QueryDeadline):
        nonlocal called
        if called or request.tracking_number != route.tracking_number:
            return TrackingLookupResult(status=ToolStatus.not_found)
        called = True
        async with asyncio.timeout(deadline.operation_timeout(10)):
            return await lookup(request.tracking_number)

    with database() as db:
        execution = await get_external_support_agent().execute(
            question, session=db, access=access, deadline=QueryDeadline(15),
            rag_factory=_deny_knowledge, run_id=run_id, tracking_tool=tracking_tool,
        )
    result = execution.result
    return (
        called and result.route == "tracking"
        and validate_external_result(result).status is OutputValidationStatus.safe
        and result.tracking is not None and result.tracking.status is ToolStatus.ok
        and result.tracking.tracking_number == route.tracking_number
    )
