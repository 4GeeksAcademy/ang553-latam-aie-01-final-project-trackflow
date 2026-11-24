"""Authorized creation of the first global proposal after department sign-off.

This service closes the intentional gap between independent departmental
approval and fan-in. It imports unchanged, already evaluated Parte 2 global
terms only when every current department revision has a durable applied
approval. It never invents commercial values and never acts without an exact
``manage_approval_run`` authorization resolved by the API/worker boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, select
from sqlmodel import Session

from data.pipelines.rfp_response.contracts import (
    EvidenceReference,
    GlobalCommercialTermsDraft,
    ProposalEvaluationResult,
    VolumeDiscountRow,
)
from data.pipelines.rfp_response.compliance import build_global_safe_preview
from data.pipelines.rfp_response.evaluators import evaluate_global, inspect_global_safety
from data.pipelines.rfp_response.generators import generate_global_terms
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    AuthenticatedPrincipal,
    AuthorizedHumanActor,
    BusinessInputProvenance,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    ImmutableArtifactReference,
    InitialProposalRevisionCommand,
    MonthlyStorageVolumeInput,
    ProposalCorrectedDraftPayload,
    ProposalRevision,
    ProposalRevisionValidation,
    RunInitiator,
    SafePreviewArtifactPayload,
    VolumeDiscountInput,
    indicative_warnings_are_reviewable,
    proposal_revision_content_hash,
)
from pipelines.rfp_approval.fan_in import determine_fan_in_readiness
from services.rfp_approval.artifact_store import (
    ArtifactProducerContext,
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import (
    AuthorizationReference,
    AuthorizationTarget,
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from services.rfp_approval.commercial_store import (
    ProposalRevisionRecord,
    create_initial_proposal_revision,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_proposal_revisions,
    rfp_human_decisions,
    rfp_authorized_business_inputs,
    rfp_identity_bindings,
    rfp_principals,
)
from services.rfp_approval.store import (
    ApprovalStoreError,
    _persisted_revision_creation_reason,
)
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_previews,
)
from services.rfp_response.store import load_response_start


@dataclass(frozen=True, slots=True)
class ProposalMaterializationResult:
    proposal_revision_id: UUID
    approval_run_id: UUID
    eligible_for_synthesis: bool
    replayed: bool


def _stable_id(approval_run_id: UUID, label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"trackflow:{approval_run_id}:proposal:{label}:v1")


def _provenance(
    *,
    producer_node: str,
    execution_id: UUID,
    source_kind: str,
    source_artifact_ids: list[UUID],
    source_hashes: list[str],
    produced_at: datetime,
) -> ApprovalArtifactProvenance:
    values = {
        "producer_node": producer_node,
        "execution_id": execution_id,
        "source_kind": source_kind,
        "source_artifact_ids": source_artifact_ids,
        "source_hashes": source_hashes,
        "produced_at": produced_at,
    }
    return ApprovalArtifactProvenance(
        **values, provenance_hash=canonical_hash(values)
    )


def _assert_part2_global_source_eligibility(
    draft: GlobalCommercialTermsDraft,
    evaluation: ProposalEvaluationResult,
    preview_row: dict[str, object],
    *,
    require_department_rollup_pass: bool,
) -> None:
    """Keep intrinsic global safety distinct from the historical rollup."""

    pending_codes = [item.code for item in draft.pending_commercial_items]
    if (
        not evaluation.global_compliance.passed
        or not indicative_warnings_are_reviewable(
            blocking_rule_ids=evaluation.blocking_rule_ids,
            unresolved_input_codes=pending_codes,
        )
        or preview_row["gate_status"] != "passed"
        or not preview_row["safe_content"]
    ):
        raise ApprovalStoreError("part2_global_source_not_eligible_for_import")
    if require_department_rollup_pass and evaluation.outcome not in {
        "ready_for_part3",
        "needs_human_review",
    }:
        raise ApprovalStoreError("part2_global_source_not_eligible_for_import")


def _rehydrate_global_input(
    payload_type,
    row: dict[str, object],
    error_code: str,
    *,
    field: str = "payload",
):
    try:
        return payload_type.model_validate_json(json.dumps(row[field]))
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError(error_code) from error


def _effective_global_source_from_authorized_volume(
    session: Session,
    *,
    run: dict[str, object],
    source: GlobalCommercialTermsDraft,
    evaluation: ProposalEvaluationResult,
    preview_row: dict[str, object],
) -> tuple[GlobalCommercialTermsDraft, ProposalEvaluationResult, dict[str, object]]:
    """Build a fresh in-memory P3 source without rewriting historical P2 rows."""

    pending_codes = {item.code for item in source.pending_commercial_items}
    blocking_rules = {
        item.rule_id for item in evaluation.global_compliance.findings if item.blocking
    }
    if (
        not pending_codes
        or not pending_codes.issubset(
            {"MONTHLY_STORAGE_VOLUME_REQUIRED", "VOLUME_DISCOUNT_APPROVAL_REQUIRED"}
        )
        or not blocking_rules.issubset({"TF-DISCOUNT-001"})
    ):
        raise ApprovalStoreError("part2_global_source_not_eligible_for_import")
    today = session.execute(select(func.current_date())).scalar_one()
    rows = session.execute(
        select(rfp_authorized_business_inputs)
        .where(
            rfp_authorized_business_inputs.c.approval_run_id
            == run["approval_run_id"],
            rfp_authorized_business_inputs.c.ticket_id == run["ticket_id"],
            rfp_authorized_business_inputs.c.input_kind
            == "monthly_storage_volume",
            rfp_authorized_business_inputs.c.department_scope.is_(None),
        )
        .order_by(rfp_authorized_business_inputs.c.recorded_at.desc())
    ).mappings().all()
    effective: list[tuple[dict[str, object], MonthlyStorageVolumeInput]] = []
    for raw in rows:
        row = dict(raw)
        value = _rehydrate_global_input(
            MonthlyStorageVolumeInput,
            row,
            "authorized_volume_payload_invalid",
        )
        provenance = _rehydrate_global_input(
            BusinessInputProvenance,
            row,
            "authorized_volume_provenance_invalid",
            field="provenance",
        )
        if (
            canonical_hash(provenance) != row["provenance_hash"]
            or provenance.source_payload_hash != row["evidence_hash"]
        ):
            raise ApprovalStoreError("authorized_volume_provenance_invalid")
        if provenance.valid_until is not None and today > provenance.valid_until:
            continue
        if value.effective_from <= today and (
            value.effective_until is None or today <= value.effective_until
        ):
            effective.append((row, value))
    if not effective:
        raise ApprovalStoreError("part2_global_source_not_eligible_for_import")
    distinct = {row["payload_hash"] for row, _value in effective}
    if len(distinct) != 1:
        raise ApprovalStoreError("authorized_monthly_volume_conflict")
    volume_row, volume = effective[0]
    unit = volume.unit.strip().lower().replace("³", "3")
    period = volume.period.strip().lower()
    scope = volume.operational_scope
    if (
        unit not in {"m3", "cubic meter", "cubic meters", "metro cubico", "metros cubicos"}
        or period not in {"month", "monthly", "mes", "mensual"}
        or not (
            scope.scope_code == "monthly-storage-volume"
            or scope.service_code in {"warehouse-storage", "warehouse-lastmile-throughput"}
        )
    ):
        raise ApprovalStoreError("authorized_monthly_volume_incompatible")
    request = load_response_start(session, run_id=run["response_run_id"])
    if request.average_monthly_volume_m3 is not None and (
        Decimal(str(request.average_monthly_volume_m3)) != volume.value
    ):
        raise ApprovalStoreError("monthly_storage_volume_source_contradiction")
    request = request.model_copy(
        update={"average_monthly_volume_m3": float(volume.value)}
    )
    generated = generate_global_terms(
        request,
        department_draft_ids=source.source_section_ids,
        iteration_count=source.global_iteration_count,
        generated_at=source.generated_at,
        generation_feedback=source.generation_feedback,
    )
    authorized_discounts: tuple[VolumeDiscountRow, ...] = ()
    if any(
        item.code == "VOLUME_DISCOUNT_APPROVAL_REQUIRED"
        for item in generated.pending_commercial_items
    ):
        discount_rows = session.execute(
            select(rfp_authorized_business_inputs)
            .where(
                rfp_authorized_business_inputs.c.approval_run_id
                == run["approval_run_id"],
                rfp_authorized_business_inputs.c.ticket_id == run["ticket_id"],
                rfp_authorized_business_inputs.c.input_kind == "volume_discount",
                rfp_authorized_business_inputs.c.department_scope.is_(None),
            )
            .order_by(rfp_authorized_business_inputs.c.recorded_at.desc())
        ).mappings().all()
        selected: tuple[dict[str, object], VolumeDiscountInput] | None = None
        for raw in discount_rows:
            row = dict(raw)
            value = _rehydrate_global_input(
                VolumeDiscountInput,
                row,
                "authorized_discount_payload_invalid",
            )
            provenance = _rehydrate_global_input(
                BusinessInputProvenance,
                row,
                "authorized_discount_provenance_invalid",
                field="provenance",
            )
            if (
                canonical_hash(provenance) != row["provenance_hash"]
                or provenance.source_payload_hash != row["evidence_hash"]
            ):
                raise ApprovalStoreError("authorized_discount_provenance_invalid")
            if provenance.valid_until is not None and today > provenance.valid_until:
                continue
            if value.effective_from <= today and (
                value.effective_until is None or today <= value.effective_until
            ):
                selected = (row, value)
                break
        if selected is not None:
            discount_row, discount = selected
            if (
                discount.volume_unit.strip().lower().replace("³", "3") != "m3"
                or discount.period.strip().lower()
                not in {"month", "monthly", "mes", "mensual"}
            ):
                raise ApprovalStoreError("authorized_volume_discount_incompatible")
            evidence = EvidenceReference(
                document_id="authorized-business-input-v1",
                section_id=str(discount_row["input_id"]),
                evidence_hash=discount_row["payload_hash"],
            )
            authorized_discounts = tuple(
                VolumeDiscountRow(
                    volume_from=float(tier.minimum_volume),
                    volume_to=(
                        None
                        if tier.maximum_volume is None
                        else float(tier.maximum_volume)
                    ),
                    discount_percentage=float(tier.discount_percentage),
                    condition="Authorized monthly storage volume discount policy.",
                    status="documented",
                    evidence=[evidence],
                )
                for tier in discount.tiers
            )
            generated = generated.model_copy(
                update={
                    "volume_discounts": list(authorized_discounts),
                    "pending_commercial_items": [
                        item
                        for item in generated.pending_commercial_items
                        if item.code != "VOLUME_DISCOUNT_APPROVAL_REQUIRED"
                    ],
                }
            )
    generated = generated.model_copy(update={"draft_id": source.draft_id})
    inspection = inspect_global_safety(
        request,
        generated,
        inspected_at=evaluation.evaluated_at,
        authorized_discount_rows=authorized_discounts,
    )
    preview = build_global_safe_preview(generated, inspection)
    compliance, _feedback = evaluate_global(
        request,
        generated,
        section_currencies=frozenset(
            [generated.currency.value] if generated.currency.value else []
        ),
        consistency_issues=[],
        evaluated_at=evaluation.evaluated_at,
        authorized_discount_rows=authorized_discounts,
    )
    blocking = [item for item in compliance.findings if item.blocking]
    blocks = any(
        item.blocks_commercial_completeness for item in compliance.findings
    )
    human = any(item.requires_human_input for item in compliance.findings)
    passed = evaluation.all_departments_passed and compliance.passed and not blocks and not human
    regenerable = (
        not passed
        and not human
        and bool(blocking)
        and evaluation.global_iteration_count < 3
        and all(item.regenerable for item in blocking)
    )
    corrected_evaluation = ProposalEvaluationResult(
        **{
            **evaluation.model_dump(mode="python"),
            "global_compliance": compliance,
            "blocking_rule_ids": [item.rule_id for item in blocking],
            "blocks_commercial_completeness": blocks,
            "requires_human_input": human,
            "regenerable": regenerable,
            "overall_pass": passed,
            "outcome": "ready_for_part3" if passed else "regenerate" if regenerable else "needs_human_review",
            "terminal_status": "ready_for_part3" if passed else "needs_human_input" if human else "iteration_limit_reached" if evaluation.global_iteration_count == 3 else None,
        }
    )
    return generated, corrected_evaluation, {
        **preview_row,
        "preview_id": preview.preview_id,
        "draft_id": source.draft_id,
        "gate_status": preview.gate_status,
        "safe_content": preview.content,
        "reason_codes": preview.reason_codes,
        "created_at": evaluation.evaluated_at,
        "authorized_input_ids": [
            volume_row["input_id"],
            *(
                [discount_row["input_id"]]
                if authorized_discounts
                else []
            ),
        ],
    }


def _assert_no_authorized_volume_contradiction(
    session: Session, *, run: dict[str, object]
) -> None:
    """Never ignore a later authorized fact that contradicts documented intake."""

    rows = session.execute(
        select(rfp_authorized_business_inputs)
        .where(
            rfp_authorized_business_inputs.c.approval_run_id
            == run["approval_run_id"],
            rfp_authorized_business_inputs.c.ticket_id == run["ticket_id"],
            rfp_authorized_business_inputs.c.input_kind
            == "monthly_storage_volume",
            rfp_authorized_business_inputs.c.department_scope.is_(None),
        )
    ).mappings().all()
    if not rows:
        return
    request = load_response_start(session, run_id=run["response_run_id"])
    if request.average_monthly_volume_m3 is None:
        return
    today = session.execute(select(func.current_date())).scalar_one()
    for raw in rows:
        row = dict(raw)
        value = _rehydrate_global_input(
            MonthlyStorageVolumeInput,
            row,
            "authorized_volume_payload_invalid",
        )
        provenance = _rehydrate_global_input(
            BusinessInputProvenance,
            row,
            "authorized_volume_provenance_invalid",
            field="provenance",
        )
        if (
            canonical_hash(provenance) != row["provenance_hash"]
            or provenance.source_payload_hash != row["evidence_hash"]
        ):
            raise ApprovalStoreError("authorized_volume_provenance_invalid")
        if (
            (provenance.valid_until is None or today <= provenance.valid_until)
            and value.effective_from <= today
            and (value.effective_until is None or today <= value.effective_until)
            and Decimal(str(request.average_monthly_volume_m3)) != value.value
        ):
            raise ApprovalStoreError("monthly_storage_volume_source_contradiction")


def _load_part2_sources(
    session: Session,
    run: dict[str, object],
    *,
    require_department_rollup_pass: bool = True,
):
    """Load hash-bound global artifacts without rewriting their history.

    Corrected department revisions may replace the failed department rollup
    that made the historical proposal evaluation non-terminal.  The global
    terms themselves must remain complete, safe and compliant in every mode.
    Initial import additionally requires the original department rollup to
    have passed.
    """
    handoff_row = session.execute(
        select(rfp_response_handoffs).where(
            rfp_response_handoffs.c.run_id == run["response_run_id"],
            rfp_response_handoffs.c.ticket_id == run["ticket_id"],
            rfp_response_handoffs.c.payload_hash == run["response_handoff_hash"],
        )
    ).mappings().one_or_none()
    if handoff_row is None or canonical_hash(handoff_row["payload"]) != handoff_row["payload_hash"]:
        raise ApprovalStoreError("response_handoff_not_found_or_invalid")
    try:
        from data.pipelines.rfp_response.contracts import RFPResponseHandoff

        handoff = RFPResponseHandoff.model_validate_json(json.dumps(handoff_row["payload"]))
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("response_handoff_payload_invalid") from error
    draft_row = session.execute(
        select(rfp_response_drafts).where(
            rfp_response_drafts.c.draft_id == handoff.global_draft_id,
            rfp_response_drafts.c.run_id == handoff.run_id,
            rfp_response_drafts.c.scope_key == "proposal",
        )
    ).mappings().one_or_none()
    evaluation_row = session.execute(
        select(rfp_response_evaluations).where(
            rfp_response_evaluations.c.evaluation_id
            == handoff.global_evaluation_result_id,
            rfp_response_evaluations.c.run_id == handoff.run_id,
            rfp_response_evaluations.c.draft_id == handoff.global_draft_id,
            rfp_response_evaluations.c.scope_key == "proposal",
        )
    ).mappings().one_or_none()
    preview_row = session.execute(
        select(rfp_response_previews).where(
            rfp_response_previews.c.draft_id == handoff.global_draft_id
        )
    ).mappings().one_or_none()
    if draft_row is None or evaluation_row is None or preview_row is None:
        raise ApprovalStoreError("part2_global_artifacts_incomplete")
    if canonical_hash(draft_row["payload"]) != draft_row["payload_hash"]:
        raise ApprovalStoreError("source_global_draft_hash_invalid")
    if canonical_hash(evaluation_row["payload"]) != evaluation_row["payload_hash"]:
        raise ApprovalStoreError("source_global_evaluation_hash_invalid")
    try:
        draft = GlobalCommercialTermsDraft.model_validate_json(
            json.dumps(draft_row["payload"])
        )
        evaluation = ProposalEvaluationResult.model_validate_json(
            json.dumps(evaluation_row["payload"])
        )
    except (TypeError, ValueError) as error:
        raise ApprovalStoreError("part2_global_contract_invalid") from error
    if (
        draft.draft_id != handoff.global_draft_id
        or evaluation.evaluation_id != handoff.global_evaluation_result_id
        or evaluation.global_draft_id != draft.draft_id
    ):
        raise ApprovalStoreError("part2_global_identity_mismatch")
    _assert_no_authorized_volume_contradiction(session, run=run)
    effective_preview = dict(preview_row)
    pending_codes = {item.code for item in draft.pending_commercial_items}
    if pending_codes and pending_codes.issubset(
        {"MONTHLY_STORAGE_VOLUME_REQUIRED", "VOLUME_DISCOUNT_APPROVAL_REQUIRED"}
    ):
        try:
            draft, evaluation, effective_preview = (
                _effective_global_source_from_authorized_volume(
                    session,
                    run=run,
                    source=draft,
                    evaluation=evaluation,
                    preview_row=effective_preview,
                )
            )
        except ApprovalStoreError as error:
            if error.code != "part2_global_source_not_eligible_for_import":
                raise
    _assert_part2_global_source_eligibility(
        draft,
        evaluation,
        effective_preview,
        require_department_rollup_pass=require_department_rollup_pass,
    )
    return handoff, draft, evaluation, dict(draft_row), dict(evaluation_row), effective_preview


def materialize_initial_proposal(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    identity: ResolvedIdentity,
    authorization: EffectiveAuthorization,
) -> ProposalMaterializationResult:
    """Persist the exact Part 2 proposal and fresh approval-aware validation."""

    if authorization.permission != "manage_approval_run":
        raise ApprovalStoreError("manage_approval_run_required")
    return _materialize_initial_proposal(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        identity=identity,
        authorization_reference=AuthorizationReference(
            authorization.authorization_id,
            authorization.authorization_version,
        ),
    )


def materialize_delegated_initial_proposal(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
) -> ProposalMaterializationResult:
    """Use only the explicit, persisted run-management delegation.

    This is not authentication-by-database-row.  The authenticated manager
    opted into automation while starting the run, and the exact principal,
    binding, general role and authorization version were persisted for this
    one deterministic operation.  The grant is revalidated under lock by the
    materialization store before any artifact is written.
    """

    delegated = session.execute(
        select(
            rfp_approval_runs.c.started_by_principal_id,
            rfp_approval_runs.c.started_by_binding_id,
            rfp_approval_runs.c.start_authorization_id,
            rfp_approval_runs.c.authorization_version,
            rfp_approval_runs.c.started_by_general_role,
            rfp_approval_runs.c.automate_proposal_materialization,
            rfp_approval_runs.c.environment,
            rfp_principals.c.trackflow_user_id,
            rfp_principals.c.responsible_code,
            rfp_principals.c.synthetic,
            rfp_identity_bindings.c.auth_source,
            rfp_identity_bindings.c.issuer,
            rfp_identity_bindings.c.subject,
        )
        .select_from(
            rfp_approval_runs.join(
                rfp_principals,
                and_(
                    rfp_principals.c.principal_id
                    == rfp_approval_runs.c.started_by_principal_id,
                    rfp_principals.c.environment == rfp_approval_runs.c.environment,
                ),
            ).join(
                rfp_identity_bindings,
                and_(
                    rfp_identity_bindings.c.binding_id
                    == rfp_approval_runs.c.started_by_binding_id,
                    rfp_identity_bindings.c.principal_id
                    == rfp_approval_runs.c.started_by_principal_id,
                    rfp_identity_bindings.c.environment
                    == rfp_approval_runs.c.environment,
                ),
            )
        )
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
        )
    ).mappings().one_or_none()
    if delegated is None:
        raise ApprovalStoreError("approval_run_not_found")
    row = dict(delegated)
    if not row["automate_proposal_materialization"]:
        raise ApprovalStoreError("proposal_automation_not_delegated")
    if row["started_by_general_role"] is None:
        raise ApprovalStoreError("proposal_automation_delegation_invalid")
    identity = ResolvedIdentity._from_persisted_binding(
        environment=row["environment"],
        auth_source=row["auth_source"],
        issuer=row["issuer"],
        subject=row["subject"],
        trackflow_user_id=row["trackflow_user_id"],
        general_role=row["started_by_general_role"],
        principal_id=row["started_by_principal_id"],
        binding_id=row["started_by_binding_id"],
        responsible_code=row["responsible_code"],
        synthetic=bool(row["synthetic"]),
    )
    return _materialize_initial_proposal(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        identity=identity,
        authorization_reference=AuthorizationReference(
            row["start_authorization_id"], row["authorization_version"]
        ),
    )


def _materialize_initial_proposal(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
) -> ProposalMaterializationResult:
    run_row = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == identity.environment,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run_row is None:
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    run = dict(run_row)
    if run["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    authorization = resolve_effective_authorization(
        session,
        identity=identity,
        target=AuthorizationTarget(
            permission="manage_approval_run", target_policy="run"
        ),
        reference=authorization_reference,
        lock_rows=True,
    )
    existing = session.execute(
        select(rfp_approval_proposal_revisions).where(
            rfp_approval_proposal_revisions.c.approval_run_id == approval_run_id,
            rfp_approval_proposal_revisions.c.ticket_id == ticket_id,
            rfp_approval_proposal_revisions.c.is_current.is_(True),
        )
    ).mappings().one_or_none()
    if existing is not None:
        return ProposalMaterializationResult(
            proposal_revision_id=existing["proposal_revision_id"],
            approval_run_id=approval_run_id,
            eligible_for_synthesis=existing["eligible_for_synthesis"],
            replayed=True,
        )
    readiness = determine_fan_in_readiness(
        session, approval_run_id=approval_run_id, ticket_id=ticket_id
    )
    if readiness.blockers != ("current_proposal_count",):
        raise ApprovalStoreError(
            "proposal_sources_not_ready:" + ",".join(readiness.blockers)
        )
    department_rows = session.execute(
        select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id.in_(
                [item.revision_id for item in readiness.approved_revisions]
            )
        )
    ).mappings().all()
    by_department = {row["department_id"]: dict(row) for row in department_rows}
    require_department_rollup_pass = all(
        _persisted_revision_creation_reason(row) == "part2_import"
        for row in by_department.values()
    )
    handoff, source, _evaluation, draft_row, evaluation_row, preview_row = (
        _load_part2_sources(
            session,
            run,
            require_department_rollup_pass=require_department_rollup_pass,
        )
    )
    authorized_input_ids = list(preview_row.get("authorized_input_ids", []))
    proposal_authorization = authorization
    proposal_authorization_reference = authorization_reference
    created_by: AuthorizedHumanActor | None = None
    if authorized_input_ids:
        owner_rows = session.execute(
            select(rfp_authorized_business_inputs).where(
                rfp_authorized_business_inputs.c.input_id.in_(authorized_input_ids),
                rfp_authorized_business_inputs.c.approval_run_id == approval_run_id,
                rfp_authorized_business_inputs.c.ticket_id == ticket_id,
            )
        ).mappings().all()
        if len(owner_rows) != len(authorized_input_ids):
            raise ApprovalStoreError("authorized_input_reference_mismatch")
        ownership = {
            (
                row["actor_principal_id"],
                row["actor_binding_id"],
                row["authorization_id"],
                row["authorization_version"],
            )
            for row in owner_rows
        }
        if len(ownership) != 1:
            raise ApprovalStoreError("global_commercial_input_actor_mismatch")
        principal_id, binding_id, authorization_id, authorization_version = next(
            iter(ownership)
        )
        if (
            principal_id != identity.principal_id
            or binding_id != identity.binding_id
            or identity.responsible_code != "miguel_torres"
        ):
            raise ApprovalStoreError("global_commercial_input_actor_mismatch")
        proposal_authorization_reference = AuthorizationReference(
            authorization_id, authorization_version
        )
        proposal_authorization = resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="provide_business_input",
                target_policy="proposal_wide",
            ),
            reference=proposal_authorization_reference,
            lock_rows=True,
        )
        created_by = AuthorizedHumanActor(
            principal=AuthenticatedPrincipal(
                issuer=identity.issuer,
                subject=identity.subject,
                trackflow_user_id=identity.trackflow_user_id,
                auth_source=identity.auth_source,
                general_role=identity.general_role,
                synthetic=identity.synthetic,
            ),
            authorization_id=proposal_authorization.authorization_id,
            official_responsible="Miguel Torres",
            permission="provide_business_input",
            department_scope=None,
        )
    handoff_sources = {
        item.department.value: item for item in handoff.department_results
    }
    if set(by_department) != {item.department.value for item in readiness.approved_revisions}:
        raise ApprovalStoreError("approved_department_source_missing")
    for department, row in by_department.items():
        historical = handoff_sources.get(department)
        if (
            historical is None
            or row["source_part2_draft_id"] != historical.final_draft_id
            or row["source_part2_evaluation_id"] != historical.evaluation_result_id
            or row["source_part2_iteration"] != historical.iteration_count
        ):
            raise ApprovalStoreError("approved_department_part2_heritage_mismatch")
    applied_times = session.execute(
        select(rfp_human_decisions.c.applied_at).where(
            rfp_human_decisions.c.decision_id.in_(
                [item.decision_id for item in readiness.approved_revisions]
            )
        )
    ).scalars().all()
    if not applied_times or any(item is None for item in applied_times):
        raise ApprovalStoreError("approved_decision_timestamp_missing")
    validated_at = max(
        [
            source.generated_at,
            _evaluation.evaluated_at,
            preview_row["created_at"],
            *applied_times,
        ]
    )
    source_revision_ids = [item.revision_id for item in readiness.approved_revisions]
    payload = ProposalCorrectedDraftPayload(
        currency=source.currency,
        sla=source.sla,
        volume_discounts=source.volume_discounts,
        source_department_revision_ids=source_revision_ids,
        authorized_input_ids=authorized_input_ids,
    )
    content_hash = proposal_revision_content_hash(
        currency=source.currency,
        sla=source.sla,
        volume_discounts=source.volume_discounts,
        source_department_revision_ids=source_revision_ids,
        authorized_input_ids=authorized_input_ids,
    )
    execution_id = _stable_id(approval_run_id, "execution")
    producer_node = "coordinate_global_proposal"
    producer = ArtifactProducerContext._from_verified_orchestrator(
        environment=identity.environment,
        producer_node=producer_node,
        execution_id=execution_id,
    )
    draft_id = _stable_id(approval_run_id, "draft")
    draft_provenance = _provenance(
        producer_node=producer_node,
        execution_id=execution_id,
        source_kind=("human_correction" if authorized_input_ids else "part2_import"),
        source_artifact_ids=[source.draft_id, *authorized_input_ids],
        source_hashes=[
            draft_row["payload_hash"],
            *(
                [row["payload_hash"] for row in owner_rows]
                if authorized_input_ids
                else []
            ),
        ],
        produced_at=validated_at,
    )
    draft = CorrectedDraftArtifact(
        draft_artifact_id=draft_id,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        scope_key="proposal",
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=draft_provenance,
        created_at=validated_at,
    )
    unresolved_input_codes = [item.code for item in source.pending_commercial_items]
    proposal_reviewable = indicative_warnings_are_reviewable(
        blocking_rule_ids=_evaluation.blocking_rule_ids,
        unresolved_input_codes=unresolved_input_codes,
    )
    mandatory_compliance_passed = (
        _evaluation.global_compliance.passed
        and preview_row["gate_status"] == "passed"
    )
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=mandatory_compliance_passed,
        blocks_commercial_completeness=bool(unresolved_input_codes),
        blocking_rule_ids=_evaluation.blocking_rule_ids,
        unresolved_input_codes=unresolved_input_codes,
        open_conflict_ids=[],
        eligible=mandatory_compliance_passed and proposal_reviewable,
    )
    preview_payload = SafePreviewArtifactPayload(
        gate_status="passed",
        rendered_content=preview_row["safe_content"],
        rendered_content_hash=canonical_hash(
            {"rendered_content": preview_row["safe_content"]}
        ),
        reason_codes=[],
    )
    ordered_department_hashes = [
        by_department[item.department.value]["content_hash"]
        for item in readiness.approved_revisions
    ]
    evaluation_provenance = _provenance(
        producer_node=producer_node,
        execution_id=execution_id,
        source_kind="deterministic_evaluator",
        source_artifact_ids=[
            draft_id,
            source.draft_id,
            _evaluation.evaluation_id,
            *source_revision_ids,
        ],
        source_hashes=[
            content_hash,
            draft_row["payload_hash"],
            evaluation_row["payload_hash"],
            *ordered_department_hashes,
        ],
        produced_at=validated_at,
    )
    preview_import_hash = canonical_hash(
        {
            "preview_id": preview_row["preview_id"],
            "draft_id": preview_row["draft_id"],
            "gate_status": preview_row["gate_status"],
            "safe_content": preview_row["safe_content"],
            "reason_codes": preview_row["reason_codes"],
        }
    )
    preview_provenance = _provenance(
        producer_node=producer_node,
        execution_id=execution_id,
        source_kind=(
            "compliance_evaluator" if authorized_input_ids else "part2_import"
        ),
        source_artifact_ids=[
            draft_id,
            source.draft_id,
            preview_row["preview_id"],
        ],
        source_hashes=[
            content_hash,
            draft_row["payload_hash"],
            preview_import_hash,
        ],
        produced_at=validated_at,
    )
    evaluation_id = _stable_id(approval_run_id, "evaluation")
    preview_id = _stable_id(approval_run_id, "preview")
    evaluation_artifact = ApprovalValidationArtifact(
        artifact_id=evaluation_id,
        artifact_type="global_evaluation",
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        scope_key="proposal",
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
        ticket_id=ticket_id,
        scope_key="proposal",
        corrected_draft_id=draft_id,
        evaluated_content_hash=content_hash,
        payload=preview_payload,
        payload_hash=canonical_hash(preview_payload),
        provenance=preview_provenance,
        created_at=validated_at,
    )
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="global_evaluation",
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
    validation_values = {
        "revision_content_hash": content_hash,
        "global_evaluation": evaluation_ref,
        "safe_preview": preview_ref,
        "mandatory_compliance_passed": mandatory_compliance_passed,
        "unresolved_input_codes": unresolved_input_codes,
        "blocking_rule_ids": _evaluation.blocking_rule_ids,
        "open_conflict_ids": [],
        "source_departments_approved": True,
        "eligible_for_synthesis": mandatory_compliance_passed and proposal_reviewable,
        "validated_at": validated_at,
    }
    validation_hash = canonical_hash(validation_values)
    validation = ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        **validation_values,
        validation_hash=validation_hash,
    )
    bundle_provenance = _provenance(
        producer_node=producer_node,
        execution_id=execution_id,
        source_kind="deterministic_evaluator",
        source_artifact_ids=[draft_id, evaluation_id, preview_id],
        source_hashes=[
            content_hash,
            evaluation_artifact.payload_hash,
            preview_artifact.payload_hash,
        ],
        produced_at=validated_at,
    )
    bundle_values = {
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "scope_key": "proposal",
        "corrected_draft_id": draft_id,
        "content_hash": content_hash,
        "evaluation_artifact_id": evaluation_id,
        "evaluation_artifact_type": "global_evaluation",
        "evaluation_contract_version": evaluation_artifact.contract_version,
        "evaluation_payload_hash": evaluation_artifact.payload_hash,
        "safe_preview_artifact_id": preview_id,
        "safe_preview_contract_version": preview_artifact.contract_version,
        "safe_preview_payload_hash": preview_artifact.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": mandatory_compliance_passed,
        "blocks_commercial_completeness": bool(unresolved_input_codes),
        "blocking_rule_ids": _evaluation.blocking_rule_ids,
        "unresolved_input_codes": unresolved_input_codes,
        "open_conflict_ids": [],
        "source_departments_approved": True,
        "eligible": mandatory_compliance_passed and proposal_reviewable,
        "provenance": bundle_provenance,
        "validated_at": validated_at,
    }
    bundle = ApprovalValidationBundle(
        validation_bundle_id=_stable_id(approval_run_id, "bundle"),
        **bundle_values,
        bundle_hash=canonical_hash(bundle_values),
    )
    persist_corrected_draft(session, draft=draft, producer=producer)
    persist_validation_artifact(
        session, artifact=evaluation_artifact, producer=producer
    )
    persist_validation_artifact(session, artifact=preview_artifact, producer=producer)
    persist_validation_bundle(session, bundle=bundle, producer=producer)
    proposal = ProposalRevision(
        revision_id=_stable_id(approval_run_id, "revision"),
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        revision_no=1,
        source_global_draft_id=source.draft_id,
        source_global_draft_hash=draft_row["payload_hash"],
        source_global_evaluation_id=_evaluation.evaluation_id,
        source_global_evaluation_hash=evaluation_row["payload_hash"],
        currency=source.currency,
        sla=source.sla,
        volume_discounts=source.volume_discounts,
        source_department_revision_ids=source_revision_ids,
        authorized_input_ids=authorized_input_ids,
        content_hash=content_hash,
        validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        created_at=validated_at,
    )
    principal = AuthenticatedPrincipal(
        issuer=identity.issuer,
        subject=identity.subject,
        trackflow_user_id=identity.trackflow_user_id,
        auth_source=identity.auth_source,
        general_role=identity.general_role,
        synthetic=identity.synthetic,
    )
    command = InitialProposalRevisionCommand(
        creation_mode=(
            "authorized_commercial_change" if authorized_input_ids else "part2_import"
        ),
        response_run_id=handoff.run_id,
        response_handoff_hash=run["response_handoff_hash"],
        proposal=proposal,
        validation_bundle_id=bundle.validation_bundle_id,
        imported_by=(
            None
            if authorized_input_ids
            else RunInitiator(
                principal=principal,
                authorization_id=authorization.authorization_id,
                permission="manage_approval_run",
            )
        ),
        created_by=created_by,
        idempotency_key=_stable_id(approval_run_id, "idempotency"),
        requested_at=validated_at,
    )
    record: ProposalRevisionRecord = create_initial_proposal_revision(
        session,
        command=command,
        identity=identity,
        authorization_reference=proposal_authorization_reference,
        execution_id=execution_id,
    )
    return ProposalMaterializationResult(
        proposal_revision_id=record.revision_id,
        approval_run_id=record.approval_run_id,
        eligible_for_synthesis=record.eligible_for_synthesis,
        replayed=record.replayed,
    )
