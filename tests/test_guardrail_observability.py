from __future__ import annotations

import json
import logging
from uuid import uuid4

from agents.customer_support.guardrail_contracts import (
    FailureCategory,
    GuardrailEvent,
    GuardrailEventAction,
    GuardrailReasonCode,
)
from agents.customer_support.guardrail_observability import (
    _controlled_session,
    record_guardrail_event,
    reset_guardrail_counters,
    snapshot_guardrail_counters,
)


def test_event_log_and_counter_are_content_free(caplog) -> None:
    reset_guardrail_counters()
    correlation_id = str(uuid4())
    caplog.set_level(logging.INFO, logger="agent.customer_support.guardrails")
    record_guardrail_event(
        GuardrailEvent(
            guard_id="support.output.http.v1",
            failure_category=FailureCategory.security,
            action=GuardrailEventAction.blocked,
            reason_code=GuardrailReasonCode.unsafe_output,
            correlation_id=correlation_id,
        )
    )

    assert snapshot_guardrail_counters() == {
        "support.output.http.v1": {"blocked": 1}
    }
    event_payload = json.loads(caplog.records[-1].message.split(" | ", 1)[1])
    assert event_payload == {
        "action": "blocked",
        "failure_category": "security",
        "guard_id": "support.output.http.v1",
        "reason_code": "unsafe_output",
    }
    serialized = json.dumps(event_payload)
    assert correlation_id not in serialized
    for forbidden in (
        "user_text",
        "prompt",
        "response",
        "jwt",
        "token",
        "client_id",
        "memory",
    ):
        assert forbidden not in serialized.casefold()


def test_guardrail_logger_emits_content_free_info_in_runtime() -> None:
    assert logging.getLogger("agent.customer_support.guardrails").level == logging.INFO


def test_counters_group_guardrails_without_high_cardinality_dimensions() -> None:
    reset_guardrail_counters()
    for _ in range(2):
        record_guardrail_event(
            GuardrailEvent(
                guard_id="support.input.v1",
                failure_category=FailureCategory.content,
                action=GuardrailEventAction.redirected,
                reason_code=GuardrailReasonCode.smalltalk,
                correlation_id=str(uuid4()),
            )
        )
    assert snapshot_guardrail_counters() == {
        "support.input.v1": {"redirected": 2}
    }


def test_controlled_session_reports_blocked_and_redirected_counts() -> None:
    assert _controlled_session() == {
        "support.input.v1": {"blocked": 2, "redirected": 1}
    }
