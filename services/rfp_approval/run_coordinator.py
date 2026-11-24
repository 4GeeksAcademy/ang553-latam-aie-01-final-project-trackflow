"""Transactional Parte 2 -> Parte 3 approval-run initialization.

The client supplies only the ticket/run correlation and an idempotency key.
Every revision, artifact, hash and status is derived from the persisted Parte 2
handoff.  The caller owns the transaction; no operation here commits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlmodel import Session

from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    RFPResponseHandoff,
    SafePreview,
    SectionEvaluationResult,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalRevisionValidation,
    ApprovalRunStart,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    AuthenticatedPrincipal,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    DepartmentCorrectedDraftPayload,
    DepartmentRevision,
    ImmutableArtifactReference,
    InitialRevisionReference,
    RevisionValidationReferences,
    RunInitiator,
    SafePreviewArtifactPayload,
    department_revision_content_hash,
    indicative_warnings_are_reviewable,
)
from services.rfp_approval.artifact_store import (
    ArtifactProducerContext,
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import (
    AuthorizationReference,
    EffectiveAuthorization,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.store import (
    ApprovalStoreError,
    approval_start_request_hash,
    start_approval_run,
)
from services.rfp_approval.models import rfp_approval_runs
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_previews,
)


@dataclass(frozen=True, slots=True)
class InitializedDepartmentRevision:
    department: str
    revision_id: UUID
    status: str
    validation_bundle_id: UUID
    ready_for_hitl: bool


@dataclass(frozen=True, slots=True)
class ApprovalRunInitializationResult:
    approval_run_id: UUID
    ticket_id: UUID
    response_run_id: UUID
    revisions: tuple[InitializedDepartmentRevision, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _DepartmentChain:
    revision: DepartmentRevision
    draft: CorrectedDraftArtifact
    evaluation: ApprovalValidationArtifact
    preview: ApprovalValidationArtifact
    bundle: ApprovalValidationBundle


def _stable_id(approval_run_id: UUID, department: str, kind: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"trackflow:{approval_run_id}:initial:{department}:{kind}:v1",
    )


def _provenance(
    *,
    node: str,
    execution_id: UUID,
    source_kind: str,
    source_ids: list[UUID],
    source_hashes: list[str],
    produced_at: datetime,
) -> ApprovalArtifactProvenance:
    values = {
        "producer_node": node,
        "execution_id": execution_id,
        "source_kind": source_kind,
        "source_artifact_ids": source_ids,
        "source_hashes": source_hashes,
        "produced_at": produced_at,
    }
    return ApprovalArtifactProvenance(
        **values, provenance_hash=canonical_hash(values)
    )


def _evidence(draft: DepartmentDraft):
    ordered = []
    seen = set()
    groups = [
        *(claim.evidence for claim in draft.supported_claims),
        *(response.evidence for response in draft.aspect_responses),
        *(term.evidence for term in draft.commercial_terms),
    ]
    for group in groups:
        for item in group:
            key = (item.document_id, item.section_id, item.evidence_hash)
            if key not in seen:
                seen.add(key)
                ordered.append(item)
    return ordered


def _load_handoff(
    session: Session, *, ticket_id: UUID, response_run_id: UUID | None
) -> tuple[RFPResponseHandoff, str]:
    query = select(rfp_response_handoffs).where(
        rfp_response_handoffs.c.ticket_id == ticket_id
    )
    if response_run_id is not None:
        query = query.where(rfp_response_handoffs.c.run_id == response_run_id)
    row = session.execute(query).mappings().one_or_none()
    if row is None or canonical_hash(row["payload"]) != row["payload_hash"]:
        raise ApprovalStoreError("response_handoff_not_found_or_invalid")
    try:
        handoff = RFPResponseHandoff.model_validate_json(json.dumps(row["payload"]))
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("response_handoff_payload_invalid") from error
    if handoff.ticket_id != ticket_id or (
        response_run_id is not None and handoff.run_id != response_run_id
    ):
        raise ApprovalStoreError("response_handoff_scope_mismatch")
    return handoff, row["payload_hash"]


def _load_department_source(
    session: Session,
    *,
    handoff: RFPResponseHandoff,
    department_result,
) -> tuple[
    DepartmentDraft,
    SectionEvaluationResult,
    SafePreview,
    str,
    str,
    str,
    datetime,
]:
    draft_row = session.execute(
        select(rfp_response_drafts).where(
            rfp_response_drafts.c.draft_id == department_result.final_draft_id,
            rfp_response_drafts.c.run_id == handoff.run_id,
            rfp_response_drafts.c.scope_key == department_result.department.value,
        )
    ).mappings().one_or_none()
    evaluation_row = session.execute(
        select(rfp_response_evaluations).where(
            rfp_response_evaluations.c.evaluation_id
            == department_result.evaluation_result_id,
            rfp_response_evaluations.c.run_id == handoff.run_id,
            rfp_response_evaluations.c.draft_id == department_result.final_draft_id,
            rfp_response_evaluations.c.scope_key
            == department_result.department.value,
        )
    ).mappings().one_or_none()
    preview_row = session.execute(
        select(rfp_response_previews).where(
            rfp_response_previews.c.preview_id == department_result.safe_preview_id,
            rfp_response_previews.c.draft_id == department_result.final_draft_id,
        )
    ).mappings().one_or_none()
    if draft_row is None or evaluation_row is None or preview_row is None:
        raise ApprovalStoreError("part2_department_artifacts_incomplete")
    if canonical_hash(draft_row["payload"]) != draft_row["payload_hash"]:
        raise ApprovalStoreError("source_department_draft_hash_invalid")
    if canonical_hash(evaluation_row["payload"]) != evaluation_row["payload_hash"]:
        raise ApprovalStoreError("source_department_evaluation_hash_invalid")
    try:
        draft = DepartmentDraft.model_validate_json(json.dumps(draft_row["payload"]))
        evaluation = SectionEvaluationResult.model_validate_json(
            json.dumps(evaluation_row["payload"])
        )
        preview = SafePreview(
            preview_id=preview_row["preview_id"],
            draft_id=preview_row["draft_id"],
            gate_status=preview_row["gate_status"],
            content=preview_row["safe_content"],
            reason_codes=preview_row["reason_codes"],
        )
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("part2_department_contract_invalid") from error
    expected = (
        handoff.ticket_id,
        handoff.run_id,
        department_result.department,
        department_result.final_draft_id,
        department_result.iteration_count,
    )
    if (
        (
            draft.ticket_id,
            draft.run_id,
            draft.department,
            draft.draft_id,
            draft.iteration_count,
        )
        != expected
        or evaluation.ticket_id != handoff.ticket_id
        or evaluation.run_id != handoff.run_id
        or evaluation.department != department_result.department
        or evaluation.draft_id != draft.draft_id
        or evaluation.draft_hash != draft_row["payload_hash"]
        or evaluation.iteration_count != draft.iteration_count
        or evaluation.terminal_status != department_result.terminal_status
        or preview.draft_id != draft.draft_id
    ):
        raise ApprovalStoreError("part2_department_identity_mismatch")
    if preview.gate_status != "passed" or not preview.content:
        raise ApprovalStoreError("part2_safe_preview_not_displayable")
    # Department previews are defined by Parte 2 as the verified draft content
    # itself. Binding them back to the hash-verified draft prevents a mutated
    # preview row from being re-sealed as new evidence during import.
    if preview.content != draft.generated_content:
        raise ApprovalStoreError("source_department_preview_content_mismatch")
    validated_at = max(
        draft.generated_at,
        evaluation.evaluated_at,
        preview_row["created_at"],
    )
    preview_import_hash = canonical_hash(
        {
            "preview_id": preview.preview_id,
            "draft_id": preview.draft_id,
            "gate_status": preview.gate_status,
            "safe_content": preview.content,
            "reason_codes": preview.reason_codes,
        }
    )
    return (
        draft,
        evaluation,
        preview,
        draft_row["payload_hash"],
        evaluation_row["payload_hash"],
        preview_import_hash,
        validated_at,
    )


def _build_chain(
    *,
    approval_run_id: UUID,
    handoff: RFPResponseHandoff,
    department_result,
    source: tuple[
        DepartmentDraft,
        SectionEvaluationResult,
        SafePreview,
        str,
        str,
        str,
        datetime,
    ],
    execution_id: UUID,
) -> _DepartmentChain:
    (
        draft,
        evaluation,
        preview,
        source_draft_hash,
        source_evaluation_hash,
        preview_import_hash,
        validated_at,
    ) = source
    pending_codes = [item.code for item in draft.pending_commercial_items]
    reviewable_warnings = indicative_warnings_are_reviewable(
        blocking_rule_ids=evaluation.blocking_rule_ids,
        unresolved_input_codes=pending_codes,
    )
    mandatory_compliance_passed = (
        evaluation.compliance.passed
        and evaluation.readability.passed is not False
        and evaluation.relevance.passed
        and preview.gate_status == "passed"
    )
    if not mandatory_compliance_passed or not reviewable_warnings:
        status = "blocked_compliance"
    else:
        status = "pending_assessment"
    evidence = _evidence(draft)
    content_hash = department_revision_content_hash(
        content=draft.generated_content,
        commercial_terms=draft.commercial_terms,
        pending_commercial_items=draft.pending_commercial_items,
        evidence=evidence,
    )
    department = draft.department.value
    node = "initialize_approval_run"
    draft_id = _stable_id(approval_run_id, department, "draft")
    corrected_payload = DepartmentCorrectedDraftPayload(
        department=draft.department,
        content=draft.generated_content,
        commercial_terms=draft.commercial_terms,
        pending_commercial_items=draft.pending_commercial_items,
        evidence=evidence,
    )
    corrected = CorrectedDraftArtifact(
        draft_artifact_id=draft_id,
        approval_run_id=approval_run_id,
        ticket_id=handoff.ticket_id,
        scope_key=department,
        payload=corrected_payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(corrected_payload),
        provenance=_provenance(
            node=node,
            execution_id=execution_id,
            source_kind="part2_import",
            source_ids=[draft.draft_id],
            source_hashes=[source_draft_hash],
            produced_at=validated_at,
        ),
        created_at=validated_at,
    )
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=mandatory_compliance_passed,
        blocks_commercial_completeness=evaluation.blocks_commercial_completeness,
        blocking_rule_ids=evaluation.blocking_rule_ids,
        unresolved_input_codes=pending_codes,
        open_conflict_ids=[],
        eligible=mandatory_compliance_passed and reviewable_warnings,
    )
    preview_payload = SafePreviewArtifactPayload(
        gate_status=preview.gate_status,
        rendered_content=preview.content,
        rendered_content_hash=(
            canonical_hash({"rendered_content": preview.content})
            if preview.content is not None
            else None
        ),
        reason_codes=preview.reason_codes,
    )
    evaluation_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="part2_import",
        source_ids=[draft_id, evaluation.evaluation_id],
        source_hashes=[content_hash, source_evaluation_hash],
        produced_at=validated_at,
    )
    preview_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="part2_import",
        source_ids=[draft_id, preview.preview_id],
        source_hashes=[content_hash, preview_import_hash],
        produced_at=validated_at,
    )
    evaluation_id = _stable_id(approval_run_id, department, "evaluation")
    preview_id = _stable_id(approval_run_id, department, "preview")
    evaluation_artifact = ApprovalValidationArtifact(
        artifact_id=evaluation_id,
        artifact_type="evaluation",
        approval_run_id=approval_run_id,
        ticket_id=handoff.ticket_id,
        scope_key=department,
        corrected_draft_id=draft_id,
        evaluated_content_hash=content_hash,
        payload=evaluation_payload,
        payload_hash=canonical_hash(evaluation_payload),
        provenance=evaluation_provenance,
        created_at=validated_at,
    )
    preview_artifact = ApprovalValidationArtifact(
        artifact_id=preview_id,
        artifact_type="safe_preview",
        approval_run_id=approval_run_id,
        ticket_id=handoff.ticket_id,
        scope_key=department,
        corrected_draft_id=draft_id,
        evaluated_content_hash=content_hash,
        payload=preview_payload,
        payload_hash=canonical_hash(preview_payload),
        provenance=preview_provenance,
        created_at=validated_at,
    )
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="evaluation",
        artifact_id=evaluation_id,
        contract_version=evaluation_artifact.contract_version,
        payload_hash=evaluation_artifact.payload_hash,
        evaluated_content_hash=content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=preview_id,
        contract_version=preview_artifact.contract_version,
        payload_hash=preview_artifact.payload_hash,
        evaluated_content_hash=content_hash,
    )
    references_values = {
        "revision_content_hash": content_hash,
        "evaluation": evaluation_ref,
        "safe_preview": preview_ref,
        "validated_at": validated_at,
    }
    validation_hash = canonical_hash(references_values)
    validation = ApprovalRevisionValidation(
        references=RevisionValidationReferences(
            **references_values, validation_hash=validation_hash
        ),
        mandatory_compliance_passed=evaluation_payload.mandatory_compliance_passed,
        blocks_commercial_completeness=(
            evaluation_payload.blocks_commercial_completeness
        ),
        blocking_rule_ids=evaluation_payload.blocking_rule_ids,
        unresolved_input_codes=evaluation_payload.unresolved_input_codes,
        open_conflict_ids=[],
        global_revalidation_required=False,
        eligible_for_approval=evaluation_payload.eligible,
    )
    bundle_provenance = _provenance(
        node=node,
        execution_id=execution_id,
        source_kind="part2_import",
        source_ids=[draft_id, evaluation_id, preview_id],
        source_hashes=[
            content_hash,
            evaluation_artifact.payload_hash,
            preview_artifact.payload_hash,
        ],
        produced_at=validated_at,
    )
    bundle_values = {
        "approval_run_id": approval_run_id,
        "ticket_id": handoff.ticket_id,
        "scope_key": department,
        "corrected_draft_id": draft_id,
        "content_hash": content_hash,
        "evaluation_artifact_id": evaluation_id,
        "evaluation_artifact_type": "evaluation",
        "evaluation_contract_version": evaluation_artifact.contract_version,
        "evaluation_payload_hash": evaluation_artifact.payload_hash,
        "safe_preview_artifact_id": preview_id,
        "safe_preview_contract_version": preview_artifact.contract_version,
        "safe_preview_payload_hash": preview_artifact.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": evaluation_payload.mandatory_compliance_passed,
        "blocks_commercial_completeness": (
            evaluation_payload.blocks_commercial_completeness
        ),
        "blocking_rule_ids": evaluation_payload.blocking_rule_ids,
        "unresolved_input_codes": evaluation_payload.unresolved_input_codes,
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": evaluation_payload.eligible,
        "provenance": bundle_provenance,
        "validated_at": validated_at,
    }
    bundle = ApprovalValidationBundle(
        validation_bundle_id=_stable_id(approval_run_id, department, "bundle"),
        **bundle_values,
        bundle_hash=canonical_hash(bundle_values),
    )
    revision = DepartmentRevision(
        revision_id=_stable_id(approval_run_id, department, "revision"),
        draft_id=draft_id,
        ticket_id=handoff.ticket_id,
        approval_run_id=approval_run_id,
        department=draft.department,
        review_revision_no=1,
        source_part2_draft_id=draft.draft_id,
        source_part2_draft_hash=source_draft_hash,
        source_part2_evaluation_id=evaluation.evaluation_id,
        source_part2_evaluation_hash=source_evaluation_hash,
        source_part2_iteration=draft.iteration_count,
        content=draft.generated_content,
        content_hash=content_hash,
        commercial_terms=draft.commercial_terms,
        pending_commercial_items=draft.pending_commercial_items,
        evidence=evidence,
        validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        status=status,
        creation_reason="part2_import",
        created_at=validated_at,
    )
    return _DepartmentChain(
        revision=revision,
        draft=corrected,
        evaluation=evaluation_artifact,
        preview=preview_artifact,
        bundle=bundle,
    )


def initialize_approval_run_from_handoff(
    session: Session,
    *,
    ticket_id: UUID,
    response_run_id: UUID | None,
    identity: ResolvedIdentity,
    authorization: EffectiveAuthorization,
    idempotency_key: UUID,
    requested_at: datetime,
    automate_proposal_materialization: bool = False,
) -> ApprovalRunInitializationResult:
    """Initialize Parte 3 atomically from persisted Parte 2 artifacts."""

    if authorization.permission not in {"start_approval_run", "manage_approval_run"}:
        raise ApprovalStoreError("start_approval_run_permission_required")
    if automate_proposal_materialization and authorization.permission != "manage_approval_run":
        raise ApprovalStoreError("proposal_automation_requires_run_management")
    if requested_at.tzinfo is None:
        raise ApprovalStoreError("requested_at_timezone_required")
    handoff, handoff_hash = _load_handoff(
        session, ticket_id=ticket_id, response_run_id=response_run_id
    )
    response_run_id = handoff.run_id
    existing = session.execute(
        select(
            rfp_approval_runs.c.approval_run_id,
            rfp_approval_runs.c.started_at,
        ).where(rfp_approval_runs.c.start_idempotency_key == idempotency_key)
    ).mappings().one_or_none()
    if existing is None:
        approval_run_id = uuid5(
            NAMESPACE_URL,
            f"trackflow:{ticket_id}:{response_run_id}:{handoff_hash}:{idempotency_key}",
        )
    else:
        # Reconstruct the exact persisted envelope. Authorization is still
        # rechecked by start_approval_run before any replay is returned.
        approval_run_id = existing["approval_run_id"]
        requested_at = existing["started_at"]
    execution_id = _stable_id(approval_run_id, "run", "execution")
    try:
        chains = [
            _build_chain(
                approval_run_id=approval_run_id,
                handoff=handoff,
                department_result=result,
                source=_load_department_source(
                    session, handoff=handoff, department_result=result
                ),
                execution_id=execution_id,
            )
            for result in handoff.department_results
        ]
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("part2_initial_revision_contract_invalid") from error
    principal = AuthenticatedPrincipal(
        issuer=identity.issuer,
        subject=identity.subject,
        trackflow_user_id=identity.trackflow_user_id,
        auth_source=identity.auth_source,
        general_role=identity.general_role,
        synthetic=identity.synthetic,
    )
    references = [
        InitialRevisionReference(
            department=chain.revision.department,
            source_draft_id=chain.revision.source_part2_draft_id,
            source_draft_hash=chain.revision.source_part2_draft_hash,
            source_evaluation_id=chain.revision.source_part2_evaluation_id,
            source_evaluation_hash=chain.revision.source_part2_evaluation_hash,
            source_part2_iteration=chain.revision.source_part2_iteration,
            terminal_status=next(
                item.terminal_status
                for item in handoff.department_results
                if item.department == chain.revision.department
            ),
        )
        for chain in chains
    ]
    start = ApprovalRunStart(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        response_handoff_hash=handoff_hash,
        active_departments=handoff.selected_departments,
        initial_revisions=references,
        started_by=RunInitiator(
            principal=principal,
            authorization_id=authorization.authorization_id,
            permission=authorization.permission,
        ),
        automate_proposal_materialization=automate_proposal_materialization,
        started_at=requested_at,
    )
    reference = AuthorizationReference(
        authorization.authorization_id, authorization.authorization_version
    )
    request_hash = approval_start_request_hash(
        start, [chain.revision for chain in chains], idempotency_key
    )
    run = start_approval_run(
        session,
        start=start,
        initial_revisions=[chain.revision for chain in chains],
        identity=identity,
        authorization_reference=reference,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        execution_id=execution_id,
    )
    producer = ArtifactProducerContext._from_verified_orchestrator(
        environment=identity.environment,
        producer_node="initialize_approval_run",
        execution_id=execution_id,
    )
    initialized = []
    for chain in chains:
        persist_corrected_draft(session, draft=chain.draft, producer=producer)
        persist_validation_artifact(
            session, artifact=chain.evaluation, producer=producer
        )
        persist_validation_artifact(session, artifact=chain.preview, producer=producer)
        persist_validation_bundle(session, bundle=chain.bundle, producer=producer)
        # Conflict assessment for all three official triggers must be persisted
        # against these exact hashes before the public link operation can expose
        # the revision to HITL.
        ready = False
        initialized.append(
            InitializedDepartmentRevision(
                department=chain.revision.department.value,
                revision_id=chain.revision.revision_id,
                status=chain.revision.status,
                validation_bundle_id=chain.bundle.validation_bundle_id,
                ready_for_hitl=ready,
            )
        )
    return ApprovalRunInitializationResult(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        revisions=tuple(initialized),
        replayed=run.replayed,
    )
