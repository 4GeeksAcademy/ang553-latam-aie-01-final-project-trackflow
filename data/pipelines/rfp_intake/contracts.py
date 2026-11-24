"""Closed, persistence-neutral contracts for TrackFlow RFP intake."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DepartmentId(str, Enum):
    warehouse = "warehouse"
    lastmile = "lastmile"
    reverse = "reverse"


RESPONSIBLE_BY_DEPARTMENT: dict[DepartmentId, str] = {
    DepartmentId.warehouse: "Ana Whitfield",
    DepartmentId.lastmile: "Carlos Vega",
    DepartmentId.reverse: "Sofía Ramos",
}


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, use_enum_values=False)


class SourceReference(StrictContract):
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> "SourceReference":
        if self.line_end < self.line_start:
            raise ValueError("source reference end precedes start")
        return self


class ServiceRequest(StrictContract):
    service_id: DepartmentId
    requested: bool
    source_ref: SourceReference


class ClassificationDecision(StrictContract):
    is_valid_rfp: bool
    reason_code: Literal[
        "legitimate_rfp",
        "vendor_pitch",
        "not_a_request",
        "outside_trackflow_scope",
        "ambiguous",
    ]
    evidence_refs: list[SourceReference] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def coherent_reason(self) -> "ClassificationDecision":
        if self.reason_code == "ambiguous":
            if self.is_valid_rfp:
                raise ValueError("ambiguous classification cannot be accepted")
        elif self.is_valid_rfp != (self.reason_code == "legitimate_rfp"):
            raise ValueError("classification result and reason disagree")
        return self


class BudgetRange(StrictContract):
    minimum: float | None
    maximum: float | None
    currency: Literal["EUR", "USD"] | None
    unit: str | None = Field(max_length=80)
    source_ref: SourceReference


class RFPMetadataDraft(StrictContract):
    rfp_id: str | None = Field(max_length=160)
    client_name: str | None = Field(max_length=200)
    client_country_code: Literal["ES", "US"] | None
    client_country_original: str | None = Field(max_length=160)
    operation_country_codes: list[Literal["ES", "US"]]
    services_requested: list[ServiceRequest] = Field(min_length=1, max_length=3)
    monthly_volume_value: float | None = Field(ge=0)
    monthly_volume_unit: str | None = Field(max_length=80)
    monthly_volume_source_ref: SourceReference | None
    deadline: date | None
    deadline_source_ref: SourceReference | None
    onboarding_target: str | None = Field(max_length=300)
    onboarding_source_ref: SourceReference | None
    budget_range: BudgetRange | None
    open_questions: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_fields(self) -> "RFPMetadataDraft":
        service_ids = [item.service_id for item in self.services_requested]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError("service ids must be unique")
        if len(self.operation_country_codes) != len(set(self.operation_country_codes)):
            raise ValueError("operation country codes must be unique")
        for value, source in (
            (self.monthly_volume_value, self.monthly_volume_source_ref),
            (self.deadline, self.deadline_source_ref),
            (self.onboarding_target, self.onboarding_source_ref),
        ):
            if (value is None) != (source is None):
                raise ValueError("optional values and their evidence must be paired")
        return self


class RFPMetadata(RFPMetadataDraft):
    currency: Literal["EUR", "USD"] | None
    departments_needed: list[DepartmentId]


class KeyAspect(StrictContract):
    fact: str = Field(min_length=1, max_length=1000)
    source_ref: SourceReference
    requested_action: str = Field(min_length=1, max_length=500)


class OpenQuestion(StrictContract):
    question: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    source_ref: SourceReference | None


class DetectedConflict(StrictContract):
    description: str = Field(min_length=1, max_length=800)
    source_refs: list[SourceReference] = Field(min_length=2, max_length=6)


class DepartmentSection(StrictContract):
    department_id: DepartmentId
    key_aspects: list[KeyAspect] = Field(min_length=1, max_length=30)
    open_questions: list[OpenQuestion] = Field(max_length=20)
    source_references: list[SourceReference] = Field(min_length=1, max_length=40)
    detected_conflicts: list[DetectedConflict] = Field(max_length=10)


class SynthesisDraft(StrictContract):
    summary_for_sales: str = Field(min_length=1, max_length=4000)
    open_questions: list[OpenQuestion] = Field(max_length=30)
    detected_conflicts: list[DetectedConflict] = Field(max_length=20)


class DepartmentSectionReference(StrictContract):
    department_id: DepartmentId
    responsible: str
    ticket_id: UUID

    @model_validator(mode="after")
    def approved_responsible(self) -> "DepartmentSectionReference":
        if self.responsible != RESPONSIBLE_BY_DEPARTMENT[self.department_id]:
            raise ValueError("responsible does not match TrackFlow allowlist")
        return self


class RFPHandoff(StrictContract):
    contract_version: Literal["rfp-intake-v1"] = "rfp-intake-v1"
    ticket_id: UUID
    metadata_reference: UUID
    summary_for_sales: str = Field(min_length=1, max_length=4000)
    selected_departments: list[DepartmentId]
    department_section_references: list[DepartmentSectionReference]
    open_questions: list[OpenQuestion]
    detected_conflicts: list[DetectedConflict]
    created_at: datetime

    @model_validator(mode="after")
    def coherent_workstreams(self) -> "RFPHandoff":
        if self.metadata_reference != self.ticket_id:
            raise ValueError("handoff metadata reference must be the ticket id")
        selected = self.selected_departments
        references = [item.department_id for item in self.department_section_references]
        if len(selected) != len(set(selected)) or references != selected:
            raise ValueError("handoff department references must match selected departments")
        if any(item.ticket_id != self.ticket_id for item in self.department_section_references):
            raise ValueError("handoff section references must use the same ticket")
        if self.created_at.tzinfo is None:
            raise ValueError("handoff timestamp must be timezone-aware")
        return self


class MetricValue(StrictContract):
    name: Literal["flesch_kincaid", "gunning_fog"]
    value: float | None
    status: Literal["computed", "not_applicable", "failed"]
    language: str
    method_version: str
    reason: str | None


class ReadabilityReport(StrictContract):
    contract_version: Literal["rfp-readability-v1"] = "rfp-readability-v1"
    metrics: list[MetricValue]


class ConversionMetrics(StrictContract):
    contract_version: Literal["rfp-conversion-v1"] = "rfp-conversion-v1"
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    bytes: int = Field(ge=1, strict=True)
    page_count: int = Field(ge=1, strict=True)
    pages_with_text: int = Field(ge=0, strict=True)
    markdown_reference: str = Field(min_length=1, max_length=500)
    extracted_characters: int = Field(ge=1, strict=True)
    markdown_characters: int = Field(ge=1, strict=True)
    converter: str
    converter_version: str

    @model_validator(mode="after")
    def coherent_page_metrics(self) -> "ConversionMetrics":
        if self.pages_with_text > self.page_count:
            raise ValueError("pages_with_text cannot exceed page_count")
        reference = Path(self.markdown_reference)
        if reference.is_absolute() or ".." in reference.parts:
            raise ValueError("markdown_reference must be a safe relative path")
        return self


class RFPExecutionResult(StrictContract):
    ticket_id: UUID
    outcome: Literal["discarded", "analysis_complete"]
    classification: ClassificationDecision
    metadata: RFPMetadata | None
    sections: list[DepartmentSection]
    handoff: RFPHandoff | None


class RFPIntakeDecision(StrictContract):
    """Durable state needed to resume at the validated intake boundary."""

    version: Literal[1] = 1
    ticket_id: UUID
    markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: ClassificationDecision
    metadata: RFPMetadata
    conversion_metrics: ConversionMetrics

    @model_validator(mode="after")
    def processable(self) -> "RFPIntakeDecision":
        requested = {service.service_id for service in self.metadata.services_requested if service.requested}
        if not self.classification.is_valid_rfp or not requested or (
            self.metadata.departments_needed != [department for department in DepartmentId if department in requested]
        ):
            raise ValueError("intake decision must be valid and routed")
        return self


class RFPIntakeDecisionSink(Protocol):
    def persist_intake_decision(self, decision: RFPIntakeDecision) -> None: ...


class RFPResultSink(Protocol):
    """Persistence boundary. Implementations own the PostgreSQL transaction."""

    def persist_discard(
        self,
        *,
        ticket_id: UUID,
        classification: ClassificationDecision,
        conversion_metrics: ConversionMetrics,
        readability_metrics: ReadabilityReport,
    ) -> None: ...

    def persist_complete(
        self,
        *,
        ticket_id: UUID,
        metadata: RFPMetadata,
        sections: list[DepartmentSection],
        handoff: RFPHandoff,
        conversion_metrics: ConversionMetrics,
        readability_metrics: ReadabilityReport,
    ) -> None: ...
