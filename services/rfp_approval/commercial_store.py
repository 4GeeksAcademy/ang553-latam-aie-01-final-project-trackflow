"""Transactional store for authorized inputs and corrected RFP revisions.

Every public function participates in the caller's PostgreSQL transaction and
never commits or rolls back.  Idempotency is envelope-exact: a replay must use
the same identifiers, timestamps, hashes, actor and authorization reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    AuthorizedBusinessInput,
    DepartmentRevision,
    InitialProposalRevisionCommand,
    ImmutableArtifactReference,
    ProposalRevision,
    indicative_warnings_are_reviewable,
)
from data.pipelines.rfp_response.contracts import RFPResponseHandoff
from services.rfp_approval.authorization import (
    AuthorizationReference,
    AuthorizationTarget,
    EffectiveAuthorization,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_approval_validation_artifacts,
    rfp_approval_validation_bundles,
    rfp_authorized_business_inputs,
    rfp_workflow_events,
)
from services.rfp_approval.store import (
    ApprovalStoreError,
    _assert_identity_matches_principal,
    _authorize_after_locks,
)
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_previews,
)


@dataclass(frozen=True, slots=True)
class CommercialInputRecord:
    input_id: UUID
    approval_run_id: UUID
    payload_hash: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class DepartmentRevisionRecord:
    revision_id: UUID
    approval_run_id: UUID
    department: str
    review_revision_no: int
    status: str
    state_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProposalRevisionRecord:
    revision_id: UUID
    approval_run_id: UUID
    revision_no: int
    eligible_for_synthesis: bool
    state_version: int
    replayed: bool


def preview_artifact_payload_hash(row: dict[str, Any]) -> str:
    """Canonical hash used to bind a historical preview row to a reference."""

    return canonical_hash(
        {
            "preview_id": row["preview_id"],
            "draft_id": row["draft_id"],
            "gate_status": row["gate_status"],
            "safe_content": row["safe_content"],
            "reason_codes": row["reason_codes"],
        }
    )


def _target(department: str | None) -> AuthorizationTarget:
    if department is None:
        return AuthorizationTarget(
            permission="provide_business_input", target_policy="proposal_wide"
        )
    return AuthorizationTarget(
        permission="provide_business_input",
        department_scope=department,
        target_policy="department_exact",
        affected_departments=frozenset({department}),
    )


def _lock_active_run(
    session: Session,
    approval_run_id: UUID,
    ticket_id: UUID,
    environment: str,
) -> dict[str, Any]:
    row = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == environment,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if row is None:
        # Deliberately do not reveal whether the run exists in another
        # environment.
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    result = dict(row)
    if result["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    return result


def _authorize(
    session: Session,
    *,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
    department: str | None,
) -> EffectiveAuthorization:
    return _authorize_after_locks(
        session,
        identity=identity,
        target=_target(department),
        reference=reference,
    )


def _authorize_run_management(
    session: Session,
    *,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> EffectiveAuthorization:
    return _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(
            permission="manage_approval_run", target_policy="run"
        ),
        reference=reference,
    )


def _assert_actor(
    value: AuthorizedBusinessInput | DepartmentRevision,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> None:
    actor = value.actor if isinstance(value, AuthorizedBusinessInput) else value.created_by
    if actor is None:
        raise ApprovalStoreError("revision_creator_missing")
    _assert_identity_matches_principal(identity, actor.principal)
    if actor.authorization_id != reference.authorization_id:
        raise ApprovalStoreError("actor_authorization_reference_mismatch")


def _assert_initial_actor(
    command: InitialProposalRevisionCommand,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> None:
    actor = command.imported_by or command.created_by
    if actor is None:
        raise ApprovalStoreError("initial_proposal_actor_missing")
    _assert_identity_matches_principal(identity, actor.principal)
    if actor.authorization_id != reference.authorization_id:
        raise ApprovalStoreError("actor_authorization_reference_mismatch")


def _event_values(
    *,
    event_key: str,
    run_id: UUID,
    ticket_id: UUID,
    execution_id: UUID,
    thread_id: str,
    event_type: str,
    safe_input: dict[str, Any],
    safe_output: dict[str, Any],
    identity: ResolvedIdentity,
    authorization: EffectiveAuthorization,
    department_revision_id: UUID | None = None,
    proposal_revision_id: UUID | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": uuid4(),
        "event_key": event_key,
        "approval_run_id": run_id,
        "ticket_id": ticket_id,
        "thread_id": thread_id,
        "execution_id": execution_id,
        "sequence_no": 1,
        "node_name": "persist_authorized_commercial_change",
        "agent_name": "rfp_approval_commercial_store",
        "event_type": event_type,
        "safe_input_ref": safe_input,
        "safe_output_ref": safe_output,
        "result": "persisted",
        "from_state": from_state,
        "to_state": to_state,
        "department_revision_id": department_revision_id,
        "proposal_revision_id": proposal_revision_id,
        "actor_principal_id": identity.principal_id,
        "actor_binding_id": identity.binding_id,
        "authorization_id": authorization.authorization_id,
        "authorization_version": authorization.authorization_version,
        "environment": identity.environment,
        "started_at": func.clock_timestamp(),
        "finished_at": func.clock_timestamp(),
    }


def _insert_event(session: Session, values: dict[str, Any]) -> None:
    result = session.execute(
        pg_insert(rfp_workflow_events)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[rfp_workflow_events.c.event_key])
    )
    if result.rowcount == 1:
        return
    existing = session.execute(
        select(rfp_workflow_events).where(
            rfp_workflow_events.c.event_key == values["event_key"]
        )
    ).mappings().one_or_none()
    if existing is None:
        raise ApprovalStoreError("audit_event_insert_failed")
    ignored = {"event_id", "started_at", "finished_at"}
    if any(existing[key] != value for key, value in values.items() if key not in ignored):
        raise ApprovalStoreError("audit_event_collision")


def _input_department(value: AuthorizedBusinessInput) -> str | None:
    department = getattr(value.payload, "department", None)
    return department.value if department is not None else None


def _assert_input_replay(
    row: dict[str, Any], value: AuthorizedBusinessInput, key: UUID,
    identity: ResolvedIdentity, reference: AuthorizationReference,
) -> None:
    expected = {
        "input_id": value.input_id,
        "approval_run_id": value.approval_run_id,
        "ticket_id": value.ticket_id,
        "input_kind": value.input_kind,
        "department_scope": _input_department(value),
        "payload": canonical_value(value.payload),
        "provenance": canonical_value(value.provenance),
        "payload_hash": value.payload_hash,
        "provenance_hash": canonical_hash(value.provenance),
        "evidence_hash": value.provenance.source_payload_hash,
        "idempotency_key": key,
        "actor_principal_id": identity.principal_id,
        "actor_binding_id": identity.binding_id,
        "authorization_id": reference.authorization_id,
        "authorization_version": reference.authorization_version,
        "environment": identity.environment,
        "recorded_at": value.recorded_at,
    }
    if any(row.get(name) != item for name, item in expected.items()):
        raise ApprovalStoreError("business_input_idempotency_key_reused")


def record_authorized_business_input(
    session: Session,
    *,
    business_input: AuthorizedBusinessInput,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    idempotency_key: UUID,
    execution_id: UUID,
) -> CommercialInputRecord:
    """Persist one immutable, authorized commercial fact."""

    _assert_actor(business_input, identity, authorization_reference)
    _lock_active_run(
        session,
        business_input.approval_run_id,
        business_input.ticket_id,
        identity.environment,
    )
    department = _input_department(business_input)
    existing = session.execute(
        select(rfp_authorized_business_inputs).where(
            rfp_authorized_business_inputs.c.approval_run_id
            == business_input.approval_run_id,
            rfp_authorized_business_inputs.c.idempotency_key == idempotency_key,
        )
    ).mappings().one_or_none()
    authorization = _authorize(
        session,
        identity=identity,
        reference=authorization_reference,
        department=department,
    )
    if existing is not None:
        row = dict(existing)
        _assert_input_replay(
            row, business_input, idempotency_key, identity, authorization_reference
        )
        return CommercialInputRecord(
            row["input_id"], row["approval_run_id"], row["payload_hash"], True
        )

    values = {
        "input_id": business_input.input_id,
        "approval_run_id": business_input.approval_run_id,
        "ticket_id": business_input.ticket_id,
        "input_kind": business_input.input_kind,
        "department_scope": department,
        "payload": canonical_value(business_input.payload),
        "provenance": canonical_value(business_input.provenance),
        "payload_hash": business_input.payload_hash,
        "provenance_hash": canonical_hash(business_input.provenance),
        "evidence_hash": business_input.provenance.source_payload_hash,
        "idempotency_key": idempotency_key,
        "actor_principal_id": identity.principal_id,
        "actor_binding_id": identity.binding_id,
        "authorization_id": authorization.authorization_id,
        "authorization_version": authorization.authorization_version,
        "environment": identity.environment,
        "recorded_at": business_input.recorded_at,
    }
    inserted = session.execute(
        pg_insert(rfp_authorized_business_inputs)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[
                rfp_authorized_business_inputs.c.approval_run_id,
                rfp_authorized_business_inputs.c.idempotency_key,
            ]
        )
        .returning(rfp_authorized_business_inputs),
    ).mappings().one_or_none()
    if inserted is None:
        concurrent = session.execute(
            select(rfp_authorized_business_inputs).where(
                rfp_authorized_business_inputs.c.approval_run_id
                == business_input.approval_run_id,
                rfp_authorized_business_inputs.c.idempotency_key == idempotency_key,
            )
        ).mappings().one()
        row = dict(concurrent)
        _assert_input_replay(
            row, business_input, idempotency_key, identity, authorization_reference
        )
        return CommercialInputRecord(
            row["input_id"], row["approval_run_id"], row["payload_hash"], True
        )
    row = dict(inserted)
    _insert_event(
        session,
        _event_values(
            event_key=f"business-input-recorded:{business_input.approval_run_id}:{idempotency_key}",
            run_id=business_input.approval_run_id,
            ticket_id=business_input.ticket_id,
            execution_id=execution_id,
            thread_id=f"business-input:{business_input.input_id}",
            event_type="authorized_business_input_recorded",
            safe_input={
                "input_id": str(business_input.input_id),
                "input_kind": business_input.input_kind,
                "payload_hash": business_input.payload_hash,
                "idempotency_key": str(idempotency_key),
            },
            safe_output={"input_id": str(business_input.input_id), "stored": True},
            identity=identity,
            authorization=authorization,
        ),
    )
    return CommercialInputRecord(
        row["input_id"], row["approval_run_id"], row["payload_hash"], False
    )


def _verify_artifact(
    session: Session,
    *,
    reference: ImmutableArtifactReference,
    response_run_id: UUID,
    scope: str,
    content_hash: str,
) -> None:
    if reference.evaluated_content_hash != content_hash:
        raise ApprovalStoreError("artifact_targets_stale_content")
    if reference.artifact_type in {"evaluation", "global_evaluation"}:
        row = session.execute(
            select(rfp_response_evaluations).where(
                rfp_response_evaluations.c.evaluation_id == reference.artifact_id,
                rfp_response_evaluations.c.run_id == response_run_id,
                rfp_response_evaluations.c.scope_key == scope,
            )
        ).mappings().one_or_none()
        if row is None:
            raise ApprovalStoreError("evaluation_artifact_not_found")
        if (
            row["contract_version"] != reference.contract_version
            or row["payload_hash"] != reference.payload_hash
            or row["payload"].get("evaluated_content_hash") != content_hash
        ):
            raise ApprovalStoreError("evaluation_artifact_mismatch")
        return
    row = session.execute(
        select(
            rfp_response_previews,
            rfp_response_drafts.c.run_id,
            rfp_response_drafts.c.scope_key,
        )
        .join(
            rfp_response_drafts,
            rfp_response_drafts.c.draft_id == rfp_response_previews.c.draft_id,
        )
        .where(rfp_response_previews.c.preview_id == reference.artifact_id)
    ).mappings().one_or_none()
    if row is None:
        raise ApprovalStoreError("preview_artifact_not_found")
    preview = dict(row)
    if (
        preview["run_id"] != response_run_id
        or preview["scope_key"] != scope
        or preview["gate_status"] != "passed"
        or preview_artifact_payload_hash(preview) != reference.payload_hash
    ):
        raise ApprovalStoreError("preview_artifact_mismatch")


def _verify_authorized_input_evidence(
    session: Session,
    *,
    revision: DepartmentRevision,
) -> None:
    references: dict[UUID, str] = {}
    try:
        for item in revision.evidence:
            if item.document_id == "authorized-business-input-v1":
                references[UUID(item.section_id)] = item.evidence_hash
    except ValueError as exc:
        raise ApprovalStoreError("authorized_input_evidence_id_invalid") from exc
    if revision.creation_reason == "authorized_input" and not references:
        raise ApprovalStoreError("authorized_input_evidence_missing")
    if not references:
        return
    rows = session.execute(
        select(
            rfp_authorized_business_inputs.c.input_id,
            rfp_authorized_business_inputs.c.payload_hash,
        ).where(
            rfp_authorized_business_inputs.c.input_id.in_(references),
            rfp_authorized_business_inputs.c.approval_run_id
            == revision.approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == revision.ticket_id,
        )
    ).mappings().all()
    persisted = {row["input_id"]: row["payload_hash"] for row in rows}
    if persisted != references:
        raise ApprovalStoreError("authorized_input_evidence_mismatch")


def _assert_exact_bundle_artifact(
    row: dict[str, Any] | None,
    *,
    artifact_id: UUID,
    artifact_type: str,
    run_id: UUID,
    ticket_id: UUID,
    scope: str,
    draft_id: UUID,
    content_hash: str,
    contract_version: str,
    payload_hash: str,
) -> dict[str, Any]:
    if row is None:
        raise ApprovalStoreError("validation_bundle_artifact_missing")
    expected = {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "approval_run_id": run_id,
        "ticket_id": ticket_id,
        "scope_key": scope,
        "corrected_draft_id": draft_id,
        "evaluated_content_hash": content_hash,
        "contract_version": contract_version,
        "payload_hash": payload_hash,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise ApprovalStoreError("validation_bundle_artifact_mismatch")
    if canonical_hash(row["payload"]) != row["payload_hash"]:
        raise ApprovalStoreError("persisted_validation_artifact_hash_mismatch")
    return row


def _lock_validation_bundle(
    session: Session,
    *,
    revision: DepartmentRevision | ProposalRevision,
    scope: str,
    required_draft_source_kind: str = "human_correction",
) -> dict[str, Any]:
    """Resolve an immutable S1 bundle and bind it to the supplied revision."""

    if revision.validation_bundle_id is None:
        raise ApprovalStoreError("validation_bundle_required")
    bundle_row = session.execute(
        select(rfp_approval_validation_bundles)
        .where(
            rfp_approval_validation_bundles.c.validation_bundle_id
            == revision.validation_bundle_id,
            rfp_approval_validation_bundles.c.approval_run_id
            == revision.approval_run_id,
            rfp_approval_validation_bundles.c.ticket_id == revision.ticket_id,
            rfp_approval_validation_bundles.c.scope_key == scope,
            rfp_approval_validation_bundles.c.content_hash == revision.content_hash,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if bundle_row is None:
        raise ApprovalStoreError("validation_bundle_not_found_or_mismatch")
    bundle = dict(bundle_row)

    draft_row = session.execute(
        select(rfp_approval_corrected_drafts)
        .where(
            rfp_approval_corrected_drafts.c.draft_artifact_id
            == bundle["corrected_draft_id"],
            rfp_approval_corrected_drafts.c.approval_run_id
            == revision.approval_run_id,
            rfp_approval_corrected_drafts.c.ticket_id == revision.ticket_id,
            rfp_approval_corrected_drafts.c.scope_key == scope,
            rfp_approval_corrected_drafts.c.content_hash == revision.content_hash,
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if draft_row is None:
        raise ApprovalStoreError("validation_bundle_draft_mismatch")
    draft = dict(draft_row)
    try:
        provenance = ApprovalArtifactProvenance.model_validate_json(
            json.dumps(draft["provenance"])
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ApprovalStoreError("corrected_draft_provenance_invalid") from error
    if (
        provenance.source_kind != required_draft_source_kind
        or provenance.provenance_hash != draft["provenance_hash"]
    ):
        raise ApprovalStoreError("corrected_draft_provenance_mismatch")
    if isinstance(revision, DepartmentRevision):
        expected_payload = canonical_value(
            {
                "payload_kind": "department_corrected_draft",
                "department": revision.department,
                "content": revision.content,
                "commercial_terms": revision.commercial_terms,
                "pending_commercial_items": revision.pending_commercial_items,
                "evidence": revision.evidence,
            }
        )
        validation = revision.validation
        expected_evaluation_type = "evaluation"
        evaluation_reference = validation.references.evaluation
        preview_reference = validation.references.safe_preview
        validation_hash = validation.references.validation_hash
        source_departments_approved = False
        eligible = validation.eligible_for_approval
        validated_at = validation.references.validated_at
        expected_sources = {
            (UUID(item.section_id), item.evidence_hash)
            for item in revision.evidence
            if item.document_id == "authorized-business-input-v1"
        }
    else:
        expected_payload = canonical_value(
            {
                "payload_kind": "proposal_corrected_draft",
                "currency": revision.currency,
                "sla": revision.sla,
                "volume_discounts": revision.volume_discounts,
                "source_department_revision_ids": (
                    revision.source_department_revision_ids
                ),
                "authorized_input_ids": revision.authorized_input_ids,
            }
        )
        validation = revision.validation
        expected_evaluation_type = "global_evaluation"
        evaluation_reference = validation.global_evaluation
        preview_reference = validation.safe_preview
        validation_hash = validation.validation_hash
        source_departments_approved = validation.source_departments_approved
        eligible = validation.eligible_for_synthesis
        validated_at = validation.validated_at
        if required_draft_source_kind == "part2_import":
            expected_sources = {
                (revision.source_global_draft_id, revision.source_global_draft_hash)
            }
        else:
            input_rows = session.execute(
                select(
                    rfp_authorized_business_inputs.c.input_id,
                    rfp_authorized_business_inputs.c.payload_hash,
                ).where(
                    rfp_authorized_business_inputs.c.input_id.in_(
                        revision.authorized_input_ids
                    ),
                    rfp_authorized_business_inputs.c.approval_run_id
                    == revision.approval_run_id,
                    rfp_authorized_business_inputs.c.ticket_id == revision.ticket_id,
                )
            ).mappings().all()
            expected_sources = {
                (revision.source_global_draft_id, revision.source_global_draft_hash),
                *((row["input_id"], row["payload_hash"]) for row in input_rows),
            }
            if len(input_rows) != len(revision.authorized_input_ids):
                raise ApprovalStoreError("corrected_draft_provenance_source_not_found")
    persisted_sources = set(
        zip(provenance.source_artifact_ids, provenance.source_hashes, strict=True)
    )
    if persisted_sources != expected_sources:
        raise ApprovalStoreError("corrected_draft_provenance_sources_mismatch")
    if (
        draft["content_payload"] != expected_payload
        or canonical_hash(draft["content_payload"]) != draft["payload_hash"]
    ):
        raise ApprovalStoreError("corrected_draft_revision_mismatch")

    artifact_rows = session.execute(
        select(rfp_approval_validation_artifacts)
        .where(
            rfp_approval_validation_artifacts.c.artifact_id.in_(
                {
                    bundle["evaluation_artifact_id"],
                    bundle["safe_preview_artifact_id"],
                }
            )
        )
        .order_by(rfp_approval_validation_artifacts.c.artifact_id)
        .with_for_update(key_share=True)
    ).mappings().all()
    by_id = {row["artifact_id"]: dict(row) for row in artifact_rows}
    evaluation = _assert_exact_bundle_artifact(
        by_id.get(bundle["evaluation_artifact_id"]),
        artifact_id=evaluation_reference.artifact_id,
        artifact_type=expected_evaluation_type,
        run_id=revision.approval_run_id,
        ticket_id=revision.ticket_id,
        scope=scope,
        draft_id=bundle["corrected_draft_id"],
        content_hash=revision.content_hash,
        contract_version=evaluation_reference.contract_version,
        payload_hash=evaluation_reference.payload_hash,
    )
    preview = _assert_exact_bundle_artifact(
        by_id.get(bundle["safe_preview_artifact_id"]),
        artifact_id=preview_reference.artifact_id,
        artifact_type="safe_preview",
        run_id=revision.approval_run_id,
        ticket_id=revision.ticket_id,
        scope=scope,
        draft_id=bundle["corrected_draft_id"],
        content_hash=revision.content_hash,
        contract_version=preview_reference.contract_version,
        payload_hash=preview_reference.payload_hash,
    )
    summary = {
        "mandatory_compliance_passed": validation.mandatory_compliance_passed,
        "blocking_rule_ids": canonical_value(validation.blocking_rule_ids),
        "unresolved_input_codes": canonical_value(validation.unresolved_input_codes),
        "open_conflict_ids": canonical_value(validation.open_conflict_ids),
    }
    if isinstance(revision, DepartmentRevision):
        summary["blocks_commercial_completeness"] = (
            validation.blocks_commercial_completeness
        )
    else:
        # Proposal V2 keeps the warning codes in its validation payload; the
        # bundle records whether the approved document remains indicative.
        summary["blocks_commercial_completeness"] = bool(
            validation.unresolved_input_codes
        )
    if any(bundle[key] != value for key, value in summary.items()):
        raise ApprovalStoreError("validation_bundle_summary_mismatch")
    if (
        bundle["revision_validation_hash"] != validation_hash
        or bundle["source_departments_approved"] != source_departments_approved
        or bundle["eligible"] != eligible
        or bundle["validated_at"] != validated_at
    ):
        raise ApprovalStoreError("validation_bundle_revision_mismatch")
    if canonical_value(evaluation["payload"]) != canonical_value(
        {
            "payload_kind": "content_evaluation",
            **summary,
            "eligible": (
                validation.mandatory_compliance_passed
                and indicative_warnings_are_reviewable(
                    blocking_rule_ids=validation.blocking_rule_ids,
                    unresolved_input_codes=validation.unresolved_input_codes,
                )
                and not validation.open_conflict_ids
            ),
        }
    ):
        raise ApprovalStoreError("validation_artifact_summary_mismatch")
    if eligible and preview["payload"].get("gate_status") != "passed":
        raise ApprovalStoreError("eligible_bundle_preview_not_passed")
    return bundle


def _derived_department_status(revision: DepartmentRevision) -> str:
    validation = revision.validation
    if validation.eligible_for_approval:
        return "pending_approval"
    if revision.pending_commercial_items:
        return "blocked_input"
    if validation.blocks_commercial_completeness or validation.unresolved_input_codes:
        raise ApprovalStoreError("input_blocker_without_pending_item")
    if not validation.mandatory_compliance_passed or validation.blocking_rule_ids:
        return "blocked_compliance"
    if validation.open_conflict_ids:
        return "blocked_conflict"
    if revision.review_revision_no == 3:
        return "limit_reached"
    raise ApprovalStoreError("unsupported_revision_blocker_state")


def _derived_proposal_eligibility(revision: ProposalRevision) -> bool:
    """Global blockers govern synthesis after source approval is revalidated."""

    return revision.validation.eligible_for_synthesis


def _revision_event_replay(
    session: Session, event_key: str, request_hash: str
) -> dict[str, Any] | None:
    row = session.execute(
        select(rfp_workflow_events).where(rfp_workflow_events.c.event_key == event_key)
    ).mappings().one_or_none()
    if row is None:
        return None
    if row["safe_input_ref"].get("request_hash") != request_hash:
        raise ApprovalStoreError("revision_idempotency_key_reused")
    return dict(row)


def _verify_part2_global_source(
    session: Session,
    *,
    revision: ProposalRevision,
    response_run_id: UUID,
    require_same_terms: bool,
) -> None:
    draft = session.execute(
        select(rfp_response_drafts).where(
            rfp_response_drafts.c.draft_id == revision.source_global_draft_id,
            rfp_response_drafts.c.run_id == response_run_id,
            rfp_response_drafts.c.scope_key == "proposal",
        )
    ).mappings().one_or_none()
    if draft is None or draft["payload_hash"] != revision.source_global_draft_hash:
        raise ApprovalStoreError("source_global_draft_mismatch")
    if canonical_hash(draft["payload"]) != draft["payload_hash"]:
        raise ApprovalStoreError("source_global_draft_hash_invalid")
    if require_same_terms:
        source_terms = {
            "currency": draft["payload"].get("currency"),
            "sla": draft["payload"].get("sla"),
            "volume_discounts": draft["payload"].get("volume_discounts"),
        }
        imported_terms = canonical_value(
            {
                "currency": revision.currency,
                "sla": revision.sla,
                "volume_discounts": revision.volume_discounts,
            }
        )
        if source_terms != imported_terms:
            raise ApprovalStoreError("part2_import_terms_changed")
    evaluation = session.execute(
        select(rfp_response_evaluations).where(
            rfp_response_evaluations.c.evaluation_id
            == revision.source_global_evaluation_id,
            rfp_response_evaluations.c.run_id == response_run_id,
            rfp_response_evaluations.c.draft_id == revision.source_global_draft_id,
            rfp_response_evaluations.c.scope_key == "proposal",
        )
    ).mappings().one_or_none()
    if (
        evaluation is None
        or evaluation["payload_hash"] != revision.source_global_evaluation_hash
    ):
        raise ApprovalStoreError("source_global_evaluation_mismatch")
    if canonical_hash(evaluation["payload"]) != evaluation["payload_hash"]:
        raise ApprovalStoreError("source_global_evaluation_hash_invalid")


def _validated_handoff(
    handoff: dict[str, Any], command: InitialProposalRevisionCommand
) -> RFPResponseHandoff:
    try:
        value = RFPResponseHandoff.model_validate_json(json.dumps(handoff["payload"]))
    except (KeyError, ValueError, TypeError) as error:
        raise ApprovalStoreError("response_handoff_payload_invalid") from error
    if (
        value.run_id != command.response_run_id
        or value.ticket_id != command.proposal.ticket_id
        or value.global_draft_id != command.proposal.source_global_draft_id
        or value.global_evaluation_result_id
        != command.proposal.source_global_evaluation_id
    ):
        raise ApprovalStoreError("response_handoff_global_source_mismatch")
    return value


def _assert_revision_replay_actor(
    event: dict[str, Any],
    *,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> None:
    if (
        event["actor_principal_id"] != identity.principal_id
        or event["actor_binding_id"] != identity.binding_id
        or event["environment"] != identity.environment
    ):
        raise ApprovalStoreError("revision_replay_actor_mismatch")
    if (
        event["authorization_id"] != reference.authorization_id
        or event["authorization_version"] != reference.authorization_version
    ):
        raise ApprovalStoreError("revision_replay_authorization_mismatch")


def create_corrected_department_revision(
    session: Session,
    *,
    revision: DepartmentRevision,
    expected_predecessor_status: str,
    expected_predecessor_content_hash: str,
    expected_predecessor_state_version: int,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    idempotency_key: UUID,
    execution_id: UUID,
) -> DepartmentRevisionRecord:
    """Atomically supersede A with corrected department revision B."""

    _assert_actor(revision, identity, authorization_reference)
    if revision.review_revision_no <= 1 or revision.supersedes_revision_id is None:
        raise ApprovalStoreError("corrected_revision_requires_predecessor")
    if revision.status != _derived_department_status(revision):
        raise ApprovalStoreError("revision_status_not_derived")
    run = _lock_active_run(
        session,
        revision.approval_run_id,
        revision.ticket_id,
        identity.environment,
    )
    request_hash = canonical_hash(
        {
            "revision": revision,
            "expected_status": expected_predecessor_status,
            "expected_content_hash": expected_predecessor_content_hash,
            "expected_state_version": expected_predecessor_state_version,
            "idempotency_key": idempotency_key,
        }
    )
    event_key = f"department-revision-created:{revision.approval_run_id}:{idempotency_key}"
    replay = _revision_event_replay(session, event_key, request_hash)
    if replay is not None:
        _assert_revision_replay_actor(
            replay,
            identity=identity,
            reference=authorization_reference,
        )
        row = session.execute(
            select(rfp_approval_department_revisions)
            .where(
                rfp_approval_department_revisions.c.revision_id
                == revision.revision_id
            )
            .with_for_update(key_share=True)
        ).mappings().one()
        _authorize(
            session,
            identity=identity,
            reference=authorization_reference,
            department=revision.department.value,
        )
        return DepartmentRevisionRecord(
            row["revision_id"], row["approval_run_id"], row["department_id"],
            row["review_revision_no"], row["status"], row["state_version"], True,
        )
    predecessor = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id
            == revision.supersedes_revision_id,
            rfp_approval_department_revisions.c.approval_run_id
            == revision.approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == revision.ticket_id,
            rfp_approval_department_revisions.c.department_id
            == revision.department.value,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if predecessor is None:
        raise ApprovalStoreError("predecessor_revision_not_found")
    predecessor = dict(predecessor)
    authorization = _authorize(
        session,
        identity=identity,
        reference=authorization_reference,
        department=revision.department.value,
    )
    if (
        not predecessor["is_current"]
        or predecessor["status"] != expected_predecessor_status
        or predecessor["content_hash"] != expected_predecessor_content_hash
        or predecessor["state_version"] != expected_predecessor_state_version
    ):
        later = _revision_event_replay(session, event_key, request_hash)
        if later is not None:
            _assert_revision_replay_actor(
                later,
                identity=identity,
                reference=authorization_reference,
            )
            row = session.execute(
                select(rfp_approval_department_revisions)
                .where(
                    rfp_approval_department_revisions.c.revision_id
                    == revision.revision_id
                )
                .with_for_update(key_share=True)
            ).mappings().one()
            return DepartmentRevisionRecord(
                row["revision_id"], row["approval_run_id"], row["department_id"],
                row["review_revision_no"], row["status"], row["state_version"], True,
            )
        raise ApprovalStoreError("predecessor_revision_stale")
    if revision.review_revision_no != predecessor["review_revision_no"] + 1:
        raise ApprovalStoreError("revision_number_not_sequential")
    heritage = (
        "source_part2_draft_id", "source_part2_draft_hash",
        "source_part2_evaluation_id", "source_part2_evaluation_hash",
        "source_part2_iteration",
    )
    supplied = (
        revision.source_part2_draft_id, revision.source_part2_draft_hash,
        revision.source_part2_evaluation_id, revision.source_part2_evaluation_hash,
        revision.source_part2_iteration,
    )
    if tuple(predecessor[name] for name in heritage) != supplied:
        raise ApprovalStoreError("part2_heritage_changed")
    _verify_authorized_input_evidence(session, revision=revision)
    _lock_validation_bundle(
        session, revision=revision, scope=revision.department.value
    )
    changed = session.execute(
        update(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id == predecessor["revision_id"],
            rfp_approval_department_revisions.c.is_current.is_(True),
            rfp_approval_department_revisions.c.status == expected_predecessor_status,
            rfp_approval_department_revisions.c.content_hash
            == expected_predecessor_content_hash,
            rfp_approval_department_revisions.c.state_version
            == expected_predecessor_state_version,
        )
        .values(
            status="superseded",
            is_current=False,
            superseded_by_revision_id=revision.revision_id,
            state_version=rfp_approval_department_revisions.c.state_version + 1,
        )
    ).rowcount
    if changed != 1:
        raise ApprovalStoreError("predecessor_revision_cas_failed")
    payload = {
        "draft_id": str(revision.draft_id),
        "content": revision.content,
        "commercial_terms": canonical_value(revision.commercial_terms),
        "pending_commercial_items": canonical_value(revision.pending_commercial_items),
        "evidence": canonical_value(revision.evidence),
        "creation_reason": revision.creation_reason,
    }
    session.execute(
        insert(rfp_approval_department_revisions).values(
            revision_id=revision.revision_id,
            approval_run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            response_run_id=run["response_run_id"],
            department_id=revision.department.value,
            review_revision_no=revision.review_revision_no,
            source_part2_draft_id=revision.source_part2_draft_id,
            source_part2_draft_hash=revision.source_part2_draft_hash,
            source_part2_evaluation_id=revision.source_part2_evaluation_id,
            source_part2_evaluation_hash=revision.source_part2_evaluation_hash,
            source_part2_iteration=revision.source_part2_iteration,
            supersedes_revision_id=revision.supersedes_revision_id,
            content_payload=payload,
            content_hash=revision.content_hash,
            validation_payload=canonical_value(revision.validation),
            validation_hash=revision.validation.references.validation_hash,
            evaluated_content_hash=revision.content_hash,
            validation_bundle_id=revision.validation_bundle_id,
            status=revision.status,
            is_current=True,
            state_version=0,
            created_by_principal_id=identity.principal_id,
            created_at=revision.created_at,
        )
    )
    _insert_event(
        session,
        _event_values(
            event_key=event_key,
            run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            execution_id=execution_id,
            thread_id=f"department-revision:{revision.revision_id}",
            event_type="department_revision_corrected",
            safe_input={
                "request_hash": request_hash,
                "predecessor_revision_id": str(predecessor["revision_id"]),
                "expected_state_version": expected_predecessor_state_version,
                "content_hash": revision.content_hash,
                "validation_bundle_id": str(revision.validation_bundle_id),
            },
            safe_output={
                "revision_id": str(revision.revision_id),
                "review_revision_no": revision.review_revision_no,
                "status": revision.status,
            },
            identity=identity,
            authorization=authorization,
            department_revision_id=revision.revision_id,
            from_state=expected_predecessor_status,
            to_state=revision.status,
        ),
    )
    return DepartmentRevisionRecord(
        revision.revision_id, revision.approval_run_id, revision.department.value,
        revision.review_revision_no, revision.status, 0, False,
    )


def create_corrected_proposal_revision(
    session: Session,
    *,
    revision: ProposalRevision,
    expected_predecessor_content_hash: str,
    expected_predecessor_state_version: int,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    idempotency_key: UUID,
    execution_id: UUID,
) -> ProposalRevisionRecord:
    """Atomically supersede a proposal while rechecking all referenced facts."""

    if revision.revision_no <= 1 or revision.supersedes_revision_id is None:
        raise ApprovalStoreError("corrected_proposal_requires_predecessor")
    run = _lock_active_run(
        session,
        revision.approval_run_id,
        revision.ticket_id,
        identity.environment,
    )
    if (
        revision.validation.contract_version
        != "rfp-proposal-revision-validation-v2"
    ):
        raise ApprovalStoreError("proposal_validation_v2_required")
    request_hash = canonical_hash(
        {
            "revision": revision,
            "expected_content_hash": expected_predecessor_content_hash,
            "expected_state_version": expected_predecessor_state_version,
            "idempotency_key": idempotency_key,
        }
    )
    event_key = f"proposal-revision-created:{revision.approval_run_id}:{idempotency_key}"
    replay = _revision_event_replay(session, event_key, request_hash)
    if replay is not None:
        _assert_revision_replay_actor(
            replay,
            identity=identity,
            reference=authorization_reference,
        )
        row = session.execute(
            select(rfp_approval_proposal_revisions)
            .where(
                rfp_approval_proposal_revisions.c.proposal_revision_id
                == revision.revision_id
            )
            .with_for_update(key_share=True)
        ).mappings().one()
        _authorize(
            session,
            identity=identity,
            reference=authorization_reference,
            department=None,
        )
        return ProposalRevisionRecord(
            row["proposal_revision_id"], row["approval_run_id"], row["revision_no"],
            row["eligible_for_synthesis"], row["state_version"], True,
        )
    predecessor = session.execute(
        select(rfp_approval_proposal_revisions)
        .where(
            rfp_approval_proposal_revisions.c.proposal_revision_id
            == revision.supersedes_revision_id,
            rfp_approval_proposal_revisions.c.approval_run_id
            == revision.approval_run_id,
            rfp_approval_proposal_revisions.c.ticket_id == revision.ticket_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if predecessor is None:
        raise ApprovalStoreError("predecessor_proposal_not_found")
    predecessor = dict(predecessor)
    if (
        not predecessor["is_current"]
        or predecessor["content_hash"] != expected_predecessor_content_hash
        or predecessor["state_version"] != expected_predecessor_state_version
    ):
        later = _revision_event_replay(session, event_key, request_hash)
        if later is not None:
            _assert_revision_replay_actor(
                later,
                identity=identity,
                reference=authorization_reference,
            )
            row = session.execute(
                select(rfp_approval_proposal_revisions)
                .where(
                    rfp_approval_proposal_revisions.c.proposal_revision_id
                    == revision.revision_id
                )
                .with_for_update(key_share=True)
            ).mappings().one()
            _authorize(
                session,
                identity=identity,
                reference=authorization_reference,
                department=None,
            )
            return ProposalRevisionRecord(
                row["proposal_revision_id"],
                row["approval_run_id"],
                row["revision_no"],
                row["eligible_for_synthesis"],
                row["state_version"],
                True,
            )
        raise ApprovalStoreError("predecessor_proposal_stale")
    if revision.revision_no != predecessor["revision_no"] + 1:
        raise ApprovalStoreError("proposal_revision_number_not_sequential")
    heritage = (
        predecessor["source_global_draft_id"], predecessor["source_global_draft_hash"],
        predecessor["source_global_evaluation_id"],
        predecessor["source_global_evaluation_hash"],
    )
    if heritage != (
        revision.source_global_draft_id, revision.source_global_draft_hash,
        revision.source_global_evaluation_id, revision.source_global_evaluation_hash,
    ):
        raise ApprovalStoreError("global_part2_heritage_changed")
    authorization = _authorize(
        session,
        identity=identity,
        reference=authorization_reference,
        department=None,
    )
    _lock_validation_bundle(session, revision=revision, scope="proposal")
    departments = session.execute(
        select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id.in_(
                revision.source_department_revision_ids
            )
        ).with_for_update(key_share=True)
    ).mappings().all()
    if len(departments) != len(revision.source_department_revision_ids):
        raise ApprovalStoreError("source_department_revision_not_found")
    department_map = {row["revision_id"]: dict(row) for row in departments}
    for revision_id in revision.source_department_revision_ids:
        row = department_map[revision_id]
        if (
            row["approval_run_id"] != revision.approval_run_id
            or row["ticket_id"] != revision.ticket_id
            or not row["is_current"]
            or row["status"] != "approved"
        ):
            raise ApprovalStoreError("source_department_revision_not_eligible")
    if {row["department_id"] for row in department_map.values()} != set(
        run["active_departments"]
    ):
        raise ApprovalStoreError("proposal_department_scope_incomplete")
    inputs = session.execute(
        select(rfp_authorized_business_inputs.c.input_id).where(
            rfp_authorized_business_inputs.c.input_id.in_(revision.authorized_input_ids),
            rfp_authorized_business_inputs.c.approval_run_id == revision.approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == revision.ticket_id,
        )
    ).scalars().all()
    if set(inputs) != set(revision.authorized_input_ids):
        raise ApprovalStoreError("authorized_input_reference_mismatch")
    eligible = _derived_proposal_eligibility(revision)
    changed = session.execute(
        update(rfp_approval_proposal_revisions)
        .where(
            rfp_approval_proposal_revisions.c.proposal_revision_id
            == predecessor["proposal_revision_id"],
            rfp_approval_proposal_revisions.c.is_current.is_(True),
            rfp_approval_proposal_revisions.c.content_hash
            == expected_predecessor_content_hash,
            rfp_approval_proposal_revisions.c.state_version
            == expected_predecessor_state_version,
        )
        .values(
            is_current=False,
            superseded_by_revision_id=revision.revision_id,
            state_version=rfp_approval_proposal_revisions.c.state_version + 1,
        )
    ).rowcount
    if changed != 1:
        raise ApprovalStoreError("predecessor_proposal_cas_failed")
    by_department = {row["department_id"]: row["revision_id"] for row in department_map.values()}
    session.execute(
        insert(rfp_approval_proposal_revisions).values(
            proposal_revision_id=revision.revision_id,
            approval_run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            response_run_id=run["response_run_id"],
            revision_no=revision.revision_no,
            source_global_draft_id=revision.source_global_draft_id,
            source_global_draft_hash=revision.source_global_draft_hash,
            source_global_evaluation_id=revision.source_global_evaluation_id,
            source_global_evaluation_hash=revision.source_global_evaluation_hash,
            supersedes_revision_id=revision.supersedes_revision_id,
            warehouse_revision_id=by_department.get("warehouse"),
            lastmile_revision_id=by_department.get("lastmile"),
            reverse_revision_id=by_department.get("reverse"),
            content_payload={
                "currency": canonical_value(revision.currency),
                "sla": canonical_value(revision.sla),
                "volume_discounts": canonical_value(revision.volume_discounts),
                "source_department_revision_ids": [
                    str(item) for item in revision.source_department_revision_ids
                ],
                "authorized_input_ids": [str(item) for item in revision.authorized_input_ids],
            },
            content_hash=revision.content_hash,
            validation_payload=canonical_value(revision.validation),
            validation_hash=revision.validation.validation_hash,
            evaluated_content_hash=revision.content_hash,
            validation_bundle_id=revision.validation_bundle_id,
            eligible_for_synthesis=eligible,
            is_current=True,
            state_version=0,
            created_at=revision.created_at,
        )
    )
    _insert_event(
        session,
        _event_values(
            event_key=event_key,
            run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            execution_id=execution_id,
            thread_id=f"proposal-revision:{revision.revision_id}",
            event_type="proposal_revision_corrected",
            safe_input={
                "request_hash": request_hash,
                "predecessor_revision_id": str(predecessor["proposal_revision_id"]),
                "expected_state_version": expected_predecessor_state_version,
                "content_hash": revision.content_hash,
                "validation_bundle_id": str(revision.validation_bundle_id),
            },
            safe_output={
                "proposal_revision_id": str(revision.revision_id),
                "revision_no": revision.revision_no,
                "eligible_for_synthesis": eligible,
            },
            identity=identity,
            authorization=authorization,
            proposal_revision_id=revision.revision_id,
        ),
    )
    return ProposalRevisionRecord(
        revision.revision_id, revision.approval_run_id, revision.revision_no,
        eligible, 0, False,
    )


def create_initial_proposal_revision(
    session: Session,
    *,
    command: InitialProposalRevisionCommand,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    execution_id: UUID,
) -> ProposalRevisionRecord:
    """Create proposal revision one from a verified handoff and S1 bundle.

    The approval-run row is locked exclusively so concurrent first-revision
    commands serialize before the partial unique-current constraint is reached.
    The caller owns the surrounding transaction.
    """

    _assert_initial_actor(command, identity, authorization_reference)
    revision = command.proposal
    run_row = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == revision.approval_run_id,
            rfp_approval_runs.c.ticket_id == revision.ticket_id,
            rfp_approval_runs.c.environment == identity.environment,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run_row is None:
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    run = dict(run_row)
    if run["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    if (
        run["response_run_id"] != command.response_run_id
        or run["response_handoff_hash"] != command.response_handoff_hash
    ):
        raise ApprovalStoreError("response_handoff_mismatch")

    request_hash = canonical_hash(command)
    event_key = (
        f"proposal-initial-created:{revision.approval_run_id}:"
        f"{command.idempotency_key}"
    )
    replay = _revision_event_replay(session, event_key, request_hash)
    if replay is not None:
        _assert_revision_replay_actor(
            replay, identity=identity, reference=authorization_reference
        )
        row = session.execute(
            select(rfp_approval_proposal_revisions)
            .where(
                rfp_approval_proposal_revisions.c.proposal_revision_id
                == revision.revision_id
            )
            .with_for_update(key_share=True)
        ).mappings().one()
        if command.creation_mode == "part2_import":
            _authorize_run_management(
                session, identity=identity, reference=authorization_reference
            )
        else:
            _authorize(
                session,
                identity=identity,
                reference=authorization_reference,
                department=None,
            )
        return ProposalRevisionRecord(
            row["proposal_revision_id"], row["approval_run_id"],
            row["revision_no"], row["eligible_for_synthesis"],
            row["state_version"], True,
        )

    current = session.execute(
        select(rfp_approval_proposal_revisions.c.proposal_revision_id).where(
            rfp_approval_proposal_revisions.c.approval_run_id
            == revision.approval_run_id,
            rfp_approval_proposal_revisions.c.ticket_id == revision.ticket_id,
            rfp_approval_proposal_revisions.c.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if current is not None:
        raise ApprovalStoreError("initial_proposal_already_exists")

    handoff = session.execute(
        select(rfp_response_handoffs).where(
            rfp_response_handoffs.c.run_id == command.response_run_id,
            rfp_response_handoffs.c.ticket_id == revision.ticket_id,
            rfp_response_handoffs.c.payload_hash == command.response_handoff_hash,
        )
    ).mappings().one_or_none()
    if handoff is None:
        raise ApprovalStoreError("response_handoff_not_found")
    if canonical_hash(handoff["payload"]) != command.response_handoff_hash:
        raise ApprovalStoreError("response_handoff_hash_invalid")
    _validated_handoff(dict(handoff), command)
    _verify_part2_global_source(
        session,
        revision=revision,
        response_run_id=command.response_run_id,
        require_same_terms=command.creation_mode == "part2_import",
    )

    if command.creation_mode == "part2_import":
        authorization = _authorize_run_management(
            session, identity=identity, reference=authorization_reference
        )
    else:
        authorization = _authorize(
            session,
            identity=identity,
            reference=authorization_reference,
            department=None,
        )

    departments = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id.in_(
                revision.source_department_revision_ids
            )
        )
        .with_for_update(key_share=True)
    ).mappings().all()
    department_map = {row["revision_id"]: dict(row) for row in departments}
    if set(department_map) != set(revision.source_department_revision_ids):
        raise ApprovalStoreError("source_department_revision_not_found")
    for row in department_map.values():
        if (
            row["approval_run_id"] != revision.approval_run_id
            or row["ticket_id"] != revision.ticket_id
            or not row["is_current"]
            or row["status"] != "approved"
        ):
            raise ApprovalStoreError("source_department_revision_not_eligible")
    if {row["department_id"] for row in department_map.values()} != set(
        run["active_departments"]
    ):
        raise ApprovalStoreError("proposal_department_scope_incomplete")

    inputs = session.execute(
        select(rfp_authorized_business_inputs.c.input_id).where(
            rfp_authorized_business_inputs.c.input_id.in_(
                revision.authorized_input_ids
            ),
            rfp_authorized_business_inputs.c.approval_run_id
            == revision.approval_run_id,
            rfp_authorized_business_inputs.c.ticket_id == revision.ticket_id,
        )
    ).scalars().all()
    if set(inputs) != set(revision.authorized_input_ids):
        raise ApprovalStoreError("authorized_input_reference_mismatch")

    _lock_validation_bundle(
        session,
        revision=revision,
        scope="proposal",
        required_draft_source_kind=(
            "part2_import"
            if command.creation_mode == "part2_import"
            else "human_correction"
        ),
    )
    if not revision.validation.source_departments_approved:
        raise ApprovalStoreError("initial_proposal_sources_not_approved")
    by_department = {
        row["department_id"]: row["revision_id"] for row in department_map.values()
    }
    session.execute(
        insert(rfp_approval_proposal_revisions).values(
            proposal_revision_id=revision.revision_id,
            approval_run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            response_run_id=command.response_run_id,
            revision_no=1,
            source_global_draft_id=revision.source_global_draft_id,
            source_global_draft_hash=revision.source_global_draft_hash,
            source_global_evaluation_id=revision.source_global_evaluation_id,
            source_global_evaluation_hash=revision.source_global_evaluation_hash,
            warehouse_revision_id=by_department.get("warehouse"),
            lastmile_revision_id=by_department.get("lastmile"),
            reverse_revision_id=by_department.get("reverse"),
            content_payload={
                "currency": canonical_value(revision.currency),
                "sla": canonical_value(revision.sla),
                "volume_discounts": canonical_value(revision.volume_discounts),
                "source_department_revision_ids": [
                    str(item) for item in revision.source_department_revision_ids
                ],
                "authorized_input_ids": [
                    str(item) for item in revision.authorized_input_ids
                ],
            },
            content_hash=revision.content_hash,
            validation_payload=canonical_value(revision.validation),
            validation_hash=revision.validation.validation_hash,
            evaluated_content_hash=revision.content_hash,
            validation_bundle_id=revision.validation_bundle_id,
            eligible_for_synthesis=revision.validation.eligible_for_synthesis,
            is_current=True,
            state_version=0,
            created_at=revision.created_at,
        )
    )
    _insert_event(
        session,
        _event_values(
            event_key=event_key,
            run_id=revision.approval_run_id,
            ticket_id=revision.ticket_id,
            execution_id=execution_id,
            thread_id=f"proposal-revision:{revision.revision_id}",
            event_type="proposal_revision_initial_created",
            safe_input={
                "request_hash": request_hash,
                "creation_mode": command.creation_mode,
                "response_handoff_hash": command.response_handoff_hash,
                "content_hash": revision.content_hash,
                "validation_bundle_id": str(revision.validation_bundle_id),
            },
            safe_output={
                "proposal_revision_id": str(revision.revision_id),
                "revision_no": 1,
                "eligible_for_synthesis": (
                    revision.validation.eligible_for_synthesis
                ),
            },
            identity=identity,
            authorization=authorization,
            proposal_revision_id=revision.revision_id,
        ),
    )
    return ProposalRevisionRecord(
        revision.revision_id,
        revision.approval_run_id,
        1,
        revision.validation.eligible_for_synthesis,
        0,
        False,
    )
