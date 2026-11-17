"""Closed HTTP contracts for RFP intake."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TicketStatus = Literal["analizando", "descartado", "analisis_completo"]
JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "dead_letter"]


class RFPTicketAccepted(BaseModel):
    ticket_id: UUID
    status: Literal["analizando"] = "analizando"


class RFPJobPublic(BaseModel):
    status: JobStatus
    attempt_count: int
    max_attempts: int
    last_error_code: str | None = None
    last_error_summary: str | None = None


class RFPMetadataPublic(BaseModel):
    client_name: str | None = None
    client_country_code: Literal["ES", "US"] | None = None
    operation_country_codes: list[Literal["ES", "US"]] | None = None
    currency: Literal["EUR", "USD"] | None = None
    services_requested: list[dict[str, Any]]
    monthly_volume_value: float | None = None
    monthly_volume_unit: str | None = None
    deadline: date | None = None
    onboarding_target: str | None = None
    departments_needed: list[Literal["warehouse", "lastmile", "reverse"]]
    readability_metrics: dict[str, Any]
    conversion_metrics: dict[str, Any]


class DepartmentSectionPublic(BaseModel):
    department_id: Literal["warehouse", "lastmile", "reverse"]
    responsible: str
    key_aspects: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    detected_conflicts: list[dict[str, Any]]


class RFPHandoffPublic(BaseModel):
    contract_version: Literal["rfp-intake-v1"]
    summary_for_sales: str
    department_section_references: list[dict[str, Any]]
    open_questions: list[Any]
    detected_conflicts: list[Any]


class RFPSummary(BaseModel):
    ticket_id: UUID
    rfp_id: str | None = None
    status: TicketStatus
    client_name: str | None = None
    currency: Literal["EUR", "USD"] | None = None
    departments_needed: list[str] = Field(default_factory=list)
    job_status: JobStatus | None = None
    created_at: datetime
    updated_at: datetime


class RFPListResponse(BaseModel):
    items: list[RFPSummary]
    total: int


class RFPDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: UUID
    rfp_id: str | None = None
    status: TicketStatus
    discard_reason_code: str | None = None
    discard_summary: str | None = None
    created_at: datetime
    updated_at: datetime
    job: RFPJobPublic | None = None
    metadata: RFPMetadataPublic | None = None
    department_sections: list[DepartmentSectionPublic] = Field(default_factory=list)
    handoff: RFPHandoffPublic | None = None
