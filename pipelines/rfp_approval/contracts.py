"""Strict, persistence-neutral contracts for Hito 9, Parte 3.

These models validate structure and internal coherence only. An
``authorization_id`` is an immutable reference, not proof that an authorization
is active; PostgreSQL-backed authorization belongs to a later gate.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    CommercialTerm,
    EvidenceReference,
    GlobalCurrency,
    GlobalSLA,
    MissingCommercialInput,
    VolumeDiscountRow,
)
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value


Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ReviewAction = Literal["approve", "reject", "request_changes"]

INDICATIVE_COMMERCIAL_INPUT_CODES = frozenset(
    {
        "LAST_MILE_RATE_REQUIRED",
        "RETURN_PROCESSING_RATE_REQUIRED",
        "MONTHLY_STORAGE_VOLUME_REQUIRED",
        "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
    }
)
INDICATIVE_APPROVAL_ACK_PREFIX = "[indicative-commercial-ack:v1]"


def indicative_warnings_are_reviewable(
    *, blocking_rule_ids: list[str], unresolved_input_codes: list[str]
) -> bool:
    """Return true only for the academic, explicitly disclosed commercial gaps."""

    return (
        not blocking_rule_ids
        and set(unresolved_input_codes).issubset(INDICATIVE_COMMERCIAL_INPUT_CODES)
    )


def _require_commercial_completeness_coherence(
    *, blocks_commercial_completeness: bool, unresolved_input_codes: list[str]
) -> None:
    if blocks_commercial_completeness != bool(unresolved_input_codes):
        raise ValueError(
            "commercial completeness flag disagrees with unresolved inputs"
        )


def indicative_approval_reason(
    pending_codes: list[str], user_reason: str | None = None
) -> str:
    """Build the durable acknowledgement stored with an indicative approval."""

    codes = ",".join(sorted(set(pending_codes)))
    acknowledgement = f"{INDICATIVE_APPROVAL_ACK_PREFIX} {codes}"
    if user_reason and user_reason.strip():
        return f"{acknowledgement} | {user_reason.strip()}"
    return acknowledgement


def acknowledges_indicative_commercial_items(
    reason: str | None, pending_codes: list[str]
) -> bool:
    expected = indicative_approval_reason(pending_codes)
    return bool(reason) and (reason == expected or reason.startswith(expected + " | "))
TriggerId = Literal[
    "volume-vs-capacity",
    "returns-sla-breach",
    "currency-mismatch",
]
OfficialResponsible = Literal[
    "Ana Whitfield",
    "Carlos Vega",
    "Sofía Ramos",
    "Miguel Torres",
]
Currency = Literal["USD", "EUR"]

_RESPONSIBLE_BY_DEPARTMENT: dict[DepartmentId, str] = {
    DepartmentId.warehouse: "Ana Whitfield",
    DepartmentId.lastmile: "Carlos Vega",
    DepartmentId.reverse: "Sofía Ramos",
}


class StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, use_enum_values=False, populate_by_name=True
    )


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _unique(values: list[object], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _markdown_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


class AuthenticatedPrincipal(StrictContract):
    issuer: str = Field(min_length=1, max_length=300)
    subject: str = Field(min_length=1, max_length=300)
    trackflow_user_id: str = Field(min_length=1, max_length=128)
    auth_source: Literal["local", "oidc"]
    general_role: Literal["admin", "manager", "user"]
    synthetic: bool = False

    @model_validator(mode="after")
    def coherent_source(self) -> "AuthenticatedPrincipal":
        qa_issuer = self.issuer.startswith("urn:trackflow:qa")
        if self.synthetic != qa_issuer:
            raise ValueError("synthetic principals require the isolated QA issuer")
        if self.auth_source == "local" and self.issuer not in {
            "trackflow-local",
            "urn:trackflow:qa:local",
        }:
            raise ValueError("local principals require the TrackFlow local issuer")
        return self


class RunInitiator(StrictContract):
    principal: AuthenticatedPrincipal
    authorization_id: UUID
    permission: Literal["start_approval_run", "manage_approval_run"]


class AuthorizedHumanActor(StrictContract):
    """Structurally scoped actor; active authorization is checked later."""

    principal: AuthenticatedPrincipal
    authorization_id: UUID
    official_responsible: OfficialResponsible
    permission: Literal[
        "approve_department", "provide_business_input", "arbitrate"
    ]
    department_scope: DepartmentId | None = None
    trigger_scope: TriggerId | None = None

    @model_validator(mode="after")
    def coherent_scope(self) -> "AuthorizedHumanActor":
        if self.permission == "approve_department":
            if self.department_scope is None or self.trigger_scope is not None:
                raise ValueError("department approval requires only a department scope")
            if self.official_responsible != _RESPONSIBLE_BY_DEPARTMENT[self.department_scope]:
                raise ValueError("approver does not match the official department owner")
        elif self.permission == "provide_business_input":
            if self.trigger_scope is not None:
                raise ValueError("business-input actors cannot carry an arbitration trigger")
            if self.official_responsible == "Miguel Torres":
                if self.department_scope is not None:
                    raise ValueError("Miguel's commercial input scope is proposal-wide")
            elif self.department_scope is None or (
                self.official_responsible
                != _RESPONSIBLE_BY_DEPARTMENT[self.department_scope]
            ):
                raise ValueError("business-input actor and department disagree")
        else:
            if self.trigger_scope is None:
                raise ValueError("arbitration requires a trigger scope")
            if self.official_responsible == "Sofía Ramos":
                if (
                    self.trigger_scope != "returns-sla-breach"
                    or self.department_scope != DepartmentId.reverse
                ):
                    raise ValueError("Sofía may arbitrate only reverse return-SLA breaches")
            elif self.official_responsible == "Miguel Torres":
                if self.department_scope is not None:
                    raise ValueError("Miguel's arbitration scope is cross-departmental")
            else:
                raise ValueError("actor is not an official TrackFlow arbitrator")
        return self


class InitialRevisionReference(StrictContract):
    department: DepartmentId
    source_draft_id: UUID
    source_draft_hash: Hash64
    source_evaluation_id: UUID
    source_evaluation_hash: Hash64
    source_part2_iteration: int = Field(ge=1, le=3)
    terminal_status: Literal["passed", "needs_human_input", "iteration_limit_reached"]


class ApprovalRunStart(StrictContract):
    contract_version: Literal["rfp-approval-start-v1"] = "rfp-approval-start-v1"
    approval_run_id: UUID
    ticket_id: UUID
    response_run_id: UUID
    response_handoff_version: Literal["rfp-response-v1"] = "rfp-response-v1"
    response_handoff_hash: Hash64
    active_departments: list[DepartmentId] = Field(min_length=1, max_length=3)
    initial_revisions: list[InitialRevisionReference] = Field(
        min_length=1, max_length=3
    )
    max_review_revisions: Literal[3] = 3
    started_by: RunInitiator
    automate_proposal_materialization: bool = False
    started_at: datetime

    @model_validator(mode="after")
    def coherent_start(self) -> "ApprovalRunStart":
        _unique(list(self.active_departments), "active_departments")
        if (
            self.automate_proposal_materialization
            and self.started_by.permission != "manage_approval_run"
        ):
            raise ValueError(
                "automatic proposal materialization requires run management"
            )
        referenced = [item.department for item in self.initial_revisions]
        if referenced != self.active_departments:
            raise ValueError("initial revisions must match active departments in order")
        _require_aware(self.started_at, "started_at")
        return self


ArtifactType = Literal[
    "draft",
    "evaluation",
    "safe_preview",
    "revision_validation",
    "global_evaluation",
    "conflict_recheck",
]


class ImmutableArtifactReference(StrictContract):
    artifact_type: ArtifactType
    artifact_id: UUID
    contract_version: str = Field(min_length=1, max_length=100)
    payload_hash: Hash64
    evaluated_content_hash: Hash64


class RevisionValidationReferences(StrictContract):
    revision_content_hash: Hash64
    evaluation: ImmutableArtifactReference
    safe_preview: ImmutableArtifactReference
    validation_hash: Hash64
    validated_at: datetime

    @model_validator(mode="after")
    def tied_to_revised_content(self) -> "RevisionValidationReferences":
        _require_aware(self.validated_at, "validated_at")
        if self.evaluation.artifact_type != "evaluation":
            raise ValueError("evaluation reference has the wrong artifact type")
        if self.safe_preview.artifact_type != "safe_preview":
            raise ValueError("safe preview reference has the wrong artifact type")
        if {
            self.revision_content_hash,
            self.evaluation.evaluated_content_hash,
            self.safe_preview.evaluated_content_hash,
        } != {self.revision_content_hash}:
            raise ValueError("evaluation and preview must target the revised content")
        expected = canonical_hash(
            {
                "revision_content_hash": self.revision_content_hash,
                "evaluation": self.evaluation,
                "safe_preview": self.safe_preview,
                "validated_at": self.validated_at,
            }
        )
        if self.validation_hash != expected:
            raise ValueError("revision validation hash is invalid")
        return self


class ApprovalRevisionValidation(StrictContract):
    contract_version: Literal[
        "rfp-approval-revision-validation-v1"
    ] = "rfp-approval-revision-validation-v1"
    references: RevisionValidationReferences
    mandatory_compliance_passed: bool
    blocks_commercial_completeness: bool
    blocking_rule_ids: list[str] = Field(max_length=60)
    unresolved_input_codes: list[str] = Field(max_length=60)
    open_conflict_ids: list[UUID] = Field(max_length=30)
    global_revalidation_required: bool
    eligible_for_approval: bool

    @model_validator(mode="after")
    def coherent_eligibility(self) -> "ApprovalRevisionValidation":
        _require_commercial_completeness_coherence(
            blocks_commercial_completeness=(
                self.blocks_commercial_completeness
            ),
            unresolved_input_codes=self.unresolved_input_codes,
        )
        expected = (
            self.mandatory_compliance_passed
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=self.blocking_rule_ids,
                unresolved_input_codes=self.unresolved_input_codes,
            )
            and not self.open_conflict_ids
        )
        if self.eligible_for_approval != expected:
            raise ValueError("approval eligibility disagrees with revision blockers")
        _unique(list(self.blocking_rule_ids), "blocking_rule_ids")
        _unique(list(self.unresolved_input_codes), "unresolved_input_codes")
        _unique(list(self.open_conflict_ids), "open_conflict_ids")
        return self


RevisionStatus = Literal[
    "pending_assessment",
    "blocked_input",
    "blocked_compliance",
    "blocked_conflict",
    "pending_approval",
    "approved",
    "rejected",
    "changes_requested",
    "superseded",
    "limit_reached",
]


def department_revision_content_hash(
    *,
    content: str,
    commercial_terms: list[CommercialTerm],
    pending_commercial_items: list[MissingCommercialInput],
    evidence: list[EvidenceReference],
) -> str:
    return canonical_hash(
        {
            "content": content,
            "commercial_terms": commercial_terms,
            "pending_commercial_items": pending_commercial_items,
            "evidence": evidence,
        }
    )


class DepartmentRevision(StrictContract):
    contract_version: Literal[
        "rfp-department-approval-revision-v1"
    ] = "rfp-department-approval-revision-v1"
    revision_id: UUID
    draft_id: UUID
    ticket_id: UUID
    approval_run_id: UUID
    department: DepartmentId
    review_revision_no: int = Field(ge=1, le=3)
    source_part2_draft_id: UUID
    source_part2_draft_hash: Hash64
    source_part2_evaluation_id: UUID
    source_part2_evaluation_hash: Hash64
    source_part2_iteration: int = Field(ge=1, le=3)
    supersedes_revision_id: UUID | None = None
    superseded_by_revision_id: UUID | None = None
    content: str = Field(min_length=1, max_length=20_000)
    content_hash: Hash64
    commercial_terms: list[CommercialTerm] = Field(max_length=30)
    pending_commercial_items: list[MissingCommercialInput] = Field(max_length=30)
    evidence: list[EvidenceReference] = Field(max_length=100)
    validation: ApprovalRevisionValidation
    validation_bundle_id: UUID | None = None
    status: RevisionStatus
    applied_decision_id: UUID | None = None
    created_by: AuthorizedHumanActor | None = None
    creation_reason: Literal[
        "part2_import",
        "authorized_input",
        "human_request_changes",
        "arbitration_request_changes",
    ]
    created_at: datetime

    @model_validator(mode="after")
    def coherent_revision(self) -> "DepartmentRevision":
        _require_aware(self.created_at, "created_at")
        expected_hash = department_revision_content_hash(
            content=self.content,
            commercial_terms=self.commercial_terms,
            pending_commercial_items=self.pending_commercial_items,
            evidence=self.evidence,
        )
        if self.content_hash != expected_hash:
            raise ValueError("department revision content hash is invalid")
        if self.validation.references.revision_content_hash != self.content_hash:
            raise ValueError("revision validation targets different content")
        if self.creation_reason == "part2_import":
            if (
                self.review_revision_no != 1
                or self.supersedes_revision_id is not None
                or self.created_by is not None
            ):
                raise ValueError("Part 2 import must be the first system revision")
        elif (
            self.review_revision_no == 1
            or self.supersedes_revision_id is None
            or self.created_by is None
        ):
            raise ValueError("corrected revisions require actor and predecessor")
        if self.created_by is not None:
            if self.created_by.permission != "provide_business_input":
                raise ValueError("revision creator lacks business-input scope")
            if self.created_by.department_scope not in {None, self.department}:
                raise ValueError("revision creator and department disagree")
        if self.pending_commercial_items and self.status not in {
            "blocked_input",
            "blocked_compliance",
            "blocked_conflict",
            "pending_assessment",
            "pending_approval",
            "approved",
            "superseded",
        }:
            raise ValueError("pending commercial inputs require review or supersession")
        if self.status == "blocked_input" and not self.pending_commercial_items:
            raise ValueError("blocked-input revision requires a pending item")
        if self.status == "blocked_compliance":
            compliance_precedence = (
                (
                    not self.validation.mandatory_compliance_passed
                    or bool(self.validation.blocking_rule_ids)
                )
            )
            if not compliance_precedence:
                raise ValueError(
                    "blocked-compliance revision requires compliance blockers"
                )
        if self.status == "blocked_conflict":
            conflict_only = (
                self.validation.mandatory_compliance_passed
                and not self.validation.blocking_rule_ids
                and bool(self.validation.open_conflict_ids)
            )
            if not conflict_only:
                raise ValueError(
                    "blocked-conflict revision requires conflict-only blockers"
                )
        if self.status == "pending_approval" and not self.validation.eligible_for_approval:
            raise ValueError("ineligible revision cannot await approval")
        if (
            self.status == "pending_assessment"
            and not self.validation.eligible_for_approval
        ):
            raise ValueError("ineligible revision cannot await conflict assessment")
        if self.status == "limit_reached":
            if self.review_revision_no != 3:
                raise ValueError("review limit can be reached only on revision 3")
            if self.validation.eligible_for_approval:
                raise ValueError("eligible revision cannot be marked limit-reached")
        if self.status == "superseded":
            if self.superseded_by_revision_id is None:
                raise ValueError("superseded revision requires its successor")
            if self.superseded_by_revision_id == self.revision_id:
                raise ValueError("revision cannot supersede itself")
        elif self.superseded_by_revision_id is not None:
            raise ValueError("only superseded revisions may name a successor")
        decision_states = {"approved", "rejected", "changes_requested"}
        if (self.status in decision_states) != (self.applied_decision_id is not None):
            raise ValueError("decision state and applied decision reference disagree")
        if self.status == "approved" and not self.validation.eligible_for_approval:
            raise ValueError("ineligible revision cannot be approved")
        return self


class ProposalRevisionValidation(StrictContract):
    contract_version: Literal[
        "rfp-proposal-revision-validation-v1",
        "rfp-proposal-revision-validation-v2",
    ] = "rfp-proposal-revision-validation-v1"
    revision_content_hash: Hash64
    global_evaluation: ImmutableArtifactReference
    safe_preview: ImmutableArtifactReference
    mandatory_compliance_passed: bool
    unresolved_input_codes: list[str] = Field(max_length=60)
    blocking_rule_ids: list[str] = Field(max_length=60)
    open_conflict_ids: list[UUID] = Field(max_length=30)
    source_departments_approved: bool | None = None
    eligible_for_synthesis: bool
    validation_hash: Hash64
    validated_at: datetime

    @model_validator(mode="after")
    def coherent_validation(self) -> "ProposalRevisionValidation":
        _require_aware(self.validated_at, "validated_at")
        if self.global_evaluation.artifact_type != "global_evaluation":
            raise ValueError("proposal revision requires a global evaluation")
        if self.safe_preview.artifact_type != "safe_preview":
            raise ValueError("proposal revision requires a safe preview")
        if {
            self.revision_content_hash,
            self.global_evaluation.evaluated_content_hash,
            self.safe_preview.evaluated_content_hash,
        } != {self.revision_content_hash}:
            raise ValueError("global evaluation and preview target stale content")
        if self.contract_version == "rfp-proposal-revision-validation-v1":
            if self.source_departments_approved is not None:
                raise ValueError("V1 validation cannot assert department approvals")
            departments_ready = True
        else:
            if self.source_departments_approved is None:
                raise ValueError("V2 validation requires department approval state")
            departments_ready = self.source_departments_approved
        expected = (
            departments_ready
            and self.mandatory_compliance_passed
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=self.blocking_rule_ids,
                unresolved_input_codes=self.unresolved_input_codes,
            )
            and not self.open_conflict_ids
        )
        if self.eligible_for_synthesis != expected:
            raise ValueError("synthesis eligibility disagrees with global blockers")
        hashed = {
            "revision_content_hash": self.revision_content_hash,
            "global_evaluation": self.global_evaluation,
            "safe_preview": self.safe_preview,
            "mandatory_compliance_passed": self.mandatory_compliance_passed,
            "unresolved_input_codes": self.unresolved_input_codes,
            "blocking_rule_ids": self.blocking_rule_ids,
            "open_conflict_ids": self.open_conflict_ids,
            "eligible_for_synthesis": self.eligible_for_synthesis,
            "validated_at": self.validated_at,
        }
        if self.contract_version == "rfp-proposal-revision-validation-v2":
            hashed["source_departments_approved"] = (
                self.source_departments_approved
            )
        expected_hash = canonical_hash(hashed)
        if self.validation_hash != expected_hash:
            raise ValueError("proposal validation hash is invalid")
        return self


def proposal_revision_content_hash(
    *,
    currency: GlobalCurrency,
    sla: GlobalSLA,
    volume_discounts: list[VolumeDiscountRow],
    source_department_revision_ids: list[UUID],
    authorized_input_ids: list[UUID],
) -> str:
    return canonical_hash(
        {
            "currency": currency,
            "sla": sla,
            "volume_discounts": volume_discounts,
            "source_department_revision_ids": source_department_revision_ids,
            "authorized_input_ids": authorized_input_ids,
        }
    )


class ProposalRevision(StrictContract):
    contract_version: Literal["rfp-proposal-revision-v1"] = "rfp-proposal-revision-v1"
    revision_id: UUID
    approval_run_id: UUID
    ticket_id: UUID
    revision_no: int = Field(ge=1, le=3)
    source_global_draft_id: UUID
    source_global_draft_hash: Hash64
    source_global_evaluation_id: UUID
    source_global_evaluation_hash: Hash64
    supersedes_revision_id: UUID | None = None
    currency: GlobalCurrency
    sla: GlobalSLA
    volume_discounts: list[VolumeDiscountRow] = Field(max_length=30)
    source_department_revision_ids: list[UUID] = Field(min_length=1, max_length=3)
    authorized_input_ids: list[UUID] = Field(max_length=60)
    content_hash: Hash64
    validation: ProposalRevisionValidation
    validation_bundle_id: UUID | None = None
    created_at: datetime

    @model_validator(mode="after")
    def coherent_revision(self) -> "ProposalRevision":
        _require_aware(self.created_at, "created_at")
        _unique(list(self.source_department_revision_ids), "source department revisions")
        _unique(list(self.authorized_input_ids), "authorized inputs")
        if (self.revision_no == 1) != (self.supersedes_revision_id is None):
            raise ValueError("proposal predecessor and revision number disagree")
        expected = proposal_revision_content_hash(
            currency=self.currency,
            sla=self.sla,
            volume_discounts=self.volume_discounts,
            source_department_revision_ids=self.source_department_revision_ids,
            authorized_input_ids=self.authorized_input_ids,
        )
        if self.content_hash != expected:
            raise ValueError("proposal revision content hash is invalid")
        if self.validation.revision_content_hash != self.content_hash:
            raise ValueError("proposal validation targets different content")
        return self


ApprovalArtifactScope = Literal["warehouse", "lastmile", "reverse", "proposal"]


class ApprovalArtifactProvenance(StrictContract):
    contract_version: Literal[
        "rfp-approval-artifact-provenance-v1"
    ] = "rfp-approval-artifact-provenance-v1"
    producer_node: str = Field(min_length=1, max_length=200)
    execution_id: UUID
    source_kind: Literal[
        "part2_import",
        "human_correction",
        "deterministic_evaluator",
        "compliance_evaluator",
    ]
    source_artifact_ids: list[UUID] = Field(max_length=60)
    source_hashes: list[Hash64] = Field(max_length=60)
    produced_at: datetime
    provenance_hash: Hash64

    @model_validator(mode="after")
    def coherent_provenance(self) -> "ApprovalArtifactProvenance":
        _require_aware(self.produced_at, "produced_at")
        _unique(list(self.source_artifact_ids), "source artifact ids")
        if len(self.source_artifact_ids) != len(self.source_hashes):
            raise ValueError("source artifact ids and hashes must align")
        expected = canonical_hash(
            {
                "producer_node": self.producer_node,
                "execution_id": self.execution_id,
                "source_kind": self.source_kind,
                "source_artifact_ids": self.source_artifact_ids,
                "source_hashes": self.source_hashes,
                "produced_at": self.produced_at,
            }
        )
        if self.provenance_hash != expected:
            raise ValueError("artifact provenance hash is invalid")
        return self


class DepartmentCorrectedDraftPayload(StrictContract):
    payload_kind: Literal["department_corrected_draft"] = (
        "department_corrected_draft"
    )
    department: DepartmentId
    content: str = Field(min_length=1, max_length=20_000)
    commercial_terms: list[CommercialTerm] = Field(max_length=30)
    pending_commercial_items: list[MissingCommercialInput] = Field(max_length=30)
    evidence: list[EvidenceReference] = Field(max_length=100)


class ProposalCorrectedDraftPayload(StrictContract):
    payload_kind: Literal["proposal_corrected_draft"] = "proposal_corrected_draft"
    currency: GlobalCurrency
    sla: GlobalSLA
    volume_discounts: list[VolumeDiscountRow] = Field(max_length=30)
    source_department_revision_ids: list[UUID] = Field(min_length=1, max_length=3)
    authorized_input_ids: list[UUID] = Field(max_length=60)

    @model_validator(mode="after")
    def unique_references(self) -> "ProposalCorrectedDraftPayload":
        _unique(
            list(self.source_department_revision_ids),
            "source department revisions",
        )
        _unique(list(self.authorized_input_ids), "authorized inputs")
        return self


CorrectedDraftPayload = Annotated[
    Union[DepartmentCorrectedDraftPayload, ProposalCorrectedDraftPayload],
    Field(discriminator="payload_kind"),
]


class CorrectedDraftArtifact(StrictContract):
    contract_version: Literal[
        "rfp-approval-corrected-draft-v1"
    ] = "rfp-approval-corrected-draft-v1"
    draft_artifact_id: UUID
    approval_run_id: UUID
    ticket_id: UUID
    scope_key: ApprovalArtifactScope
    payload: CorrectedDraftPayload
    content_hash: Hash64
    payload_hash: Hash64
    provenance: ApprovalArtifactProvenance
    created_at: datetime

    @model_validator(mode="after")
    def coherent_draft(self) -> "CorrectedDraftArtifact":
        _require_aware(self.created_at, "created_at")
        if self.created_at < self.provenance.produced_at:
            raise ValueError("corrected draft predates its provenance")
        if isinstance(self.payload, DepartmentCorrectedDraftPayload):
            if self.scope_key != self.payload.department.value:
                raise ValueError("corrected draft department scope is inconsistent")
            expected_content = department_revision_content_hash(
                content=self.payload.content,
                commercial_terms=self.payload.commercial_terms,
                pending_commercial_items=self.payload.pending_commercial_items,
                evidence=self.payload.evidence,
            )
        else:
            if self.scope_key != "proposal":
                raise ValueError("proposal corrected draft requires proposal scope")
            expected_content = proposal_revision_content_hash(
                currency=self.payload.currency,
                sla=self.payload.sla,
                volume_discounts=self.payload.volume_discounts,
                source_department_revision_ids=(
                    self.payload.source_department_revision_ids
                ),
                authorized_input_ids=self.payload.authorized_input_ids,
            )
        if self.content_hash != expected_content:
            raise ValueError("corrected draft content hash is invalid")
        if self.payload_hash != canonical_hash(self.payload):
            raise ValueError("corrected draft payload hash is invalid")
        return self


class ContentEvaluationArtifactPayload(StrictContract):
    payload_kind: Literal["content_evaluation"] = "content_evaluation"
    mandatory_compliance_passed: bool
    blocks_commercial_completeness: bool
    blocking_rule_ids: list[str] = Field(max_length=60)
    unresolved_input_codes: list[str] = Field(max_length=60)
    open_conflict_ids: list[UUID] = Field(max_length=30)
    eligible: bool

    @model_validator(mode="after")
    def coherent_evaluation(self) -> "ContentEvaluationArtifactPayload":
        _unique(list(self.blocking_rule_ids), "blocking_rule_ids")
        _unique(list(self.unresolved_input_codes), "unresolved_input_codes")
        _unique(list(self.open_conflict_ids), "open_conflict_ids")
        _require_commercial_completeness_coherence(
            blocks_commercial_completeness=(
                self.blocks_commercial_completeness
            ),
            unresolved_input_codes=self.unresolved_input_codes,
        )
        expected = (
            self.mandatory_compliance_passed
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=self.blocking_rule_ids,
                unresolved_input_codes=self.unresolved_input_codes,
            )
            and not self.open_conflict_ids
        )
        if self.eligible != expected:
            raise ValueError("evaluation eligibility disagrees with blockers")
        return self


class SafePreviewArtifactPayload(StrictContract):
    payload_kind: Literal["safe_preview"] = "safe_preview"
    gate_status: Literal["passed", "blocked", "indeterminate", "technical_error"]
    rendered_content: str | None = Field(default=None, max_length=40_000)
    rendered_content_hash: Hash64 | None = None
    reason_codes: list[str] = Field(max_length=60)

    @model_validator(mode="after")
    def coherent_preview(self) -> "SafePreviewArtifactPayload":
        if self.gate_status == "passed":
            if not self.rendered_content or self.reason_codes:
                raise ValueError("passed preview requires content and no reasons")
            expected = canonical_hash({"rendered_content": self.rendered_content})
            if self.rendered_content_hash != expected:
                raise ValueError("safe preview rendered-content hash is invalid")
        elif (
            self.rendered_content is not None
            or self.rendered_content_hash is not None
            or not self.reason_codes
        ):
            raise ValueError("unsafe preview exposes no content and requires reasons")
        return self


ValidationArtifactPayload = Annotated[
    Union[ContentEvaluationArtifactPayload, SafePreviewArtifactPayload],
    Field(discriminator="payload_kind"),
]


class ApprovalValidationArtifact(StrictContract):
    contract_version: Literal[
        "rfp-approval-validation-artifact-v1"
    ] = "rfp-approval-validation-artifact-v1"
    artifact_id: UUID
    artifact_type: Literal["evaluation", "global_evaluation", "safe_preview"]
    approval_run_id: UUID
    ticket_id: UUID
    scope_key: ApprovalArtifactScope
    corrected_draft_id: UUID
    evaluated_content_hash: Hash64
    payload: ValidationArtifactPayload
    payload_hash: Hash64
    provenance: ApprovalArtifactProvenance
    created_at: datetime

    @model_validator(mode="after")
    def coherent_artifact(self) -> "ApprovalValidationArtifact":
        _require_aware(self.created_at, "created_at")
        if self.created_at < self.provenance.produced_at:
            raise ValueError("validation artifact predates its provenance")
        if self.artifact_type == "safe_preview":
            if not isinstance(self.payload, SafePreviewArtifactPayload):
                raise ValueError("safe-preview artifact has an evaluation payload")
        elif not isinstance(self.payload, ContentEvaluationArtifactPayload):
            raise ValueError("evaluation artifact has a preview payload")
        if (self.scope_key == "proposal") != (
            self.artifact_type == "global_evaluation"
        ) and self.artifact_type != "safe_preview":
            raise ValueError("evaluation artifact type and scope disagree")
        if self.payload_hash != canonical_hash(self.payload):
            raise ValueError("validation artifact payload hash is invalid")
        return self


class ApprovalValidationBundle(StrictContract):
    contract_version: Literal[
        "rfp-approval-validation-bundle-v1"
    ] = "rfp-approval-validation-bundle-v1"
    validation_bundle_id: UUID
    approval_run_id: UUID
    ticket_id: UUID
    scope_key: ApprovalArtifactScope
    corrected_draft_id: UUID
    content_hash: Hash64
    evaluation_artifact_id: UUID
    evaluation_artifact_type: Literal["evaluation", "global_evaluation"]
    evaluation_contract_version: str = Field(min_length=1, max_length=100)
    evaluation_payload_hash: Hash64
    safe_preview_artifact_id: UUID
    safe_preview_contract_version: str = Field(min_length=1, max_length=100)
    safe_preview_payload_hash: Hash64
    revision_validation_hash: Hash64
    mandatory_compliance_passed: bool
    blocks_commercial_completeness: bool
    blocking_rule_ids: list[str] = Field(max_length=60)
    unresolved_input_codes: list[str] = Field(max_length=60)
    open_conflict_ids: list[UUID] = Field(max_length=30)
    source_departments_approved: bool
    eligible: bool
    provenance: ApprovalArtifactProvenance
    validated_at: datetime
    bundle_hash: Hash64

    @model_validator(mode="after")
    def coherent_bundle(self) -> "ApprovalValidationBundle":
        _require_aware(self.validated_at, "validated_at")
        if self.validated_at < self.provenance.produced_at:
            raise ValueError("validation bundle predates its provenance")
        _unique(list(self.blocking_rule_ids), "blocking_rule_ids")
        _unique(list(self.unresolved_input_codes), "unresolved_input_codes")
        _unique(list(self.open_conflict_ids), "open_conflict_ids")
        _require_commercial_completeness_coherence(
            blocks_commercial_completeness=(
                self.blocks_commercial_completeness
            ),
            unresolved_input_codes=self.unresolved_input_codes,
        )
        if (self.scope_key == "proposal") != (
            self.evaluation_artifact_type == "global_evaluation"
        ):
            raise ValueError("bundle evaluation type and scope disagree")
        expected_eligible = (
            (self.scope_key != "proposal" or self.source_departments_approved)
            and self.mandatory_compliance_passed
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=self.blocking_rule_ids,
                unresolved_input_codes=self.unresolved_input_codes,
            )
            and not self.open_conflict_ids
        )
        if self.eligible != expected_eligible:
            raise ValueError("bundle eligibility disagrees with blockers")
        evaluation_reference = ImmutableArtifactReference(
            artifact_type=self.evaluation_artifact_type,
            artifact_id=self.evaluation_artifact_id,
            contract_version=self.evaluation_contract_version,
            payload_hash=self.evaluation_payload_hash,
            evaluated_content_hash=self.content_hash,
        )
        preview_reference = ImmutableArtifactReference(
            artifact_type="safe_preview",
            artifact_id=self.safe_preview_artifact_id,
            contract_version=self.safe_preview_contract_version,
            payload_hash=self.safe_preview_payload_hash,
            evaluated_content_hash=self.content_hash,
        )
        if self.scope_key == "proposal":
            revision_validation = {
                "revision_content_hash": self.content_hash,
                "global_evaluation": evaluation_reference,
                "safe_preview": preview_reference,
                "mandatory_compliance_passed": self.mandatory_compliance_passed,
                "unresolved_input_codes": self.unresolved_input_codes,
                "blocking_rule_ids": self.blocking_rule_ids,
                "open_conflict_ids": self.open_conflict_ids,
                "source_departments_approved": self.source_departments_approved,
                "eligible_for_synthesis": self.eligible,
                "validated_at": self.validated_at,
            }
        else:
            revision_validation = {
                "revision_content_hash": self.content_hash,
                "evaluation": evaluation_reference,
                "safe_preview": preview_reference,
                "validated_at": self.validated_at,
            }
        if self.revision_validation_hash != canonical_hash(revision_validation):
            raise ValueError("bundle revision-validation hash is invalid")
        expected_hash = canonical_hash(
            {
                "approval_run_id": self.approval_run_id,
                "ticket_id": self.ticket_id,
                "scope_key": self.scope_key,
                "corrected_draft_id": self.corrected_draft_id,
                "content_hash": self.content_hash,
                "evaluation_artifact_id": self.evaluation_artifact_id,
                "evaluation_artifact_type": self.evaluation_artifact_type,
                "evaluation_contract_version": self.evaluation_contract_version,
                "evaluation_payload_hash": self.evaluation_payload_hash,
                "safe_preview_artifact_id": self.safe_preview_artifact_id,
                "safe_preview_contract_version": self.safe_preview_contract_version,
                "safe_preview_payload_hash": self.safe_preview_payload_hash,
                "revision_validation_hash": self.revision_validation_hash,
                "mandatory_compliance_passed": self.mandatory_compliance_passed,
                "blocks_commercial_completeness": self.blocks_commercial_completeness,
                "blocking_rule_ids": self.blocking_rule_ids,
                "unresolved_input_codes": self.unresolved_input_codes,
                "open_conflict_ids": self.open_conflict_ids,
                "source_departments_approved": self.source_departments_approved,
                "eligible": self.eligible,
                "provenance": self.provenance,
                "validated_at": self.validated_at,
            }
        )
        if self.bundle_hash != expected_hash:
            raise ValueError("validation bundle hash is invalid")
        return self


class InitialProposalRevisionCommand(StrictContract):
    contract_version: Literal[
        "rfp-initial-proposal-revision-command-v1"
    ] = "rfp-initial-proposal-revision-command-v1"
    creation_mode: Literal["part2_import", "authorized_commercial_change"]
    response_run_id: UUID
    response_handoff_hash: Hash64
    proposal: ProposalRevision
    validation_bundle_id: UUID
    imported_by: RunInitiator | None = None
    created_by: AuthorizedHumanActor | None = None
    idempotency_key: UUID
    requested_at: datetime

    @model_validator(mode="after")
    def coherent_initial_creation(self) -> "InitialProposalRevisionCommand":
        _require_aware(self.requested_at, "requested_at")
        if self.proposal.revision_no != 1 or self.proposal.supersedes_revision_id:
            raise ValueError("initial proposal must be revision one without predecessor")
        if self.proposal.validation_bundle_id != self.validation_bundle_id:
            raise ValueError("initial proposal and validation bundle disagree")
        if (
            self.proposal.validation.contract_version
            != "rfp-proposal-revision-validation-v2"
        ):
            raise ValueError("initial proposal requires V2 global validation")
        if self.creation_mode == "part2_import":
            if (
                self.created_by is not None
                or self.imported_by is None
                or self.imported_by.permission != "manage_approval_run"
                or self.proposal.authorized_input_ids
            ):
                raise ValueError("Part 2 proposal import cannot claim human changes")
        else:
            valid_actor = (
                self.imported_by is None
                and self.created_by is not None
                and self.created_by.official_responsible == "Miguel Torres"
                and self.created_by.permission == "provide_business_input"
                and self.created_by.department_scope is None
                and bool(self.proposal.authorized_input_ids)
            )
            if not valid_actor:
                raise ValueError("commercial proposal creation requires Miguel and inputs")
        return self


class HumanDecision(StrictContract):
    contract_version: Literal["rfp-human-decision-v1"] = "rfp-human-decision-v1"
    decision_id: UUID
    idempotency_key: UUID
    ticket_id: UUID
    approval_run_id: UUID
    department: DepartmentId
    revision_id: UUID
    draft_id: UUID
    expected_content_hash: Hash64
    interrupt_id: str = Field(min_length=1, max_length=300)
    action: ReviewAction
    reason: str | None = Field(default=None, max_length=2_000)
    actor: AuthorizedHumanActor
    decided_at: datetime

    @model_validator(mode="after")
    def coherent_decision(self) -> "HumanDecision":
        _require_aware(self.decided_at, "decided_at")
        if self.actor.permission != "approve_department":
            raise ValueError("human decision requires department-approval permission")
        if self.actor.department_scope != self.department:
            raise ValueError("decision actor and department disagree")
        if self.action in {"reject", "request_changes"} and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("reject and request_changes require a reason")
        return self


def review_audit_event_key(
    approval_run_id: UUID, revision_id: UUID, content_hash: str
) -> str:
    digest = canonical_hash(
        {
            "approval_run_id": approval_run_id,
            "revision_id": revision_id,
            "content_hash": content_hash,
        }
    )
    return f"review-ready:{digest}"


class ReviewPreparationReceipt(StrictContract):
    preparation_id: UUID
    approval_run_id: UUID
    revision_id: UUID
    draft_id: UUID
    content_hash: Hash64
    validation_hash: Hash64
    audit_event_key: str = Field(pattern=r"^review-ready:[a-f0-9]{64}$")
    prepared_at: datetime

    @model_validator(mode="after")
    def coherent_receipt(self) -> "ReviewPreparationReceipt":
        _require_aware(self.prepared_at, "prepared_at")
        expected = review_audit_event_key(
            self.approval_run_id, self.revision_id, self.content_hash
        )
        if self.audit_event_key != expected:
            raise ValueError("review audit event key is invalid")
        return self


def interrupt_payload_hash(
    *,
    preparation_id: UUID,
    revision_id: UUID,
    draft_id: UUID,
    expected_content_hash: str,
    allowed_actions: list[ReviewAction],
    safe_preview_ref: ImmutableArtifactReference,
) -> str:
    return canonical_hash(
        {
            "preparation_id": preparation_id,
            "revision_id": revision_id,
            "draft_id": draft_id,
            "expected_content_hash": expected_content_hash,
            "allowed_actions": allowed_actions,
            "safe_preview_ref": safe_preview_ref,
        }
    )


class ReviewInterruptPayload(StrictContract):
    preparation_id: UUID
    revision_id: UUID
    draft_id: UUID
    expected_content_hash: Hash64
    allowed_actions: list[ReviewAction] = Field(min_length=1, max_length=3)
    safe_preview_ref: ImmutableArtifactReference
    interrupt_payload_hash: Hash64

    @model_validator(mode="after")
    def coherent_payload(self) -> "ReviewInterruptPayload":
        _unique(list(self.allowed_actions), "allowed_actions")
        if self.safe_preview_ref.artifact_type != "safe_preview":
            raise ValueError("interrupt payload requires a safe preview")
        if self.safe_preview_ref.evaluated_content_hash != self.expected_content_hash:
            raise ValueError("interrupt preview targets different content")
        expected = interrupt_payload_hash(
            preparation_id=self.preparation_id,
            revision_id=self.revision_id,
            draft_id=self.draft_id,
            expected_content_hash=self.expected_content_hash,
            allowed_actions=self.allowed_actions,
            safe_preview_ref=self.safe_preview_ref,
        )
        if self.interrupt_payload_hash != expected:
            raise ValueError("interrupt payload hash is invalid")
        return self


def decision_application_key(decision: HumanDecision, preparation_id: UUID) -> str:
    return f"decision-apply:{canonical_hash({'decision': decision, 'preparation_id': preparation_id})}"


class DecisionApplicationCommand(StrictContract):
    decision: HumanDecision
    preparation_id: UUID
    expected_revision_state: Literal["pending_approval"] = "pending_approval"
    application_key: str = Field(pattern=r"^decision-apply:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent_command(self) -> "DecisionApplicationCommand":
        if self.application_key != decision_application_key(
            self.decision, self.preparation_id
        ):
            raise ValueError("decision application key is invalid")
        return self


CommercialInputEvidenceType = Literal[
    "responsible_attestation",
    "approved_quote",
    "approved_policy",
    "authorized_conversion",
]


class BusinessInputProvenance(StrictContract):
    evidence_type: CommercialInputEvidenceType
    evidence_id: UUID
    source_name: str = Field(min_length=1, max_length=300)
    issued_at: datetime
    valid_until: date | None = None
    source_payload_hash: Hash64
    notes: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def coherent_provenance(self) -> "BusinessInputProvenance":
        _require_aware(self.issued_at, "issued_at")
        if self.valid_until is not None and self.valid_until < self.issued_at.date():
            raise ValueError("commercial evidence cannot expire before issuance")
        return self


class QuantityScope(StrictContract):
    location_code: str | None = Field(default=None, min_length=1, max_length=100)
    service_code: str | None = Field(default=None, min_length=1, max_length=100)
    scope_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def identifiable_scope(self) -> "QuantityScope":
        if not any((self.location_code, self.service_code, self.scope_code)):
            raise ValueError("quantity scope requires location, service, or scope code")
        return self


class MonthlyStorageVolumeInput(StrictContract):
    input_kind: Literal["monthly_storage_volume"] = "monthly_storage_volume"
    value: Decimal = Field(ge=Decimal("0"), max_digits=16, decimal_places=4)
    dimension: Literal["volume"] = "volume"
    unit: Literal["m3"]
    period: Literal["month"]
    operational_scope: QuantityScope
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "MonthlyStorageVolumeInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("volume end cannot precede its start")
        return self


class ClientPriceQuoteInput(StrictContract):
    input_kind: Literal["client_price_quote"] = "client_price_quote"
    department: DepartmentId
    service_code: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=Decimal("0"), max_digits=14, decimal_places=4)
    currency: Currency
    unit: str = Field(min_length=1, max_length=80)
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "ClientPriceQuoteInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("quote end cannot precede its start")
        return self


class WarehouseCapacityInput(StrictContract):
    input_kind: Literal["warehouse_capacity"] = "warehouse_capacity"
    department: Literal[DepartmentId.warehouse] = DepartmentId.warehouse
    site_code: str = Field(min_length=1, max_length=100)
    capacity_value: Decimal = Field(gt=Decimal("0"), max_digits=16, decimal_places=4)
    capacity_unit: str = Field(min_length=1, max_length=80)
    period: str = Field(min_length=1, max_length=80)
    available_from: date
    available_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "WarehouseCapacityInput":
        if self.available_until is not None and self.available_until < self.available_from:
            raise ValueError("capacity end cannot precede its start")
        return self


class OperationalScopeInput(StrictContract):
    input_kind: Literal["operational_scope"] = "operational_scope"
    department: DepartmentId
    scope_code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4_000)
    included: bool
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "OperationalScopeInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("scope end cannot precede its start")
        return self


class CurrencyQuoteInput(StrictContract):
    input_kind: Literal["currency_quote"] = "currency_quote"
    department: DepartmentId | None = None
    term_code: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=Decimal("0"), max_digits=14, decimal_places=4)
    currency: Currency
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "CurrencyQuoteInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("currency quote end cannot precede its start")
        return self


class CurrencyConversionInput(StrictContract):
    input_kind: Literal["currency_conversion"] = "currency_conversion"
    department: DepartmentId | None = None
    source_currency: Currency
    target_currency: Currency
    conversion_rate: Decimal = Field(gt=Decimal("0"), max_digits=18, decimal_places=8)
    effective_at: datetime
    source_term_codes: list[str] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def coherent_conversion(self) -> "CurrencyConversionInput":
        _require_aware(self.effective_at, "effective_at")
        if self.source_currency == self.target_currency:
            raise ValueError("conversion currencies must differ")
        _unique(list(self.source_term_codes), "source_term_codes")
        return self


class GlobalCommercialTermInput(StrictContract):
    input_kind: Literal["global_commercial_term"] = "global_commercial_term"
    term_code: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2_000)
    currency: Currency | None = None
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_dates(self) -> "GlobalCommercialTermInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("term end cannot precede its start")
        return self


class VolumeDiscountTierInput(StrictContract):
    minimum_volume: Decimal = Field(ge=Decimal("0"), max_digits=16, decimal_places=4)
    maximum_volume: Decimal | None = Field(
        default=None, gt=Decimal("0"), max_digits=16, decimal_places=4
    )
    discount_percentage: Decimal = Field(
        ge=Decimal("0"), le=Decimal("100"), max_digits=7, decimal_places=4
    )

    @model_validator(mode="after")
    def coherent_tier(self) -> "VolumeDiscountTierInput":
        if self.maximum_volume is not None and self.maximum_volume <= self.minimum_volume:
            raise ValueError("discount maximum must exceed its minimum")
        return self


class VolumeDiscountInput(StrictContract):
    input_kind: Literal["volume_discount"] = "volume_discount"
    volume_unit: str = Field(min_length=1, max_length=80)
    period: str = Field(min_length=1, max_length=80)
    tiers: list[VolumeDiscountTierInput] = Field(min_length=1, max_length=30)
    effective_from: date
    effective_until: date | None = None

    @model_validator(mode="after")
    def coherent_tiers(self) -> "VolumeDiscountInput":
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("discount end cannot precede its start")
        previous_max: Decimal | None = None
        for index, tier in enumerate(self.tiers):
            if tier.maximum_volume is None and index != len(self.tiers) - 1:
                raise ValueError("an open discount tier must be the final tier")
            if previous_max is not None and tier.minimum_volume < previous_max:
                raise ValueError("discount tiers overlap or are out of order")
            previous_max = tier.maximum_volume
        return self


AuthorizedInputPayload = Annotated[
    Union[
        ClientPriceQuoteInput,
        WarehouseCapacityInput,
        OperationalScopeInput,
        CurrencyQuoteInput,
        CurrencyConversionInput,
        GlobalCommercialTermInput,
        VolumeDiscountInput,
        MonthlyStorageVolumeInput,
    ],
    Field(discriminator="input_kind"),
]


def authorized_business_input_hash(
    *,
    input_kind: str,
    payload: AuthorizedInputPayload,
    provenance: BusinessInputProvenance,
    actor: AuthorizedHumanActor,
) -> str:
    return canonical_hash(
        {
            "input_kind": input_kind,
            "payload": payload,
            "provenance": provenance,
            "actor": actor,
        }
    )


class AuthorizedBusinessInput(StrictContract):
    contract_version: Literal[
        "rfp-authorized-business-input-v1"
    ] = "rfp-authorized-business-input-v1"
    input_id: UUID
    ticket_id: UUID
    approval_run_id: UUID
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
    actor: AuthorizedHumanActor
    payload_hash: Hash64
    recorded_at: datetime

    @model_validator(mode="after")
    def coherent_input(self) -> "AuthorizedBusinessInput":
        _require_aware(self.recorded_at, "recorded_at")
        if self.input_kind != self.payload.input_kind:
            raise ValueError("envelope and payload input kinds disagree")
        if self.actor.permission != "provide_business_input":
            raise ValueError("actor lacks business-input permission")
        payload_department = getattr(self.payload, "department", None)
        if self.actor.official_responsible == "Miguel Torres":
            if payload_department is not None:
                raise ValueError("proposal-wide actor cannot own departmental input")
        elif payload_department != self.actor.department_scope:
            raise ValueError("input department and actor scope disagree")
        monetary_kinds = {
            "client_price_quote",
            "currency_quote",
            "global_commercial_term",
            "volume_discount",
        }
        if self.input_kind in monetary_kinds and self.provenance.evidence_type not in {
            "approved_quote",
            "approved_policy",
        }:
            raise ValueError("monetary input requires an approved quote or policy")
        if self.input_kind == "currency_conversion" and (
            self.provenance.evidence_type != "authorized_conversion"
        ):
            raise ValueError("currency conversion requires conversion evidence")
        if self.input_kind == "warehouse_capacity" and (
            self.actor.official_responsible != "Ana Whitfield"
        ):
            raise ValueError("warehouse capacity must be authorized by Ana")
        expected = authorized_business_input_hash(
            input_kind=self.input_kind,
            payload=self.payload,
            provenance=self.provenance,
            actor=self.actor,
        )
        if self.payload_hash != expected:
            raise ValueError("authorized business input hash is invalid")
        return self

    def evidence_reference(self) -> EvidenceReference:
        return EvidenceReference(
            document_id="authorized-business-input-v1",
            section_id=str(self.input_id),
            evidence_hash=self.payload_hash,
        )


class QuantityFact(StrictContract):
    value: Decimal = Field(ge=Decimal("0"), max_digits=16, decimal_places=4)
    dimension: Literal["volume", "mass", "item_count", "pallet_count"]
    unit: str = Field(min_length=1, max_length=80)
    period: str = Field(min_length=1, max_length=80)
    operational_scope: QuantityScope


class VolumeCapacityFacts(StrictContract):
    trigger: Literal["volume-vs-capacity"] = "volume-vs-capacity"
    applicable: bool = True
    required_volume: QuantityFact | None = None
    required_volume_input_id: UUID | None = None
    available_capacity: QuantityFact | None = None
    capacity_input_id: UUID | None = None
    normalization_input_id: UUID | None = None
    normalized_required_volume: QuantityFact | None = None
    normalized_available_capacity: QuantityFact | None = None
    comparison_status: Literal[
        "not_applicable",
        "comparable",
        "missing_required_volume",
        "missing_capacity",
        "incompatible_units",
    ]
    exceeds_capacity: bool | None

    @model_validator(mode="after")
    def derive_comparison(self) -> "VolumeCapacityFacts":
        if not self.applicable:
            if any(
                value is not None
                for value in (
                    self.required_volume,
                    self.available_capacity,
                    self.capacity_input_id,
                    self.normalization_input_id,
                    self.normalized_required_volume,
                    self.normalized_available_capacity,
                )
            ):
                raise ValueError("non-applicable volume trigger cannot claim facts")
            expected_status = "not_applicable"
            expected_exceeds = None
        elif self.required_volume is None:
            if any(
                value is not None
                for value in (
                    self.available_capacity,
                    self.capacity_input_id,
                    self.normalization_input_id,
                    self.normalized_required_volume,
                    self.normalized_available_capacity,
                )
            ):
                raise ValueError("capacity cannot be compared without required volume")
            expected_status = "missing_required_volume"
            expected_exceeds = None
        elif self.available_capacity is None:
            if any(
                value is not None
                for value in (
                    self.capacity_input_id,
                    self.normalization_input_id,
                    self.normalized_required_volume,
                    self.normalized_available_capacity,
                )
            ):
                raise ValueError("capacity or normalization reference without capacity fact")
            expected_status = "missing_capacity"
            expected_exceeds = None
        elif self.capacity_input_id is None:
            raise ValueError("capacity fact requires its authorized input reference")
        else:
            same_context = (
                self.required_volume.dimension == self.available_capacity.dimension
                and self.required_volume.operational_scope
                == self.available_capacity.operational_scope
            )
            directly_comparable = (
                same_context
                and self.required_volume.unit == self.available_capacity.unit
                and self.required_volume.period == self.available_capacity.period
            )
            has_complete_normalization = all(
                value is not None
                for value in (
                    self.normalization_input_id,
                    self.normalized_required_volume,
                    self.normalized_available_capacity,
                )
            )
            has_any_normalization = any(
                value is not None
                for value in (
                    self.normalization_input_id,
                    self.normalized_required_volume,
                    self.normalized_available_capacity,
                )
            )
            if directly_comparable:
                if has_any_normalization:
                    raise ValueError("already comparable facts cannot claim normalization")
                expected_status = "comparable"
                expected_exceeds = (
                    self.required_volume.value > self.available_capacity.value
                )
            elif has_complete_normalization:
                if not same_context:
                    raise ValueError(
                        "normalization cannot merge different dimensions or operational scopes"
                    )
                normalized_required = self.normalized_required_volume
                normalized_capacity = self.normalized_available_capacity
                if normalized_required is None or normalized_capacity is None:
                    raise ValueError("normalization facts are incomplete")
                normalized_context_matches = (
                    normalized_required.dimension == self.required_volume.dimension
                    and normalized_capacity.dimension == self.required_volume.dimension
                    and normalized_required.operational_scope
                    == self.required_volume.operational_scope
                    and normalized_capacity.operational_scope
                    == self.required_volume.operational_scope
                )
                if not normalized_context_matches:
                    raise ValueError("normalized facts changed dimension or operational scope")
                if (
                    normalized_required.unit != normalized_capacity.unit
                    or normalized_required.period != normalized_capacity.period
                ):
                    raise ValueError("normalized volume and capacity remain incomparable")
                expected_status = "comparable"
                expected_exceeds = normalized_required.value > normalized_capacity.value
            else:
                if has_any_normalization:
                    raise ValueError(
                        "normalization requires its input and both normalized facts"
                    )
                expected_status = "incompatible_units"
                expected_exceeds = None
        if self.comparison_status != expected_status:
            raise ValueError("volume/capacity comparison status is not derived from facts")
        if self.exceeds_capacity != expected_exceeds:
            raise ValueError("volume/capacity conflict flag is not derived from facts")
        return self


class ReturnSLAViolation(StrictContract):
    department_revision_id: UUID
    sla_hours: Decimal = Field(gt=Decimal("0"), max_digits=8, decimal_places=2)


class ReturnsSLAFacts(StrictContract):
    trigger: Literal["returns-sla-breach"] = "returns-sla-breach"
    minimum_supported_hours: Literal[48] = 48
    observed: list[ReturnSLAViolation] = Field(max_length=30)
    violating_revision_ids: list[UUID] = Field(max_length=30)
    conflict_demonstrated: bool

    @model_validator(mode="after")
    def derive_violations(self) -> "ReturnsSLAFacts":
        _unique([item.department_revision_id for item in self.observed], "observed revisions")
        expected = [
            item.department_revision_id
            for item in self.observed
            if item.sla_hours < Decimal("48")
        ]
        if self.violating_revision_ids != expected:
            raise ValueError("return SLA violations are not derived from observed facts")
        if self.conflict_demonstrated != bool(expected):
            raise ValueError("return SLA conflict flag is not derived from facts")
        return self


class MonetaryTermFact(StrictContract):
    term_code: str = Field(min_length=1, max_length=100)
    department_revision_id: UUID
    currency: Currency
    amount: Decimal | None = Field(
        default=None, gt=Decimal("0"), max_digits=14, decimal_places=4
    )


class CurrencyMismatchFacts(StrictContract):
    trigger: Literal["currency-mismatch"] = "currency-mismatch"
    client_country: Literal["US", "ES"]
    expected_currency: Currency
    proposal_revision_id: UUID
    proposal_currency: Currency
    monetary_terms: list[MonetaryTermFact] = Field(max_length=60)
    mismatched_revision_ids: list[UUID] = Field(max_length=60)
    conflict_demonstrated: bool

    @model_validator(mode="after")
    def derive_mismatch(self) -> "CurrencyMismatchFacts":
        expected_currency = "USD" if self.client_country == "US" else "EUR"
        if self.expected_currency != expected_currency:
            raise ValueError("expected currency is not derived from client country")
        offenders: list[UUID] = []
        if self.proposal_currency != expected_currency:
            offenders.append(self.proposal_revision_id)
        offenders.extend(
            term.department_revision_id
            for term in self.monetary_terms
            if term.currency != expected_currency
        )
        offenders = list(dict.fromkeys(offenders))
        if self.mismatched_revision_ids != offenders:
            raise ValueError("currency offenders are not derived from monetary facts")
        if self.conflict_demonstrated != bool(offenders):
            raise ValueError("currency conflict flag is not derived from facts")
        return self


ArbitrationFacts = Annotated[
    Union[VolumeCapacityFacts, ReturnsSLAFacts, CurrencyMismatchFacts],
    Field(discriminator="trigger"),
]


class ArbitrationTarget(StrictContract):
    department: DepartmentId | None = None
    department_revision_id: UUID | None = None
    proposal_revision_id: UUID | None = None

    @model_validator(mode="after")
    def one_target_kind(self) -> "ArbitrationTarget":
        department_target = self.department is not None or self.department_revision_id is not None
        if department_target and (
            self.department is None or self.department_revision_id is None
        ):
            raise ValueError("department target requires department and revision")
        if department_target == (self.proposal_revision_id is not None):
            raise ValueError("arbitration target must select exactly one revision kind")
        return self


ConflictOutcome = Literal[
    "demonstrated", "insufficient_information", "no_conflict"
]


def _conflict_outcome(facts: ArbitrationFacts) -> ConflictOutcome:
    if isinstance(facts, VolumeCapacityFacts):
        if facts.comparison_status == "not_applicable":
            return "no_conflict"
        if facts.exceeds_capacity is True:
            return "demonstrated"
        if facts.comparison_status != "comparable":
            return "insufficient_information"
        return "no_conflict"
    if facts.conflict_demonstrated:
        return "demonstrated"
    return "no_conflict"


def conflict_assessment_hash(
    *, trigger: TriggerId, facts: ArbitrationFacts, outcome: ConflictOutcome,
    missing_input_codes: list[str], assessed_content_hashes: dict[str, Hash64] | None = None,
) -> str:
    value = {
        "trigger": trigger,
        "facts": facts,
        "outcome": outcome,
        "missing_input_codes": missing_input_codes,
    }
    if assessed_content_hashes:
        value["assessed_content_hashes"] = assessed_content_hashes
    return canonical_hash(value)


class ConflictAssessment(StrictContract):
    """Deterministic result before deciding whether a conflict record is needed."""

    contract_version: Literal[
        "rfp-conflict-assessment-v1"
    ] = "rfp-conflict-assessment-v1"
    assessment_id: UUID
    trigger: TriggerId
    facts: ArbitrationFacts
    outcome: ConflictOutcome
    missing_input_codes: list[str] = Field(max_length=30)
    assessed_content_hashes: dict[str, Hash64] = Field(default_factory=dict, max_length=4)
    assessment_hash: Hash64
    assessed_at: datetime

    @model_validator(mode="after")
    def coherent_assessment(self) -> "ConflictAssessment":
        _require_aware(self.assessed_at, "assessed_at")
        if self.trigger != self.facts.trigger:
            raise ValueError("assessment envelope and facts trigger disagree")
        expected_outcome = _conflict_outcome(self.facts)
        if self.outcome != expected_outcome:
            raise ValueError("conflict outcome is not derived from its facts")
        if self.outcome == "insufficient_information":
            if not self.missing_input_codes:
                raise ValueError("insufficient information must name missing inputs")
        elif self.missing_input_codes:
            raise ValueError("complete facts cannot claim missing inputs")
        _unique(list(self.missing_input_codes), "missing_input_codes")
        expected_hash = conflict_assessment_hash(
            trigger=self.trigger,
            facts=self.facts,
            outcome=self.outcome,
            missing_input_codes=self.missing_input_codes,
            assessed_content_hashes=self.assessed_content_hashes,
        )
        if self.assessment_hash != expected_hash:
            raise ValueError("conflict assessment hash is invalid")
        return self


def arbitration_conflict_fingerprint(
    *, trigger: TriggerId, facts: ArbitrationFacts, affected_targets: list[ArbitrationTarget]
) -> str:
    return canonical_hash(
        {"trigger": trigger, "facts": facts, "affected_targets": affected_targets}
    )


class ArbitrationConflict(StrictContract):
    contract_version: Literal[
        "rfp-arbitration-conflict-v1"
    ] = "rfp-arbitration-conflict-v1"
    conflict_id: UUID
    approval_run_id: UUID
    trigger: TriggerId
    facts: ArbitrationFacts
    affected_targets: list[ArbitrationTarget] = Field(min_length=1, max_length=30)
    status: Literal["demonstrated", "insufficient_information"]
    missing_input_codes: list[str] = Field(max_length=30)
    fingerprint: Hash64
    detected_at: datetime

    @model_validator(mode="after")
    def coherent_conflict(self) -> "ArbitrationConflict":
        _require_aware(self.detected_at, "detected_at")
        if self.trigger != self.facts.trigger:
            raise ValueError("conflict envelope and facts trigger disagree")
        expected_status = _conflict_outcome(self.facts)
        if expected_status == "no_conflict":
            raise ValueError("sufficient facts without contradiction are not a conflict")
        if self.status != expected_status:
            raise ValueError("conflict status is not derived from its facts")
        if self.status == "insufficient_information" and not self.missing_input_codes:
            raise ValueError("insufficient information must name missing inputs")
        if self.status == "demonstrated" and self.missing_input_codes:
            raise ValueError("demonstrated conflict cannot claim missing inputs")
        _unique(list(self.missing_input_codes), "missing_input_codes")
        expected = arbitration_conflict_fingerprint(
            trigger=self.trigger,
            facts=self.facts,
            affected_targets=self.affected_targets,
        )
        if self.fingerprint != expected:
            raise ValueError("arbitration conflict fingerprint is invalid")
        return self


def deterministic_execution_hash(
    *, conflict_id: UUID, rule_id: str, outcome: str, resulting_status: str
) -> str:
    return canonical_hash(
        {
            "conflict_id": conflict_id,
            "rule_id": rule_id,
            "outcome": outcome,
            "resulting_status": resulting_status,
        }
    )


class DeterministicRuleExecution(StrictContract):
    execution_kind: Literal["deterministic_rule"] = "deterministic_rule"
    execution_id: UUID
    conflict_id: UUID
    rule_id: str = Field(min_length=1, max_length=150)
    outcome: Literal["no_conflict", "request_input", "route_to_arbitration"]
    resulting_status: Literal[
        "resolved", "blocked_input", "pending_arbitration"
    ]
    execution_hash: Hash64
    executed_at: datetime

    @model_validator(mode="after")
    def coherent_execution(self) -> "DeterministicRuleExecution":
        _require_aware(self.executed_at, "executed_at")
        expected = deterministic_execution_hash(
            conflict_id=self.conflict_id,
            rule_id=self.rule_id,
            outcome=self.outcome,
            resulting_status=self.resulting_status,
        )
        if self.execution_hash != expected:
            raise ValueError("deterministic execution hash is invalid")
        return self


class HumanArbitrationExecution(StrictContract):
    execution_kind: Literal["human_arbitration"] = "human_arbitration"
    execution_id: UUID
    conflict_id: UUID
    actor: AuthorizedHumanActor
    decided_at: datetime

    @model_validator(mode="after")
    def coherent_actor(self) -> "HumanArbitrationExecution":
        _require_aware(self.decided_at, "decided_at")
        if self.actor.permission != "arbitrate":
            raise ValueError("human arbitration requires arbitration permission")
        return self


ArbitrationExecution = Annotated[
    Union[DeterministicRuleExecution, HumanArbitrationExecution],
    Field(discriminator="execution_kind"),
]


class CorrectedRevisionEvidence(StrictContract):
    original_target: ArbitrationTarget
    corrected_revision_id: UUID
    corrected_content_hash: Hash64
    revision_validation: ImmutableArtifactReference
    conflict_recheck: ImmutableArtifactReference

    @model_validator(mode="after")
    def tied_to_corrected_content(self) -> "CorrectedRevisionEvidence":
        original_revision_id = (
            self.original_target.department_revision_id
            or self.original_target.proposal_revision_id
        )
        if self.corrected_revision_id == original_revision_id:
            raise ValueError("corrected evidence must reference a new revision")
        if self.revision_validation.artifact_type != "revision_validation":
            raise ValueError("corrected revision requires revision-validation evidence")
        if self.conflict_recheck.artifact_type != "conflict_recheck":
            raise ValueError("corrected revision requires conflict-recheck evidence")
        if {
            self.corrected_content_hash,
            self.revision_validation.evaluated_content_hash,
            self.conflict_recheck.evaluated_content_hash,
        } != {self.corrected_content_hash}:
            raise ValueError("corrected evidence targets stale content")
        return self


def arbitration_resolution_hash(
    *, conflict_id: UUID, conflict_fingerprint: str, action: str,
    targets: list[ArbitrationTarget], authorized_input_ids: list[UUID], reason: str,
    corrected_evidence: list[CorrectedRevisionEvidence] | None = None,
    post_correction_assessment: ConflictAssessment | None = None,
) -> str:
    return canonical_hash(
        {
            "conflict_id": conflict_id,
            "conflict_fingerprint": conflict_fingerprint,
            "action": action,
            "targets": targets,
            "authorized_input_ids": authorized_input_ids,
            "reason": reason,
            "corrected_evidence": corrected_evidence or [],
            "post_correction_assessment": post_correction_assessment,
        }
    )


class ArbitrationResolution(StrictContract):
    contract_version: Literal[
        "rfp-arbitration-resolution-v1"
    ] = "rfp-arbitration-resolution-v1"
    resolution_id: UUID
    conflict_id: UUID
    conflict_fingerprint: Hash64
    trigger: TriggerId
    action: Literal["accept_resolution", "request_changes"]
    targets: list[ArbitrationTarget] = Field(min_length=1, max_length=30)
    authorized_input_ids: list[UUID] = Field(max_length=60)
    reason: str = Field(min_length=1, max_length=2_000)
    execution: HumanArbitrationExecution
    corrected_evidence: list[CorrectedRevisionEvidence] = Field(
        default_factory=list, max_length=30
    )
    post_correction_assessment: ConflictAssessment | None = None
    resolution_hash: Hash64

    @model_validator(mode="after")
    def coherent_resolution(self) -> "ArbitrationResolution":
        if self.execution.conflict_id != self.conflict_id:
            raise ValueError("arbitration execution targets a different conflict")
        if self.execution.actor.trigger_scope != self.trigger:
            raise ValueError("arbitrator trigger scope disagrees")
        _unique(list(self.authorized_input_ids), "authorized_input_ids")
        actor = self.execution.actor
        if actor.official_responsible == "Sofía Ramos":
            if any(target.department != DepartmentId.reverse for target in self.targets):
                raise ValueError("Sofía may resolve only reverse targets")
        if (
            actor.official_responsible == "Miguel Torres"
            and self.trigger == "returns-sla-breach"
            and all(
                target.department == DepartmentId.reverse
                and target.proposal_revision_id is None
                for target in self.targets
            )
        ):
            raise ValueError(
                "a reverse-only return-SLA breach belongs to Sofía, not Miguel"
            )
        if self.action == "request_changes":
            if self.corrected_evidence or self.post_correction_assessment is not None:
                raise ValueError("request_changes cannot claim corrected evidence")
        else:
            if not self.corrected_evidence or self.post_correction_assessment is None:
                raise ValueError(
                    "accept_resolution requires corrected evidence and re-evaluation"
                )
            if self.post_correction_assessment.trigger != self.trigger:
                raise ValueError("post-correction assessment has the wrong trigger")
            if self.post_correction_assessment.outcome != "no_conflict":
                raise ValueError("corrected content must re-evaluate to no conflict")
            evidence_targets = [item.original_target for item in self.corrected_evidence]
            if evidence_targets != self.targets:
                raise ValueError("corrected evidence must cover every target in order")
            if any(
                item.conflict_recheck.payload_hash
                != self.post_correction_assessment.assessment_hash
                for item in self.corrected_evidence
            ):
                raise ValueError("conflict recheck does not reference the final assessment")
            if self.trigger == "returns-sla-breach":
                corrected_facts = self.post_correction_assessment.facts
                if not isinstance(corrected_facts, ReturnsSLAFacts):
                    raise ValueError("return-SLA resolution requires return-SLA facts")
                observed_ids = {
                    item.department_revision_id for item in corrected_facts.observed
                }
                corrected_ids = {
                    item.corrected_revision_id for item in self.corrected_evidence
                }
                if observed_ids != corrected_ids:
                    raise ValueError(
                        "return-SLA recheck must observe the corrected revisions"
                    )
        expected = arbitration_resolution_hash(
            conflict_id=self.conflict_id,
            conflict_fingerprint=self.conflict_fingerprint,
            action=self.action,
            targets=self.targets,
            authorized_input_ids=self.authorized_input_ids,
            reason=self.reason,
            corrected_evidence=self.corrected_evidence,
            post_correction_assessment=self.post_correction_assessment,
        )
        if self.resolution_hash != expected:
            raise ValueError("arbitration resolution hash is invalid")
        return self


class FinalDepartmentSection(StrictContract):
    department: DepartmentId
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=30_000)
    approved_revision_id: UUID
    approved_content_hash: Hash64
    approval_decision_id: UUID


class FinalGlobalTerms(StrictContract):
    currency: GlobalCurrency
    sla: GlobalSLA
    volume_discounts: list[VolumeDiscountRow] = Field(max_length=30)
    unresolved_input_codes: list[
        Literal[
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
        ]
    ] = Field(
        default_factory=list,
        max_length=2,
        exclude_if=lambda value: not value,
    )
    proposal_revision_id: UUID
    proposal_content_hash: Hash64

    @model_validator(mode="after")
    def complete_currency(self) -> "FinalGlobalTerms":
        if (
            self.currency.status != "documented"
            or self.currency.value is None
            or not self.currency.evidence
        ):
            raise ValueError("final document requires documented global currency")
        if self.unresolved_input_codes != sorted(set(self.unresolved_input_codes)):
            raise ValueError(
                "final global unresolved input codes must be unique and ordered"
            )
        return self


class FinalDocumentManifest(StrictContract):
    ticket_id: UUID
    approval_run_id: UUID
    response_run_id: UUID
    department_revision_ids: list[UUID] = Field(min_length=1, max_length=3)
    approval_decision_ids: list[UUID] = Field(min_length=1, max_length=3)
    proposal_revision_id: UUID
    arbitration_resolution_ids: list[UUID] = Field(max_length=30)
    authorized_input_ids: list[UUID] = Field(max_length=60)


class FinalDocumentPayload(StrictContract):
    contract_version: Literal[
        "rfp-final-document-payload-v1"
    ] = "rfp-final-document-payload-v1"
    title: str = Field(min_length=1, max_length=300)
    executive_summary_markdown: str = Field(min_length=1, max_length=10_000)
    sections: list[FinalDepartmentSection] = Field(min_length=1, max_length=3)
    global_terms: FinalGlobalTerms
    manifest: FinalDocumentManifest

    @model_validator(mode="after")
    def coherent_manifest(self) -> "FinalDocumentPayload":
        departments = [section.department for section in self.sections]
        _unique(list(departments), "final departments")
        section_revisions = [section.approved_revision_id for section in self.sections]
        decision_ids = [section.approval_decision_id for section in self.sections]
        if self.manifest.department_revision_ids != section_revisions:
            raise ValueError("manifest department revisions disagree with sections")
        if self.manifest.approval_decision_ids != decision_ids:
            raise ValueError("manifest decisions disagree with sections")
        if self.manifest.proposal_revision_id != self.global_terms.proposal_revision_id:
            raise ValueError("manifest proposal revision disagrees with global terms")
        return self


class PersistedFinalDocument(StrictContract):
    """Persisted payload plus rendered bytes.

    ``markdown_hash`` proves byte integrity only. Deterministic rendering and
    cross-checking Markdown against ``payload`` are deliberately deferred to
    the rendering gate.
    """
    contract_version: Literal[
        "rfp-persisted-final-document-v1"
    ] = "rfp-persisted-final-document-v1"
    final_document_id: UUID
    payload: FinalDocumentPayload
    payload_hash: Hash64
    markdown: str = Field(min_length=1, max_length=120_000)
    markdown_hash: Hash64
    storage_reference: str = Field(min_length=1, max_length=1_000)
    generated_at: datetime

    @model_validator(mode="after")
    def coherent_document(self) -> "PersistedFinalDocument":
        _require_aware(self.generated_at, "generated_at")
        if self.payload_hash != canonical_hash(self.payload):
            raise ValueError("final payload hash is invalid")
        if self.markdown_hash != _markdown_hash(self.markdown):
            raise ValueError("final Markdown hash is invalid")
        return self


class PublicDepartmentSection(StrictContract):
    department: DepartmentId
    title: str
    markdown: str


class PublicCurrency(StrictContract):
    value: Literal["EUR", "USD"] | None
    status: Literal["documented", "pending"]


class PublicSLA(StrictContract):
    applicability: Literal["applicable", "not_applicable", "pending"]
    percentage: float | None
    measurement_period: str | None
    scope: list[str]
    exclusions: list[str]


class PublicVolumeDiscount(StrictContract):
    volume_from: float | None
    volume_to: float | None
    discount_percentage: float | None
    condition: str
    status: Literal["documented", "pending"]


class PublicFinalDocument(StrictContract):
    contract_version: Literal[
        "rfp-public-final-document-v1"
    ] = "rfp-public-final-document-v1"
    final_document_id: UUID
    title: str
    executive_summary_markdown: str
    sections: list[PublicDepartmentSection]
    currency: PublicCurrency
    sla: PublicSLA
    volume_discounts: list[PublicVolumeDiscount]
    markdown: str
    markdown_hash: Hash64
    download_filename: str = Field(pattern=r"^[A-Za-z0-9._-]+\.md$")


def public_final_document(
    document: PersistedFinalDocument, *, download_filename: str
) -> PublicFinalDocument:
    return PublicFinalDocument(
        final_document_id=document.final_document_id,
        title=document.payload.title,
        executive_summary_markdown=document.payload.executive_summary_markdown,
        sections=[
            PublicDepartmentSection(
                department=section.department,
                title=section.title,
                markdown=section.markdown,
            )
            for section in document.payload.sections
        ],
        currency=PublicCurrency(
            value=document.payload.global_terms.currency.value,
            status=document.payload.global_terms.currency.status,
        ),
        sla=PublicSLA(
            applicability=document.payload.global_terms.sla.applicability,
            percentage=document.payload.global_terms.sla.percentage,
            measurement_period=document.payload.global_terms.sla.measurement_period,
            scope=document.payload.global_terms.sla.scope,
            exclusions=document.payload.global_terms.sla.exclusions,
        ),
        volume_discounts=[
            PublicVolumeDiscount(
                volume_from=row.volume_from,
                volume_to=row.volume_to,
                discount_percentage=row.discount_percentage,
                condition=row.condition,
                status=row.status,
            )
            for row in document.payload.global_terms.volume_discounts
        ],
        markdown=document.markdown,
        markdown_hash=document.markdown_hash,
        download_filename=download_filename,
    )


def canonical_contract_payload(contract: BaseModel) -> object:
    """Return the same stable, JSON-safe representation used for contract hashes."""

    return canonical_value(contract)
