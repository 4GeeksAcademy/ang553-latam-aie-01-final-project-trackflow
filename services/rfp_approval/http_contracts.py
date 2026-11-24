"""Strict public contracts for the Parte 3 operational API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import (
    AuthorizedInputPayload,
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    CurrencyConversionInput,
    CurrencyQuoteInput,
    GlobalCommercialTermInput,
    MonthlyStorageVolumeInput,
    OperationalScopeInput,
    VolumeDiscountInput,
    WarehouseCapacityInput,
)


class PublicContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _http_uuid(value: object, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UUID string") from error


def _http_uuid_list(value: object, field_name: str) -> list[UUID]:
    if type(value) is not list:
        raise ValueError(f"{field_name} must be a JSON array of UUID strings")
    return [
        _http_uuid(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    ]


def _http_date(value: object, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid ISO date string") from error


def _http_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(
                f"{field_name} must be a valid ISO datetime string"
            ) from error
    else:
        raise ValueError(f"{field_name} must be an ISO datetime string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


def _http_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"{field_name} must be a decimal string") from error
    else:
        raise ValueError(f"{field_name} must be a decimal string")
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal string")
    return parsed


def _http_department(value: object, field_name: str) -> DepartmentId:
    if isinstance(value, DepartmentId):
        return value
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a department string")
    try:
        return DepartmentId(value)
    except ValueError as error:
        raise ValueError(f"{field_name} is not an official department") from error


_INPUT_PAYLOAD_TYPES = {
    "client_price_quote": ClientPriceQuoteInput,
    "warehouse_capacity": WarehouseCapacityInput,
    "operational_scope": OperationalScopeInput,
    "currency_quote": CurrencyQuoteInput,
    "currency_conversion": CurrencyConversionInput,
    "global_commercial_term": GlobalCommercialTermInput,
    "volume_discount": VolumeDiscountInput,
    "monthly_storage_volume": MonthlyStorageVolumeInput,
}
_INPUT_PAYLOAD_INSTANCES = tuple(_INPUT_PAYLOAD_TYPES.values())


def _http_input_payload(value: object) -> AuthorizedInputPayload:
    if isinstance(value, _INPUT_PAYLOAD_INSTANCES):
        return value
    if type(value) is not dict:
        raise ValueError("payload must be a JSON object")
    converted = dict(value)
    input_kind = converted.get("input_kind")
    if type(input_kind) is not str or input_kind not in _INPUT_PAYLOAD_TYPES:
        raise ValueError("payload input_kind is not supported")

    if "department" in converted and converted["department"] is not None:
        converted["department"] = _http_department(
            converted["department"], "payload.department"
        )
    for field_name in {
        "effective_from",
        "effective_until",
        "available_from",
        "available_until",
    }:
        if field_name in converted and converted[field_name] is not None:
            converted[field_name] = _http_date(
                converted[field_name], f"payload.{field_name}"
            )
    if "effective_at" in converted:
        converted["effective_at"] = _http_datetime(
            converted["effective_at"], "payload.effective_at"
        )
    for field_name in {"amount", "capacity_value", "conversion_rate", "value"}:
        if field_name in converted:
            converted[field_name] = _http_decimal(
                converted[field_name], f"payload.{field_name}"
            )
    if input_kind == "volume_discount" and "tiers" in converted:
        tiers = converted["tiers"]
        if type(tiers) is not list:
            raise ValueError("payload.tiers must be a JSON array")
        converted_tiers: list[object] = []
        for index, tier in enumerate(tiers):
            if type(tier) is not dict:
                raise ValueError(f"payload.tiers[{index}] must be a JSON object")
            converted_tier = dict(tier)
            for field_name in {
                "minimum_volume",
                "maximum_volume",
                "discount_percentage",
            }:
                if (
                    field_name in converted_tier
                    and converted_tier[field_name] is not None
                ):
                    converted_tier[field_name] = _http_decimal(
                        converted_tier[field_name],
                        f"payload.tiers[{index}].{field_name}",
                    )
            converted_tiers.append(converted_tier)
        converted["tiers"] = converted_tiers
    return _INPUT_PAYLOAD_TYPES[input_kind].model_validate(converted)


def _http_provenance(value: object) -> BusinessInputProvenance:
    if isinstance(value, BusinessInputProvenance):
        return value
    if type(value) is not dict:
        raise ValueError("provenance must be a JSON object")
    converted = dict(value)
    if "evidence_id" in converted:
        converted["evidence_id"] = _http_uuid(
            converted["evidence_id"], "provenance.evidence_id"
        )
    if "issued_at" in converted:
        converted["issued_at"] = _http_datetime(
            converted["issued_at"], "provenance.issued_at"
        )
    if "valid_until" in converted and converted["valid_until"] is not None:
        converted["valid_until"] = _http_date(
            converted["valid_until"], "provenance.valid_until"
        )
    return BusinessInputProvenance.model_validate(converted)


class ApprovalWorkflowStartRequest(PublicContract):
    revision_id: UUID
    department: DepartmentId

    @field_validator("revision_id", mode="before")
    @classmethod
    def parse_http_revision_id(cls, value: object) -> UUID:
        return _http_uuid(value, "revision_id")

    @field_validator("department", mode="before")
    @classmethod
    def parse_http_department(cls, value: object) -> DepartmentId:
        return _http_department(value, "department")


class ApprovalRunInitializeRequest(PublicContract):
    idempotency_key: UUID
    automate_proposal_materialization: bool = False

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def parse_http_idempotency_key(cls, value: object) -> UUID:
        return _http_uuid(value, "idempotency_key")


class InitializedDepartmentPublic(PublicContract):
    department: DepartmentId
    revision_id: UUID
    status: str
    validation_bundle_id: UUID
    ready_for_hitl: bool


class ApprovalRunInitializationPublic(PublicContract):
    approval_run_id: UUID
    ticket_id: UUID
    response_run_id: UUID
    revisions: list[InitializedDepartmentPublic]
    proposal_automation_delegated: bool
    replayed: bool


class ConflictAssessmentOutcomePublic(PublicContract):
    trigger: Literal[
        "volume-vs-capacity", "returns-sla-breach", "currency-mismatch"
    ]
    outcome: Literal["demonstrated", "insufficient_information", "no_conflict"]


class InitialAssessmentPublic(PublicContract):
    approval_run_id: UUID
    outcomes: list[ConflictAssessmentOutcomePublic]
    ready_revisions: list[InitializedDepartmentPublic]
    missing_input_codes: list[str]


class DepartmentReviewInboxItem(PublicContract):
    approval_run_id: UUID
    ticket_id: UUID
    revision_id: UUID
    department: DepartmentId
    review_revision_no: int
    status: str
    content_hash: str
    validation_bundle_id: UUID
    created_at: datetime


class DepartmentReviewInboxPublic(PublicContract):
    items: list[DepartmentReviewInboxItem]


class AuthorizedBusinessInputRequest(PublicContract):
    input_id: UUID
    input_kind: Literal[
        "client_price_quote",
        "warehouse_capacity",
        "operational_scope",
        "currency_quote",
        "currency_conversion",
        "global_commercial_term",
        "volume_discount",
        "monthly_storage_volume",
    ]
    payload: AuthorizedInputPayload
    provenance: BusinessInputProvenance
    idempotency_key: UUID
    recorded_at: datetime

    @field_validator("input_id", "idempotency_key", mode="before")
    @classmethod
    def parse_http_uuid(cls, value: object, info) -> UUID:
        return _http_uuid(value, info.field_name)

    @field_validator("payload", mode="before")
    @classmethod
    def parse_http_payload(cls, value: object) -> AuthorizedInputPayload:
        return _http_input_payload(value)

    @field_validator("provenance", mode="before")
    @classmethod
    def parse_http_provenance(cls, value: object) -> BusinessInputProvenance:
        return _http_provenance(value)

    @field_validator("recorded_at", mode="before")
    @classmethod
    def parse_http_recorded_at(cls, value: object) -> datetime:
        return _http_datetime(value, "recorded_at")

    @model_validator(mode="after")
    def coherent_kind(self) -> "AuthorizedBusinessInputRequest":
        if self.input_kind != self.payload.input_kind:
            raise ValueError("input kind and payload disagree")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        return self


class AuthorizedBusinessInputPublic(PublicContract):
    input_id: UUID
    approval_run_id: UUID
    payload_hash: str
    replayed: bool


class DepartmentCorrectionRequest(PublicContract):
    expected_predecessor_status: Literal["blocked_input", "changes_requested"]
    expected_predecessor_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_predecessor_state_version: int = Field(ge=0)
    authorized_input_ids: list[UUID] = Field(min_length=1, max_length=30)
    idempotency_key: UUID
    requested_at: datetime

    @field_validator("authorized_input_ids", mode="before")
    @classmethod
    def parse_http_authorized_input_ids(cls, value: object) -> list[UUID]:
        return _http_uuid_list(value, "authorized_input_ids")

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def parse_http_idempotency_key(cls, value: object) -> UUID:
        return _http_uuid(value, "idempotency_key")

    @field_validator("requested_at", mode="before")
    @classmethod
    def parse_http_requested_at(cls, value: object) -> datetime:
        return _http_datetime(value, "requested_at")

    @model_validator(mode="after")
    def coherent_request(self) -> "DepartmentCorrectionRequest":
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        if len(self.authorized_input_ids) != len(set(self.authorized_input_ids)):
            raise ValueError("authorized_input_ids must be unique")
        return self


class DepartmentCorrectionPublic(PublicContract):
    approval_run_id: UUID
    ticket_id: UUID
    department: DepartmentId
    predecessor_revision_id: UUID
    revision_id: UUID
    review_revision_no: int
    status: str
    content_hash: str
    validation_bundle_id: UUID
    remaining_input_codes: list[str]
    ready_for_assessment: bool
    ready_for_hitl: Literal[False] = False
    replayed: bool


class ApprovalDecisionRequest(PublicContract):
    action: Literal["approve", "reject", "request_changes"]
    reason: str | None = Field(default=None, max_length=2_000)
    acknowledge_pending_commercial_items: bool = False

    @model_validator(mode="after")
    def reason_required(self) -> "ApprovalDecisionRequest":
        if self.action != "approve" and not (self.reason and self.reason.strip()):
            raise ValueError("reject and request_changes require a reason")
        return self


class ApprovalWorkflowPublic(PublicContract):
    thread_id: str
    status: str | None
    target: dict[str, Any]
    interrupted: bool
    interrupt_payload: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    coordination: dict[str, Any] | None = None


class ProposalMaterializationPublic(PublicContract):
    proposal_revision_id: UUID
    approval_run_id: UUID
    eligible_for_synthesis: bool
    replayed: bool
    coordination: ApprovalWorkflowPublic


class FinalDocumentLocatorPublic(PublicContract):
    ticket_id: UUID
    approval_run_id: UUID
    final_document_id: UUID


class DepartmentReviewPublic(PublicContract):
    approval_run_id: UUID
    ticket_id: UUID
    revision_id: UUID
    department: DepartmentId
    run_status: str
    revision_status: str
    is_current: bool
    state_version: int
    content_hash: str
    validation_bundle_id: UUID
    validated_at: datetime
    validation: dict[str, Any]
    pending_commercial_items: list[dict[str, Any]]
    safe_preview_artifact_id: UUID
    safe_preview_payload_hash: str
    safe_preview: str
    evidence: list[dict[str, Any]]
    provenance: dict[str, Any]
    allowed_actions: list[Literal["approve", "reject", "request_changes"]]
