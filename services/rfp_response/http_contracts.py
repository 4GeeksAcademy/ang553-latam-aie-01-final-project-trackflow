"""Fail-closed HTTP projections for RFP response generation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.rfp_intake.contracts import JobStatus


class PublicContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseGenerationAccepted(PublicContract):
    ticket_id: UUID
    run_id: UUID
    job_id: UUID
    status: Literal["queued"] = "queued"


class ResponseGenerationStart(PublicContract):
    carrier_model: Literal["unknown", "trackflow_managed", "customer_managed"] = "unknown"


class ResponseJobPublic(PublicContract):
    job_id: UUID
    status: JobStatus
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    last_error_code: str | None = None
    last_error_summary: str | None = None


class PendingCommercialItemPublic(PublicContract):
    code: str
    description: str
    owner: str


class SafePreviewPublic(PublicContract):
    gate_status: Literal["passed", "blocked", "indeterminate", "technical_error"]
    content: str | None = None
    reason_codes: list[str]

    @model_validator(mode="after")
    def fail_closed(self) -> "SafePreviewPublic":
        if self.gate_status == "passed":
            if not self.content or self.reason_codes:
                raise ValueError("passed preview requires verified content")
        elif self.content is not None or not self.reason_codes:
            raise ValueError("unverified preview cannot expose content")
        return self


class EvaluationPublic(PublicContract):
    evaluation_id: UUID
    overall_pass: bool
    terminal_status: str | None
    actionable_feedback: list[str]
    missing_aspect_ids: list[str]
    contradicted_aspect_ids: list[str]
    blocks_commercial_completeness: bool
    requires_human_input: bool
    blocking_rule_ids: list[str]


class ResponseDepartmentPublic(PublicContract):
    department: Literal["warehouse", "lastmile", "reverse"]
    responsible: str
    status: str
    iteration_count: int = Field(ge=0, le=3)
    preview: SafePreviewPublic | None
    evaluation: EvaluationPublic | None
    pending_commercial_items: list[PendingCommercialItemPublic]


class ResponseGlobalPublic(PublicContract):
    status: Literal["not_started", "en_evaluación", "passed", "failed", "technical_failure"]
    compliance_status: Literal[
        "not_started", "en_evaluación", "passed", "failed", "technical_failure",
    ]
    compliance_pass: bool | None
    evaluation_executed: bool
    iteration_count: int = Field(ge=0, le=3)
    preview: SafePreviewPublic | None
    evaluation: EvaluationPublic | None
    pending_commercial_items: list[PendingCommercialItemPublic]

    @model_validator(mode="after")
    def coherent_compliance_projection(self) -> "ResponseGlobalPublic":
        if self.status != self.compliance_status:
            raise ValueError("global status aliases must agree")
        if self.evaluation_executed != (self.evaluation is not None):
            raise ValueError("global execution flag must match evaluation presence")
        if self.compliance_pass is None:
            if self.evaluation_executed or self.compliance_status in {"passed", "failed"}:
                raise ValueError("global compliance result is missing")
        elif (not self.evaluation_executed
              or self.compliance_status != ("passed" if self.compliance_pass else "failed")):
            raise ValueError("global compliance result and presentation disagree")
        return self


class OperationalFailurePublic(PublicContract):
    evaluator: str
    infrastructure_attempts: int = Field(ge=1)
    error_code: str
    terminal: bool
    occurred_at: datetime


class ResponseRunPublic(PublicContract):
    ticket_id: UUID
    run_id: UUID
    carrier_model: Literal["unknown", "trackflow_managed", "customer_managed"]
    status: Literal[
        "pending", "generando_borrador", "en_evaluación", "ready_for_part3",
        "needs_human_review", "technical_failure",
    ]
    outcome: Literal["ready_for_part3", "needs_human_review"] | None
    technical_failure: bool
    job: ResponseJobPublic | None
    departments: list[ResponseDepartmentPublic]
    global_terms: ResponseGlobalPublic
    operational_failures: list[OperationalFailurePublic]
    updated_at: datetime
