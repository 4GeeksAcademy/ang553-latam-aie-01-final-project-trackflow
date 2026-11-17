"""Closed internal contracts for the TrackFlow support guardrail harness.

These types are deliberately transport-neutral.  Authenticated identity and
tenant access remain server-owned and are not accepted from model-controlled
input.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputCategory(str, Enum):
    support = "support"
    internal_inventory = "internal_inventory"
    general_logistics = "general_logistics"
    incomplete = "incomplete"
    smalltalk = "smalltalk"
    personal = "personal"
    security = "security"


class GuardrailAction(str, Enum):
    allow_support = "allow_support"
    allow_internal_inventory = "allow_internal_inventory"
    clarify = "clarify"
    redirect_smalltalk = "redirect_smalltalk"
    reject_personal = "reject_personal"
    reject_security = "reject_security"


class GuardrailReasonCode(str, Enum):
    support_request = "support_request"
    internal_inventory_request = "internal_inventory_request"
    general_logistics_request = "general_logistics_request"
    casual_question = "casual_question"
    missing_identifier = "missing_identifier"
    smalltalk = "smalltalk"
    out_of_scope_personal = "out_of_scope_personal"
    instruction_override = "instruction_override"
    prompt_exfiltration = "prompt_exfiltration"
    secret_exfiltration = "secret_exfiltration"
    confidential_commercial_request = "confidential_commercial_request"
    physical_security_request = "physical_security_request"
    jurisdiction_unavailable = "jurisdiction_unavailable"
    jurisdiction_override = "jurisdiction_override"
    invalid_external_structure = "invalid_external_structure"
    unsafe_external_content = "unsafe_external_content"
    unsafe_output = "unsafe_output"


class JurisdictionProvenance(str, Enum):
    authoritative_fixture = "authoritative_fixture"
    authoritative_service = "authoritative_service"
    unavailable = "unavailable"


class TrustedJurisdictionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable_country: Literal["US", "ES"] | None = None
    provenance: JurisdictionProvenance

    @model_validator(mode="after")
    def validate_authority(self) -> "TrustedJurisdictionContext":
        if self.provenance is JurisdictionProvenance.unavailable:
            if self.applicable_country is not None:
                raise ValueError("unavailable jurisdiction must not include a country")
        elif self.applicable_country is None:
            raise ValueError("authoritative jurisdiction requires applicable_country")
        return self


class GuardrailInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_text: str = Field(min_length=1, max_length=2000)
    has_pending_proposal: bool = False
    trusted_jurisdiction: TrustedJurisdictionContext | None = None

    @field_validator("user_text")
    @classmethod
    def normalize_user_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("user_text must not be blank")
        return normalized


_ACTION_BY_CATEGORY = {
    InputCategory.support: GuardrailAction.allow_support,
    InputCategory.internal_inventory: GuardrailAction.allow_internal_inventory,
    InputCategory.general_logistics: GuardrailAction.redirect_smalltalk,
    InputCategory.incomplete: GuardrailAction.clarify,
    InputCategory.smalltalk: GuardrailAction.redirect_smalltalk,
    InputCategory.personal: GuardrailAction.reject_personal,
    InputCategory.security: GuardrailAction.reject_security,
}

_REASONS_BY_CATEGORY = {
    InputCategory.support: frozenset({GuardrailReasonCode.support_request}),
    InputCategory.internal_inventory: frozenset(
        {GuardrailReasonCode.internal_inventory_request}
    ),
    InputCategory.general_logistics: frozenset(
        {GuardrailReasonCode.general_logistics_request}
    ),
    InputCategory.incomplete: frozenset(
        {
            GuardrailReasonCode.missing_identifier,
            GuardrailReasonCode.jurisdiction_unavailable,
        }
    ),
    InputCategory.smalltalk: frozenset(
        {GuardrailReasonCode.smalltalk, GuardrailReasonCode.casual_question}
    ),
    InputCategory.personal: frozenset(
        {GuardrailReasonCode.out_of_scope_personal}
    ),
    InputCategory.security: frozenset(
        {
            GuardrailReasonCode.instruction_override,
            GuardrailReasonCode.prompt_exfiltration,
            GuardrailReasonCode.secret_exfiltration,
            GuardrailReasonCode.confidential_commercial_request,
            GuardrailReasonCode.physical_security_request,
            GuardrailReasonCode.jurisdiction_override,
        }
    ),
}


class GuardrailDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: InputCategory
    action: GuardrailAction
    reason_code: GuardrailReasonCode
    response_language: Literal["en", "es"] = "en"
    effective_question: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("effective_question")
    @classmethod
    def normalize_effective_question(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("effective_question must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_decision(self) -> "GuardrailDecision":
        expected = _ACTION_BY_CATEGORY[self.category]
        if self.action is not expected:
            raise ValueError("guardrail action does not match input category")
        if self.reason_code not in _REASONS_BY_CATEGORY[self.category]:
            raise ValueError("reason code does not match input category")
        allowed = {
            GuardrailAction.allow_support,
            GuardrailAction.allow_internal_inventory,
        }
        if self.action in allowed and self.effective_question is None:
            raise ValueError("allowed input requires effective_question")
        if self.action not in allowed and self.effective_question is not None:
            raise ValueError("blocked or redirected input must not carry a question")
        return self


class AuthorizedTrackingStatus(str, Enum):
    found = "found"
    not_found = "not_found"
    unavailable = "unavailable"


class DispatchState(str, Enum):
    recorded_dispatch = "recorded_dispatch"


class AuthorizedTrackingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tracking_number: str = Field(min_length=1, max_length=128)
    status: AuthorizedTrackingStatus
    dispatch_state: DispatchState | None = None
    commercial_order_id: UUID | None = None

    @field_validator("tracking_number")
    @classmethod
    def normalize_tracking_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tracking_number must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_projection(self) -> "AuthorizedTrackingResult":
        if self.status is AuthorizedTrackingStatus.found:
            if self.dispatch_state is not DispatchState.recorded_dispatch:
                raise ValueError("found tracking requires recorded_dispatch")
        elif self.dispatch_state is not None or self.commercial_order_id is not None:
            raise ValueError("non-found tracking must not expose order data")
        return self


class FailureCategory(str, Enum):
    structural = "structural"
    content = "content"
    security = "security"


class OutputValidationStatus(str, Enum):
    safe = "safe"
    blocked = "blocked"


class OutputValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OutputValidationStatus
    failure_category: FailureCategory | None = None
    reason_code: GuardrailReasonCode | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "OutputValidationResult":
        if self.status is OutputValidationStatus.safe:
            if self.failure_category is not None or self.reason_code is not None:
                raise ValueError("safe output must not include failure details")
        elif self.failure_category is None or self.reason_code is None:
            raise ValueError("blocked output requires classified failure details")
        return self


class GuardrailEventAction(str, Enum):
    blocked = "blocked"
    redirected = "redirected"


class GuardrailEvent(BaseModel):
    """Content-free observability event safe for counters and logs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guard_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9._-]+$")
    failure_category: FailureCategory
    action: GuardrailEventAction
    reason_code: GuardrailReasonCode
    correlation_id: str = Field(min_length=1, max_length=128)

    @field_validator("correlation_id")
    @classmethod
    def normalize_correlation_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("correlation_id must not be blank")
        return normalized
