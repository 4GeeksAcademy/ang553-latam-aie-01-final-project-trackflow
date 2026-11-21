"""Persistence-neutral contracts for Hito 9, Parte 2."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data.pipelines.rfp_intake.contracts import DepartmentId


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, use_enum_values=False, populate_by_name=True)


class EvidenceReference(StrictContract):
    document_id: str = Field(min_length=1, max_length=160)
    section_id: str = Field(min_length=1, max_length=160)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequestedAspect(StrictContract):
    aspect_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,159}$")
    department: DepartmentId
    fact: str = Field(min_length=1, max_length=1000)
    requested_action: str = Field(min_length=1, max_length=500)
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> "RequestedAspect":
        if self.line_end < self.line_start:
            raise ValueError("requested aspect source range is reversed")
        return self


class RFPResponseStart(StrictContract):
    """Approved, persistence-neutral input for deterministic response generation."""

    contract_version: Literal["rfp-response-start-v1"] = "rfp-response-start-v1"
    ticket_id: UUID
    run_id: UUID
    source_handoff_version: Literal["rfp-intake-v1"] = "rfp-intake-v1"
    source_handoff_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary_for_sales: str = Field(min_length=1, max_length=4000)
    key_aspects: list[RequestedAspect] = Field(min_length=1, max_length=90)
    selected_departments: list[DepartmentId] = Field(min_length=1, max_length=3)
    client_country_code: Literal["US", "ES"] | None = None
    operation_countries: list[Literal["US", "ES"]] = Field(max_length=2)
    proposal_currency: Literal["USD", "EUR"] | None = None
    average_monthly_volume_m3: float | None = Field(default=None, ge=0)
    customer_managed_carrier: bool | None = None
    carrier_confirmation_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    language: Literal["en", "es"] = "en"

    @model_validator(mode="after")
    def unique_scope(self) -> "RFPResponseStart":
        if len(self.selected_departments) != len(set(self.selected_departments)):
            raise ValueError("selected departments must be unique")
        if len(self.operation_countries) != len(set(self.operation_countries)):
            raise ValueError("operation countries must be unique")
        aspect_ids = [item.aspect_id for item in self.key_aspects]
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("requested aspect ids must be unique")
        if {item.department for item in self.key_aspects} != set(self.selected_departments):
            raise ValueError("each selected department requires intake key aspects")
        return self


class MissingCommercialInput(StrictContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    description: str = Field(min_length=1, max_length=500)
    owner: Literal["Miguel Torres", "Ana Whitfield", "Carlos Vega", "Sofía Ramos"]
    blocks_commercial_completeness: Literal[True] = True


class SupportedClaim(StrictContract):
    statement: str = Field(min_length=1, max_length=1200)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=8)


class AspectResponse(StrictContract):
    aspect_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,159}$")
    response_statement: str = Field(min_length=1, max_length=1200)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=8)


class CommercialTerm(StrictContract):
    term_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    status: Literal["documented", "pending", "not_applicable"]
    value: str | None = Field(default=None, max_length=500)
    currency: Literal["EUR", "USD"] | None = None
    evidence: list[EvidenceReference] = Field(max_length=8)

    @model_validator(mode="after")
    def coherent_status(self) -> "CommercialTerm":
        if self.status == "documented" and (not self.value or not self.evidence):
            raise ValueError("documented commercial terms require value and evidence")
        if self.status != "documented" and (self.value is not None or self.currency is not None):
            raise ValueError("unknown or inapplicable terms cannot carry value or currency")
        return self


class DepartmentDraft(StrictContract):
    contract_version: Literal["rfp-department-draft-v1"] = "rfp-department-draft-v1"
    draft_id: UUID
    ticket_id: UUID
    run_id: UUID
    department: DepartmentId
    iteration_count: int = Field(ge=1, le=3, strict=True)
    draft_status: Literal["complete_candidate", "provisional_missing_inputs"]
    language: Literal["en", "es"]
    generation_feedback: list[str] = Field(default_factory=list, max_length=60)
    generated_content: str = Field(min_length=1, max_length=20_000)
    client_requirements: list[RequestedAspect] = Field(default_factory=list, max_length=30)
    aspect_responses: list[AspectResponse] = Field(default_factory=list, max_length=30)
    supported_claims: list[SupportedClaim] = Field(min_length=1, max_length=60)
    commercial_terms: list[CommercialTerm] = Field(max_length=30)
    pending_commercial_items: list[MissingCommercialInput] = Field(max_length=30)
    carrier_model: Literal["trackflow_managed", "customer_managed", "not_applicable", "pending"]
    last_mile_commitments: list[SupportedClaim] = Field(max_length=20)
    generated_at: datetime

    @model_validator(mode="after")
    def coherent_draft(self) -> "DepartmentDraft":
        if self.generated_at.tzinfo is None:
            raise ValueError("draft timestamp must be timezone-aware")
        pending = bool(self.pending_commercial_items) or any(t.status == "pending" for t in self.commercial_terms)
        if pending != (self.draft_status == "provisional_missing_inputs"):
            raise ValueError("draft status must reflect pending commercial inputs")
        ids = [term.term_id for term in self.commercial_terms]
        if len(ids) != len(set(ids)):
            raise ValueError("commercial term ids must be unique")
        requirement_ids = [item.aspect_id for item in self.client_requirements]
        response_ids = [item.aspect_id for item in self.aspect_responses]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("client requirement ids must be unique")
        if len(response_ids) != len(set(response_ids)) or not set(response_ids) <= set(requirement_ids):
            raise ValueError("aspect responses must uniquely reference client requirements")
        return self


class SafetyInspection(StrictContract):
    draft_id: UUID
    status: Literal["verified_safe", "blocked", "indeterminate", "technical_error"]
    reason_codes: list[str] = Field(max_length=20)
    inspected_at: datetime

    @model_validator(mode="after")
    def coherent_inspection(self) -> "SafetyInspection":
        if self.inspected_at.tzinfo is None:
            raise ValueError("inspection timestamp must be timezone-aware")
        if (self.status == "verified_safe") == bool(self.reason_codes):
            raise ValueError("safe inspection has no reasons; every other status requires one")
        return self


class SafePreview(StrictContract):
    preview_id: UUID
    draft_id: UUID
    gate_status: Literal["passed", "blocked", "indeterminate", "technical_error"]
    content: str | None = Field(default=None, max_length=20_000)
    reason_codes: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def fail_closed(self) -> "SafePreview":
        if self.gate_status == "passed":
            if not self.content or self.reason_codes:
                raise ValueError("passed preview requires content and no reasons")
        elif self.content is not None or not self.reason_codes:
            raise ValueError("unverified preview exposes no content and requires a reason")
        return self


class ReadabilityMetric(StrictContract):
    name: Literal["flesch_reading_ease", "flesch_kincaid_grade", "gunning_fog", "szigriszt_pazos", "average_sentence_words"]
    value: float


class ReadabilityEvaluation(StrictContract):
    contract_version: Literal["rfp-readability-evaluation-v1"] = "rfp-readability-evaluation-v1"
    execution_status: Literal["evaluated", "not_applicable"]
    language: Literal["en", "es"]
    readability_score: float | None = Field(default=None, ge=0, le=100)
    metrics: list[ReadabilityMetric] = Field(max_length=8)
    passed: bool | None = Field(alias="pass")
    not_applicable_reason: Literal["allowlisted_non_narrative_artifact"] | None = None

    @model_validator(mode="after")
    def coherent_execution(self) -> "ReadabilityEvaluation":
        if self.execution_status == "evaluated":
            if self.readability_score is None or self.passed is None or not self.metrics or self.not_applicable_reason:
                raise ValueError("evaluated readability requires score, metrics and pass")
        elif self.readability_score is not None or self.passed is not None or self.metrics or self.not_applicable_reason is None:
            raise ValueError("not-applicable readability only carries its allowlisted reason")
        return self


class RelevanceEvaluation(StrictContract):
    contract_version: Literal["rfp-relevance-evaluation-v1"] = "rfp-relevance-evaluation-v1"
    passed: bool = Field(alias="pass")
    score: float = Field(ge=0, le=100)
    feedback: list[str] = Field(max_length=20)
    aspect_results: list["AspectEvaluation"] = Field(default_factory=list, max_length=90)


class AspectEvaluation(StrictContract):
    aspect_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,159}$")
    status: Literal["addressed", "missing", "contradicted"]
    explanation: str = Field(min_length=1, max_length=800)
    supporting_claim_ids: list[str] = Field(default_factory=list, max_length=20)


class ComplianceFinding(StrictContract):
    rule_id: str = Field(pattern=r"^TF-[A-Z0-9-]+$")
    scope: Literal["section", "proposal"]
    message: str = Field(min_length=1, max_length=800)
    blocking: bool
    regenerable: bool
    requires_human_input: bool
    blocks_commercial_completeness: bool

    @model_validator(mode="after")
    def coherent_disposition(self) -> "ComplianceFinding":
        if self.requires_human_input and self.regenerable:
            raise ValueError("human-input findings cannot be regenerable")
        if self.blocks_commercial_completeness and not self.blocking:
            raise ValueError("commercial-completeness findings must be blocking")
        return self


class SectionComplianceEvaluation(StrictContract):
    contract_version: Literal["rfp-section-compliance-v1"] = "rfp-section-compliance-v1"
    passed: bool = Field(alias="pass")
    findings: list[ComplianceFinding] = Field(max_length=60)

    @model_validator(mode="after")
    def coherent_pass(self) -> "SectionComplianceEvaluation":
        if self.passed == any(f.blocking for f in self.findings) or any(f.scope != "section" for f in self.findings):
            raise ValueError("section pass or finding scope is inconsistent")
        return self


class SectionEvaluationResult(StrictContract):
    contract_version: Literal["section-evaluation-v1"] = "section-evaluation-v1"
    evaluation_id: UUID
    ticket_id: UUID
    run_id: UUID
    department: DepartmentId
    draft_id: UUID
    draft_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    iteration_count: int = Field(ge=1, le=3, strict=True)
    readability: ReadabilityEvaluation
    relevance: RelevanceEvaluation
    compliance: SectionComplianceEvaluation
    actionable_feedback: list[str] = Field(max_length=60)
    missing_aspect_ids: list[str] = Field(max_length=90)
    contradicted_aspect_ids: list[str] = Field(max_length=90)
    blocks_commercial_completeness: bool
    blocking_rule_ids: list[str]
    requires_human_input: bool
    regenerable: bool
    overall_pass: bool
    terminal_status: Literal["passed", "needs_human_input", "iteration_limit_reached"] | None
    evaluated_at: datetime

    @model_validator(mode="after")
    def coherent_result(self) -> "SectionEvaluationResult":
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluation timestamp must be timezone-aware")
        blocking = [f for f in self.compliance.findings if f.blocking]
        ids = [f.rule_id for f in blocking]
        blocks = any(f.blocks_commercial_completeness for f in blocking)
        human = any(f.requires_human_input for f in blocking)
        missing = [item.aspect_id for item in self.relevance.aspect_results if item.status == "missing"]
        contradicted = [item.aspect_id for item in self.relevance.aspect_results if item.status == "contradicted"]
        passed = self.readability.passed is not False and self.relevance.passed and self.compliance.passed and not blocks and not human
        evaluator_failed = self.readability.passed is False or not self.relevance.passed
        regenerable = (
            not passed
            and not human
            and self.iteration_count < 3
            and (evaluator_failed or bool(blocking))
            and all(f.regenerable for f in blocking)
        )
        terminal = "passed" if passed else "needs_human_input" if human else "iteration_limit_reached" if self.iteration_count == 3 else None
        expected_feedback = list(self.relevance.feedback)
        expected_feedback.extend(f.message for f in blocking)
        if self.readability.passed is False:
            expected_feedback.append("Shorten sentences and use clearer language before regenerating the section.")
        expected_feedback = expected_feedback[:60]
        if (self.blocking_rule_ids, self.blocks_commercial_completeness,
                self.requires_human_input, self.overall_pass, self.regenerable,
                self.terminal_status, self.missing_aspect_ids,
                self.contradicted_aspect_ids, self.actionable_feedback) != (
                ids, blocks, human, passed, regenerable, terminal, missing,
                contradicted, expected_feedback):
            raise ValueError("section result violates evaluator invariants")
        return self


class GlobalCurrency(StrictContract):
    value: Literal["EUR", "USD"] | None
    status: Literal["documented", "pending"]
    evidence: list[EvidenceReference] = Field(max_length=8)

    @model_validator(mode="after")
    def coherent_currency(self) -> "GlobalCurrency":
        if self.status == "documented" and (self.value is None or not self.evidence):
            raise ValueError("documented currency requires value and evidence")
        if self.status == "pending" and (self.value is not None or self.evidence):
            raise ValueError("pending currency cannot claim value or evidence")
        return self


class GlobalSLA(StrictContract):
    applicability: Literal["applicable", "not_applicable", "pending"]
    percentage: float | None = Field(default=None, ge=0, le=100)
    measurement_period: str | None = Field(default=None, max_length=160)
    scope: list[str] = Field(max_length=20)
    exclusions: list[str] = Field(max_length=20)
    evidence: list[EvidenceReference] = Field(max_length=8)

    @model_validator(mode="after")
    def coherent_sla(self) -> "GlobalSLA":
        if self.applicability == "applicable":
            if self.percentage is None or not self.measurement_period or not self.scope or not self.evidence:
                raise ValueError("applicable SLA requires percentage, period, scope and evidence")
        elif self.percentage is not None or self.measurement_period is not None or self.evidence:
            raise ValueError("non-applicable or pending SLA cannot claim documented terms")
        return self


class VolumeDiscountRow(StrictContract):
    volume_from: float | None = Field(default=None, ge=0)
    volume_to: float | None = Field(default=None, ge=0)
    discount_percentage: float | None = Field(default=None, ge=0, le=100)
    condition: str = Field(min_length=1, max_length=500)
    status: Literal["documented", "pending"]
    evidence: list[EvidenceReference] = Field(max_length=8)

    @model_validator(mode="after")
    def coherent_discount(self) -> "VolumeDiscountRow":
        if self.volume_from is not None and self.volume_to is not None and self.volume_to <= self.volume_from:
            raise ValueError("discount volume bounds must be ordered")
        if self.status == "documented" and (self.discount_percentage is None or not self.evidence):
            raise ValueError("documented discount requires percentage and evidence")
        if self.status == "pending" and self.discount_percentage is not None:
            raise ValueError("pending discount cannot claim a percentage")
        return self


class GlobalCommercialTermsDraft(StrictContract):
    contract_version: Literal["rfp-global-commercial-terms-v1"] = "rfp-global-commercial-terms-v1"
    draft_id: UUID
    ticket_id: UUID
    run_id: UUID
    global_iteration_count: int = Field(ge=1, le=3, strict=True)
    generation_feedback: list[str] = Field(default_factory=list, max_length=60)
    currency: GlobalCurrency
    sla: GlobalSLA
    volume_discounts: list[VolumeDiscountRow] = Field(max_length=30)
    source_section_ids: list[UUID]
    pending_commercial_items: list[MissingCommercialInput] = Field(max_length=30)
    generated_at: datetime

    @model_validator(mode="after")
    def aware(self) -> "GlobalCommercialTermsDraft":
        if self.generated_at.tzinfo is None:
            raise ValueError("global draft timestamp must be timezone-aware")
        return self


class ConsistencyIssue(StrictContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    description: str = Field(min_length=1, max_length=800)
    resolvable_from_evidence: bool


class GlobalComplianceEvaluation(StrictContract):
    contract_version: Literal["rfp-global-compliance-v1"] = "rfp-global-compliance-v1"
    passed: bool = Field(alias="pass")
    findings: list[ComplianceFinding] = Field(max_length=60)

    @model_validator(mode="after")
    def coherent_pass(self) -> "GlobalComplianceEvaluation":
        if self.passed == any(f.blocking for f in self.findings) or any(f.scope != "proposal" for f in self.findings):
            raise ValueError("global pass or finding scope is inconsistent")
        return self


class ProposalEvaluationResult(StrictContract):
    contract_version: Literal["proposal-evaluation-v1"] = "proposal-evaluation-v1"
    evaluation_id: UUID
    ticket_id: UUID
    run_id: UUID
    section_evaluation_ids: list[UUID] = Field(min_length=1)
    global_draft_id: UUID
    global_iteration_count: int = Field(ge=1, le=3, strict=True)
    global_compliance: GlobalComplianceEvaluation
    all_departments_passed: bool
    blocks_commercial_completeness: bool
    requires_human_input: bool
    blocking_rule_ids: list[str]
    regenerable: bool
    overall_pass: bool
    outcome: Literal["ready_for_part3", "needs_human_review", "regenerate"]
    terminal_status: Literal["ready_for_part3", "needs_human_input", "iteration_limit_reached"] | None
    evaluated_at: datetime

    @model_validator(mode="after")
    def coherent_result(self) -> "ProposalEvaluationResult":
        if self.evaluated_at.tzinfo is None:
            raise ValueError("proposal evaluation timestamp must be timezone-aware")
        blocking = [f for f in self.global_compliance.findings if f.blocking]
        blocks = any(f.blocks_commercial_completeness for f in blocking)
        human = any(f.requires_human_input for f in blocking)
        passed = self.all_departments_passed and self.global_compliance.passed and not blocks and not human
        regenerable = not passed and not human and bool(blocking) and self.global_iteration_count < 3 and all(f.regenerable for f in blocking)
        outcome = "ready_for_part3" if passed else "regenerate" if regenerable else "needs_human_review"
        terminal = "ready_for_part3" if passed else "needs_human_input" if human else "iteration_limit_reached" if self.global_iteration_count == 3 else None
        values = ([f.rule_id for f in blocking], blocks, human, regenerable, passed, outcome, terminal)
        if (self.blocking_rule_ids, self.blocks_commercial_completeness, self.requires_human_input, self.regenerable, self.overall_pass, self.outcome, self.terminal_status) != values:
            raise ValueError("proposal result violates evaluator invariants")
        return self


class DepartmentHandoffResult(StrictContract):
    department: DepartmentId
    terminal_status: Literal["passed", "needs_human_input", "iteration_limit_reached"]
    iteration_count: int = Field(ge=1, le=3, strict=True)
    final_draft_id: UUID
    evaluation_result_id: UUID
    safe_preview_id: UUID | None
    blocks_commercial_completeness: bool
    requires_human_input: bool
    blocking_rule_ids: list[str]

    @model_validator(mode="after")
    def coherent_terminal_result(self) -> "DepartmentHandoffResult":
        clean = (not self.blocks_commercial_completeness
                 and not self.requires_human_input and not self.blocking_rule_ids)
        if self.terminal_status == "passed" and not clean:
            raise ValueError("passed department handoff result cannot retain blockers")
        return self


class RFPResponseHandoff(StrictContract):
    contract_version: Literal["rfp-response-v1"] = "rfp-response-v1"
    ticket_id: UUID
    run_id: UUID
    source_handoff_version: Literal["rfp-intake-v1"] = "rfp-intake-v1"
    source_handoff_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: Literal["ready_for_part3", "needs_human_review"]
    all_sections_passed: bool
    global_compliance_passed: bool
    selected_departments: list[DepartmentId] = Field(min_length=1, max_length=3)
    department_results: list[DepartmentHandoffResult] = Field(min_length=1, max_length=3)
    global_draft_id: UUID
    global_evaluation_result_id: UUID
    unresolved_items: list[MissingCommercialInput] = Field(max_length=60)
    created_at: datetime

    @model_validator(mode="after")
    def complete_for_part3(self) -> "RFPResponseHandoff":
        if self.created_at.tzinfo is None:
            raise ValueError("handoff timestamp must be timezone-aware")
        if len(self.selected_departments) != len(set(self.selected_departments)) or [r.department for r in self.department_results] != self.selected_departments:
            raise ValueError("handoff results must match unique selected departments in order")
        all_passed = all(
            r.terminal_status == "passed"
            and not r.blocks_commercial_completeness
            and not r.requires_human_input
            and not r.blocking_rule_ids
            for r in self.department_results
        )
        ready = all_passed and self.global_compliance_passed and not self.unresolved_items
        if self.all_sections_passed != all_passed or ((self.outcome == "ready_for_part3") != ready):
            raise ValueError("ready_for_part3 requires every automatic evaluation to pass")
        return self


class EvaluatorExecutionFailure(StrictContract):
    """Operational evidence; deliberately not an EvaluationResult or handoff."""
    failure_id: UUID
    ticket_id: UUID
    run_id: UUID
    draft_id: UUID | None
    evaluator: Literal[
        "department_generator", "readability", "relevance",
        "section_compliance", "global_compliance",
    ]
    department: DepartmentId | None = None
    infrastructure_attempts: int = Field(ge=1, strict=True)
    error_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    diagnostic_reference: str = Field(min_length=1, max_length=500)
    terminal: bool
    occurred_at: datetime

    @model_validator(mode="after")
    def aware(self) -> "EvaluatorExecutionFailure":
        if self.occurred_at.tzinfo is None:
            raise ValueError("failure timestamp must be timezone-aware")
        reference = PurePosixPath(self.diagnostic_reference)
        if reference.is_absolute() or ".." in reference.parts or "://" in self.diagnostic_reference:
            raise ValueError("diagnostic reference must be a safe relative identifier")
        return self


class LLMInvocationRecord(StrictContract):
    """Content-free evidence for one attempted external generation call."""

    invocation_id: UUID
    ticket_id: UUID
    run_id: UUID
    artifact_id: UUID
    provider: Literal["fourgeeks_gateway"]
    model: str = Field(min_length=1, max_length=200)
    stage: Literal["department_generation", "relevance_evaluation"]
    department: DepartmentId
    call_index: int = Field(ge=1, strict=True)
    provider_request_id: str | None = Field(default=None, max_length=200)
    latency_ms: float = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0, strict=True)
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    outcome: Literal["succeeded", "technical_error", "invalid_output"]
    error_category: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    validation_path: str | None = Field(
        default=None, pattern=r"^[a-z0-9_.\[\]-]{1,200}$")
    validation_error_type: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    validation_received_type: Literal[
        "null", "boolean", "integer", "number", "string", "array", "object", "unknown"
    ] | None = None
    violated_constraint: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def coherent_invocation(self) -> "LLMInvocationRecord":
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("invocation timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("invocation finish precedes its start")
        if (self.outcome == "succeeded") == bool(self.error_category):
            raise ValueError("successful calls have no error; failures require a category")
        validation_metadata = (
            self.validation_path, self.validation_error_type,
            self.validation_received_type, self.violated_constraint,
        )
        if any(value is not None for value in validation_metadata):
            if self.outcome != "invalid_output" or not all(
                    value is not None for value in validation_metadata):
                raise ValueError(
                    "validation metadata is complete and limited to invalid output")
        if self.total_tokens is not None:
            known = [value for value in (self.prompt_tokens, self.completion_tokens)
                     if value is not None]
            if known and self.total_tokens < sum(known):
                raise ValueError("total token usage is smaller than known components")
        return self
