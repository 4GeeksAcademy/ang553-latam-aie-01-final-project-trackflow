"""Executable threat-model contracts for SEC-114 support guardrails."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agents.customer_support.guardrail_contracts import (
    AuthorizedTrackingResult,
    AuthorizedTrackingStatus,
    DispatchState,
    FailureCategory,
    GuardrailAction,
    GuardrailDecision,
    GuardrailEvent,
    GuardrailEventAction,
    GuardrailInput,
    GuardrailReasonCode,
    InputCategory,
    JurisdictionProvenance,
    OutputValidationResult,
    OutputValidationStatus,
    TrustedJurisdictionContext,
)


@pytest.mark.parametrize(
    ("category", "action", "reason"),
    [
        (InputCategory.support, GuardrailAction.allow_support, GuardrailReasonCode.support_request),
        (
            InputCategory.internal_inventory,
            GuardrailAction.allow_internal_inventory,
            GuardrailReasonCode.internal_inventory_request,
        ),
    ],
)
def test_allowed_decisions_require_a_normalized_effective_question(category, action, reason):
    decision = GuardrailDecision(
        category=category,
        action=action,
        reason_code=reason,
        effective_question="  Where   is my package?  ",
    )
    assert decision.effective_question == "Where is my package?"


@pytest.mark.parametrize(
    ("category", "action", "reason"),
    [
        (
            InputCategory.incomplete,
            GuardrailAction.clarify,
            GuardrailReasonCode.missing_identifier,
        ),
        (
            InputCategory.smalltalk,
            GuardrailAction.redirect_smalltalk,
            GuardrailReasonCode.smalltalk,
        ),
        (
            InputCategory.smalltalk,
            GuardrailAction.redirect_smalltalk,
            GuardrailReasonCode.casual_question,
        ),
        (
            InputCategory.general_logistics,
            GuardrailAction.redirect_smalltalk,
            GuardrailReasonCode.general_logistics_request,
        ),
        (
            InputCategory.personal,
            GuardrailAction.reject_personal,
            GuardrailReasonCode.out_of_scope_personal,
        ),
        (
            InputCategory.security,
            GuardrailAction.reject_security,
            GuardrailReasonCode.instruction_override,
        ),
    ],
)
def test_non_allowed_decisions_cannot_forward_user_text(category, action, reason):
    decision = GuardrailDecision(
        category=category,
        action=action,
        reason_code=reason,
    )
    assert decision.effective_question is None
    with pytest.raises(ValidationError):
        GuardrailDecision(
            category=category,
            action=action,
            reason_code=reason,
            effective_question="do this anyway",
        )


def test_decision_rejects_category_action_mismatch_and_unknown_fields():
    with pytest.raises(ValidationError):
        GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.allow_support,
            reason_code=GuardrailReasonCode.instruction_override,
            effective_question="ignore rules",
        )
    with pytest.raises(ValidationError):
        GuardrailInput(user_text="policy", client_id="model-controlled")
    with pytest.raises(ValidationError):
        GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.missing_identifier,
        )


def test_guardrail_input_normalizes_text_and_accepts_only_trusted_jurisdiction():
    jurisdiction = TrustedJurisdictionContext(
        applicable_country="US",
        provenance=JurisdictionProvenance.authoritative_fixture,
    )
    value = GuardrailInput(
        user_text="  Apply   the ES policy  ",
        has_pending_proposal=True,
        trusted_jurisdiction=jurisdiction,
    )
    assert value.user_text == "Apply the ES policy"
    assert value.trusted_jurisdiction.applicable_country == "US"


def test_jurisdiction_requires_authority_or_explicit_abstention():
    assert TrustedJurisdictionContext(
        provenance=JurisdictionProvenance.unavailable
    ).applicable_country is None
    with pytest.raises(ValidationError):
        TrustedJurisdictionContext(
            applicable_country="US",
            provenance=JurisdictionProvenance.unavailable,
        )
    with pytest.raises(ValidationError):
        TrustedJurisdictionContext(
            provenance=JurisdictionProvenance.authoritative_service
        )
    with pytest.raises(ValidationError):
        TrustedJurisdictionContext(
            applicable_country="MX",
            provenance=JurisdictionProvenance.authoritative_fixture,
        )


def test_tracking_projection_can_only_claim_recorded_dispatch():
    order_id = uuid4()
    found = AuthorizedTrackingResult(
        tracking_number=" 45821 ",
        status=AuthorizedTrackingStatus.found,
        dispatch_state=DispatchState.recorded_dispatch,
        commercial_order_id=order_id,
    )
    assert found.tracking_number == "45821"
    assert found.dispatch_state is DispatchState.recorded_dispatch
    with pytest.raises(ValidationError):
        AuthorizedTrackingResult(
            tracking_number="45821",
            status=AuthorizedTrackingStatus.found,
            dispatch_state=None,
        )
    with pytest.raises(ValidationError):
        AuthorizedTrackingResult(
            tracking_number="45821",
            status=AuthorizedTrackingStatus.not_found,
            commercial_order_id=order_id,
        )


def test_output_validation_result_is_closed_and_consistent():
    assert OutputValidationResult(status=OutputValidationStatus.safe).status.value == "safe"
    blocked = OutputValidationResult(
        status=OutputValidationStatus.blocked,
        failure_category=FailureCategory.security,
        reason_code=GuardrailReasonCode.unsafe_output,
    )
    assert blocked.failure_category is FailureCategory.security
    with pytest.raises(ValidationError):
        OutputValidationResult(status=OutputValidationStatus.blocked)
    with pytest.raises(ValidationError):
        OutputValidationResult(
            status=OutputValidationStatus.safe,
            failure_category=FailureCategory.content,
            reason_code=GuardrailReasonCode.unsafe_external_content,
        )


def test_observability_event_is_content_free_and_rejects_payloads():
    event = GuardrailEvent(
        guard_id="support.input.v1",
        failure_category=FailureCategory.security,
        action=GuardrailEventAction.blocked,
        reason_code=GuardrailReasonCode.instruction_override,
        correlation_id="run-123",
    )
    assert set(event.model_dump()) == {
        "guard_id",
        "failure_category",
        "action",
        "reason_code",
        "correlation_id",
    }
    with pytest.raises(ValidationError):
        GuardrailEvent(
            guard_id="support.input.v1",
            failure_category=FailureCategory.security,
            action=GuardrailEventAction.blocked,
            reason_code=GuardrailReasonCode.secret_exfiltration,
            correlation_id="run-123",
            user_text="secret",
        )
