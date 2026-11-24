"""Transactional persistence and authenticated resolution of RFP conflicts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationResolution,
    ConflictAssessment,
    CurrencyMismatchFacts,
    ReturnsSLAFacts,
    VolumeCapacityFacts,
)
from services.rfp_approval.authorization import (
    AuthorizationTarget,
    resolve_effective_authorization,
)
from services.rfp_approval.identity import (
    VerifiedAuthenticationContext,
    resolve_authenticated_identity,
)
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_approval_validation_bundles,
    rfp_authorized_business_inputs,
    rfp_arbitration_conflicts,
    rfp_arbitration_resolution_evidence,
    rfp_arbitration_resolutions,
    rfp_conflict_assessments,
    rfp_workflow_events,
)


class ArbitrationStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("RFP arbitration operation was rejected")
        self.code = code


_RESPONSIBLE_NAMES = {
    "ana_whitfield": "Ana Whitfield",
    "carlos_vega": "Carlos Vega",
    "sofia_ramos": "Sofía Ramos",
    "miguel_torres": "Miguel Torres",
}

_SCOPE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_CONTENT_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_ASSESSED_HASH_FIELDS = {"scope_key", "content_hash"}


def _serialize_assessed_content_hashes(
    assessed_content_hashes: dict[str, str],
) -> list[dict[str, str]]:
    """Return the deterministic JSON-array representation required by PostgreSQL."""

    if not isinstance(assessed_content_hashes, dict):
        raise ArbitrationStoreError("assessed_content_hashes_invalid")
    entries: list[dict[str, str]] = []
    for scope_key, content_hash in assessed_content_hashes.items():
        if (
            not isinstance(scope_key, str)
            or _SCOPE_KEY_PATTERN.fullmatch(scope_key) is None
            or not isinstance(content_hash, str)
            or _CONTENT_HASH_PATTERN.fullmatch(content_hash) is None
        ):
            raise ArbitrationStoreError("assessed_content_hashes_invalid")
        entries.append({"scope_key": scope_key, "content_hash": content_hash})
    return sorted(entries, key=lambda entry: entry["scope_key"])


def _deserialize_assessed_content_hashes(value: Any) -> dict[str, str]:
    """Validate and decode the canonical persisted JSON-array representation."""

    if not isinstance(value, list):
        raise ArbitrationStoreError("persisted_assessed_content_hashes_invalid")
    decoded: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != _ASSESSED_HASH_FIELDS:
            raise ArbitrationStoreError("persisted_assessed_content_hashes_invalid")
        scope_key = entry["scope_key"]
        content_hash = entry["content_hash"]
        if (
            not isinstance(scope_key, str)
            or _SCOPE_KEY_PATTERN.fullmatch(scope_key) is None
            or not isinstance(content_hash, str)
            or _CONTENT_HASH_PATTERN.fullmatch(content_hash) is None
            or scope_key in decoded
        ):
            raise ArbitrationStoreError("persisted_assessed_content_hashes_invalid")
        decoded[scope_key] = content_hash
    if value != _serialize_assessed_content_hashes(decoded):
        raise ArbitrationStoreError("persisted_assessed_content_hashes_invalid")
    return decoded


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    assessment_id: UUID
    conflict_id: UUID | None
    outcome: str
    conflict_status: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResolutionRecord:
    resolution_id: UUID
    conflict_id: UUID
    action: str
    conflict_status: str
    replayed: bool


def _event(
    session: Session,
    *,
    event_key: str,
    run_id: UUID,
    ticket_id: UUID,
    execution_id: UUID,
    sequence_no: int,
    event_type: str,
    safe_input: dict[str, Any],
    safe_output: dict[str, Any],
    conflict_id: UUID | None = None,
    identity=None,
    authorization=None,
) -> None:
    values = {
        "event_id": uuid4(),
        "event_key": event_key,
        "approval_run_id": run_id,
        "ticket_id": ticket_id,
        "thread_id": f"rfp-{ticket_id}:arbitration:{run_id}",
        "execution_id": execution_id,
        "sequence_no": sequence_no,
        "node_name": "arbitrate_conflict",
        "agent_name": "trackflow_arbitration_service",
        "event_type": event_type,
        "safe_input_ref": safe_input,
        "safe_output_ref": safe_output,
        "result": "succeeded",
        "from_state": None,
        "to_state": None,
        "conflict_id": conflict_id,
        "started_at": func.clock_timestamp(),
        "finished_at": func.clock_timestamp(),
    }
    if identity is not None and authorization is not None:
        values.update(
            actor_principal_id=identity.principal_id,
            actor_binding_id=identity.binding_id,
            authorization_id=authorization.authorization_id,
            authorization_version=authorization.authorization_version,
            environment=identity.environment,
        )
    inserted = session.execute(
        pg_insert(rfp_workflow_events)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[rfp_workflow_events.c.event_key])
    )
    if inserted.rowcount == 1:
        return
    existing = session.execute(
        select(rfp_workflow_events).where(rfp_workflow_events.c.event_key == event_key)
    ).mappings().one_or_none()
    comparable = {
        key: value
        for key, value in values.items()
        if key not in {"event_id", "started_at", "finished_at"}
    }
    if existing is None or any(existing[key] != value for key, value in comparable.items()):
        raise ArbitrationStoreError("audit_event_collision")


def _next_sequence(session: Session, run_id: UUID, ticket_id: UUID) -> int:
    thread_id = f"rfp-{ticket_id}:arbitration:{run_id}"
    return session.execute(
        select(func.coalesce(func.max(rfp_workflow_events.c.sequence_no), 0) + 1).where(
            rfp_workflow_events.c.approval_run_id == run_id,
            rfp_workflow_events.c.thread_id == thread_id,
        )
    ).scalar_one()


def persist_conflict_assessment(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    assessment: ConflictAssessment,
    assessed_content_hashes: dict[str, str],
    conflict: ArbitrationConflict | None,
    execution_id: UUID,
) -> ConflictRecord:
    if not session.in_transaction():
        raise ArbitrationStoreError("explicit_transaction_required")
    persisted_content_hashes = _serialize_assessed_content_hashes(
        assessed_content_hashes
    )
    if assessment.assessed_content_hashes and (
        assessment.assessed_content_hashes != assessed_content_hashes
    ):
        raise ArbitrationStoreError("assessment_content_hashes_mismatch")
    run = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run is None or run["status"] not in {"active", "blocked", "finalizing"}:
        raise ArbitrationStoreError("approval_run_not_arbitrable")
    if conflict is None and assessment.outcome == "demonstrated":
        raise ArbitrationStoreError("conflict_required_for_demonstrated_assessment")
    if conflict is not None and assessment.outcome != "demonstrated":
        raise ArbitrationStoreError("conflict_requires_demonstrated_assessment")
    if conflict is not None:
        if (
            conflict.approval_run_id != approval_run_id
            or conflict.trigger != assessment.trigger
            or conflict.status != assessment.outcome
            or conflict.facts != assessment.facts
        ):
            raise ArbitrationStoreError("conflict_assessment_mismatch")
        target_ids = {
            target.department_revision_id or target.proposal_revision_id
            for target in conflict.affected_targets
        }
        if isinstance(conflict.facts, ReturnsSLAFacts) and (
            not target_ids
            or not target_ids.issubset(set(conflict.facts.violating_revision_ids))
        ):
            raise ArbitrationStoreError("return_sla_target_mismatch")
        if isinstance(conflict.facts, CurrencyMismatchFacts) and target_ids != set(
            conflict.facts.mismatched_revision_ids
        ):
            raise ArbitrationStoreError("currency_target_mismatch")
        if isinstance(conflict.facts, VolumeCapacityFacts) and not any(
            target.department is not None and target.department.value == "lastmile"
            for target in conflict.affected_targets
        ):
            raise ArbitrationStoreError("volume_lastmile_review_required")
        for target in conflict.affected_targets:
            table = (
                rfp_approval_department_revisions
                if target.department_revision_id is not None
                else rfp_approval_proposal_revisions
            )
            id_column = (
                table.c.revision_id
                if target.department_revision_id is not None
                else table.c.proposal_revision_id
            )
            target_id = target.department_revision_id or target.proposal_revision_id
            target_row = session.execute(
                select(id_column)
                .where(
                    id_column == target_id,
                    table.c.approval_run_id == approval_run_id,
                    table.c.ticket_id == ticket_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if target_row is None:
                raise ArbitrationStoreError("conflict_target_not_persisted")
    fact_input_ids: set[UUID] = set()
    if isinstance(assessment.facts, VolumeCapacityFacts):
        fact_input_ids = {
            item
            for item in (
                assessment.facts.capacity_input_id,
                assessment.facts.normalization_input_id,
            )
            if item is not None
        }
    if fact_input_ids:
        persisted_inputs = set(
            session.execute(
                select(rfp_authorized_business_inputs.c.input_id).where(
                    rfp_authorized_business_inputs.c.input_id.in_(fact_input_ids),
                    rfp_authorized_business_inputs.c.approval_run_id
                    == approval_run_id,
                    rfp_authorized_business_inputs.c.ticket_id == ticket_id,
                )
            ).scalars().all()
        )
        if persisted_inputs != fact_input_ids:
            raise ArbitrationStoreError("conflict_fact_input_not_persisted")

    existing_assessment = session.execute(
        select(rfp_conflict_assessments).where(
            rfp_conflict_assessments.c.approval_run_id == approval_run_id,
            rfp_conflict_assessments.c.assessment_hash == assessment.assessment_hash,
        )
    ).mappings().one_or_none()
    assessment_replayed = existing_assessment is not None
    replayed = assessment_replayed
    if assessment_replayed:
        if existing_assessment["assessment_id"] != assessment.assessment_id:
            raise ArbitrationStoreError("assessment_hash_collision")
        if (
            _deserialize_assessed_content_hashes(
                existing_assessment["assessed_content_hashes"]
            )
            != assessed_content_hashes
        ):
            raise ArbitrationStoreError("assessment_content_hashes_mismatch")
    else:
        session.execute(
            insert(rfp_conflict_assessments).values(
                assessment_id=assessment.assessment_id,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                trigger_id=assessment.trigger,
                outcome=assessment.outcome,
                facts_payload=canonical_value(assessment.facts),
                missing_input_codes=assessment.missing_input_codes,
                assessed_content_hashes=persisted_content_hashes,
                assessment_hash=assessment.assessment_hash,
                assessed_at=assessment.assessed_at,
            )
        )

    conflict_row = None
    conflict_replayed = False
    if conflict is not None:
        conflict_row = session.execute(
            select(rfp_arbitration_conflicts).where(
                rfp_arbitration_conflicts.c.approval_run_id == approval_run_id,
                rfp_arbitration_conflicts.c.fingerprint == conflict.fingerprint,
            )
        ).mappings().one_or_none()
        if conflict_row is None:
            session.execute(
                insert(rfp_arbitration_conflicts).values(
                    conflict_id=conflict.conflict_id,
                    approval_run_id=approval_run_id,
                    ticket_id=ticket_id,
                    assessment_id=assessment.assessment_id,
                    assessment_outcome=assessment.outcome,
                    trigger_id=assessment.trigger,
                    affected_targets=canonical_value(conflict.affected_targets),
                    fingerprint=conflict.fingerprint,
                    status="open",
                    state_version=0,
                    detected_at=conflict.detected_at,
                )
            )
            conflict_status = "open"
        else:
            if conflict_row["conflict_id"] != conflict.conflict_id:
                raise ArbitrationStoreError("conflict_fingerprint_collision")
            conflict_status = conflict_row["status"]
            conflict_replayed = True
            replayed = True
    else:
        conflict_status = None

    if assessment_replayed and (conflict is None or conflict_replayed):
        return ConflictRecord(
            assessment_id=assessment.assessment_id,
            conflict_id=conflict.conflict_id if conflict else None,
            outcome=assessment.outcome,
            conflict_status=conflict_status,
            replayed=True,
        )

    sequence = _next_sequence(session, approval_run_id, ticket_id)
    base_key = canonical_hash(
        {
            "run_id": approval_run_id,
            "assessment_hash": assessment.assessment_hash,
            "conflict_fingerprint": conflict.fingerprint if conflict else None,
        }
    )
    _event(
        session,
        event_key=f"arbitration-started:{base_key}",
        run_id=approval_run_id,
        ticket_id=ticket_id,
        execution_id=execution_id,
        sequence_no=sequence,
        event_type="arbitration_started",
        safe_input={"assessment_hash": assessment.assessment_hash, "trigger": assessment.trigger},
        safe_output={"assessment_id": str(assessment.assessment_id)},
        conflict_id=conflict.conflict_id if conflict else None,
    )
    _event(
        session,
        event_key=f"arbitration-evaluated:{base_key}",
        run_id=approval_run_id,
        ticket_id=ticket_id,
        execution_id=execution_id,
        sequence_no=sequence + 1,
        event_type="arbitration_evaluated",
        safe_input={"assessment_id": str(assessment.assessment_id)},
        safe_output={"outcome": assessment.outcome, "conflict_status": conflict_status},
        conflict_id=conflict.conflict_id if conflict else None,
    )
    return ConflictRecord(
        assessment_id=assessment.assessment_id,
        conflict_id=conflict.conflict_id if conflict else None,
        outcome=assessment.outcome,
        conflict_status=conflict_status,
        replayed=replayed,
    )


def _authorization_target(
    resolution: ArbitrationResolution,
    active_departments: list[Any],
) -> AuthorizationTarget:
    departments = frozenset(
        item.department.value for item in resolution.targets if item.department is not None
    )
    if (
        resolution.trigger == "returns-sla-breach"
        and "reverse" in departments
        and len(departments) > 1
    ):
        raise ArbitrationStoreError("returns_authority_partition_required")
    if resolution.trigger == "returns-sla-breach" and departments == {"reverse"}:
        return AuthorizationTarget(
            permission="arbitrate",
            department_scope="reverse",
            trigger_scope=resolution.trigger,
            target_policy="department_exact",
            affected_departments=departments,
        )
    return AuthorizationTarget(
        permission="arbitrate",
        trigger_scope=resolution.trigger,
        target_policy=(
            "non_reverse_or_cross"
            if resolution.trigger == "returns-sla-breach"
            else "cross_department"
        ),
        affected_departments=departments
        or frozenset(
            item.value if hasattr(item, "value") else str(item)
            for item in active_departments
        ),
    )


def record_arbitration_resolution(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    resolution: ArbitrationResolution,
    idempotency_key: UUID,
    authentication: VerifiedAuthenticationContext,
    execution_id: UUID,
) -> ResolutionRecord:
    if not session.in_transaction():
        raise ArbitrationStoreError("explicit_transaction_required")
    run = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run is None or run["status"] not in {"active", "blocked", "finalizing"}:
        raise ArbitrationStoreError("approval_run_not_arbitrable")
    identity = resolve_authenticated_identity(session, context=authentication)
    authorization = resolve_effective_authorization(
        session,
        identity=identity,
        target=_authorization_target(
            resolution,
            run.get("active_departments", ["warehouse", "lastmile", "reverse"]),
        ),
        lock_rows=True,
    )
    actor = resolution.execution.actor
    principal = actor.principal
    if (
        principal.issuer != identity.issuer
        or principal.subject != identity.subject
        or principal.trackflow_user_id != identity.trackflow_user_id
        or principal.auth_source != identity.auth_source
        or principal.synthetic != identity.synthetic
        or actor.authorization_id != authorization.authorization_id
        or actor.official_responsible
        != _RESPONSIBLE_NAMES.get(identity.responsible_code)
    ):
        raise ArbitrationStoreError("resolution_actor_identity_mismatch")

    conflict = session.execute(
        select(rfp_arbitration_conflicts)
        .where(
            rfp_arbitration_conflicts.c.conflict_id == resolution.conflict_id,
            rfp_arbitration_conflicts.c.approval_run_id == approval_run_id,
            rfp_arbitration_conflicts.c.ticket_id == ticket_id,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if conflict is None:
        raise ArbitrationStoreError("conflict_not_found")
    if (
        conflict["fingerprint"] != resolution.conflict_fingerprint
        or conflict["trigger_id"] != resolution.trigger
        or conflict["affected_targets"] != canonical_value(resolution.targets)
    ):
        raise ArbitrationStoreError("resolution_conflict_mismatch")

    existing = session.execute(
        select(rfp_arbitration_resolutions).where(
            rfp_arbitration_resolutions.c.approval_run_id == approval_run_id,
            rfp_arbitration_resolutions.c.idempotency_key == idempotency_key,
        )
    ).mappings().one_or_none()
    if existing is not None:
        if existing["resolution_hash"] != resolution.resolution_hash:
            raise ArbitrationStoreError("resolution_idempotency_collision")
        return ResolutionRecord(
            existing["resolution_id"],
            existing["conflict_id"],
            existing["action"],
            conflict["status"],
            True,
        )
    material_replay = session.execute(
        select(rfp_arbitration_resolutions).where(
            rfp_arbitration_resolutions.c.conflict_id == resolution.conflict_id,
            rfp_arbitration_resolutions.c.resolution_hash == resolution.resolution_hash,
        )
    ).mappings().one_or_none()
    if material_replay is not None:
        return ResolutionRecord(
            material_replay["resolution_id"],
            material_replay["conflict_id"],
            material_replay["action"],
            conflict["status"],
            True,
        )
    if resolution.authorized_input_ids:
        requested_inputs = set(resolution.authorized_input_ids)
        persisted_inputs = set(
            session.execute(
                select(rfp_authorized_business_inputs.c.input_id).where(
                    rfp_authorized_business_inputs.c.input_id.in_(requested_inputs),
                    rfp_authorized_business_inputs.c.approval_run_id
                    == approval_run_id,
                    rfp_authorized_business_inputs.c.ticket_id == ticket_id,
                )
            ).scalars().all()
        )
        if persisted_inputs != requested_inputs:
            raise ArbitrationStoreError("resolution_authorized_input_not_persisted")
    if conflict["status"] == "resolved":
        raise ArbitrationStoreError("conflict_already_resolved")

    post = resolution.post_correction_assessment
    if resolution.action == "accept_resolution":
        if conflict["status"] != "changes_requested":
            raise ArbitrationStoreError("request_changes_required_before_acceptance")
        persisted_post = session.execute(
            select(rfp_conflict_assessments).where(
                rfp_conflict_assessments.c.assessment_id == post.assessment_id,
                rfp_conflict_assessments.c.approval_run_id == approval_run_id,
                rfp_conflict_assessments.c.outcome == "no_conflict",
            )
        ).mappings().one_or_none()
        if persisted_post is None or persisted_post["assessment_hash"] != post.assessment_hash:
            raise ArbitrationStoreError("post_correction_assessment_not_persisted")
        for evidence in resolution.corrected_evidence:
            original = evidence.original_target
            table = (
                rfp_approval_department_revisions
                if original.department_revision_id is not None
                else rfp_approval_proposal_revisions
            )
            id_column = (
                table.c.revision_id
                if original.department_revision_id is not None
                else table.c.proposal_revision_id
            )
            original_id = (
                original.department_revision_id or original.proposal_revision_id
            )
            original_row = session.execute(
                select(table)
                .where(
                    id_column == original_id,
                    table.c.approval_run_id == approval_run_id,
                    table.c.ticket_id == ticket_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            corrected_row = session.execute(
                select(table)
                .where(
                    id_column == evidence.corrected_revision_id,
                    table.c.approval_run_id == approval_run_id,
                    table.c.ticket_id == ticket_id,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if original_row is None or corrected_row is None:
                raise ArbitrationStoreError("corrected_revision_not_persisted")
            if (
                not corrected_row["is_current"]
                or corrected_row["content_hash"] != evidence.corrected_content_hash
            ):
                raise ArbitrationStoreError("corrected_revision_obsolete")
            revision_number = (
                corrected_row["review_revision_no"]
                if original.department_revision_id is not None
                else corrected_row["revision_no"]
            )
            if revision_number > 3:
                raise ArbitrationStoreError("revision_limit_reached")
            validation = evidence.revision_validation
            if corrected_row["validation_bundle_id"] != validation.artifact_id:
                raise ArbitrationStoreError(
                    "corrected_revision_validation_not_persisted"
                )
            validation_bundle = session.execute(
                select(rfp_approval_validation_bundles).where(
                    rfp_approval_validation_bundles.c.validation_bundle_id
                    == validation.artifact_id,
                    rfp_approval_validation_bundles.c.approval_run_id
                    == approval_run_id,
                    rfp_approval_validation_bundles.c.ticket_id == ticket_id,
                    rfp_approval_validation_bundles.c.scope_key
                    == (
                        original.department.value
                        if original.department is not None
                        else "proposal"
                    ),
                    rfp_approval_validation_bundles.c.contract_version
                    == validation.contract_version,
                    rfp_approval_validation_bundles.c.content_hash
                    == evidence.corrected_content_hash,
                    rfp_approval_validation_bundles.c.revision_validation_hash
                    == validation.payload_hash,
                )
                .with_for_update()
            ).mappings().one_or_none()
            if validation_bundle is None:
                raise ArbitrationStoreError(
                    "corrected_revision_validation_not_persisted"
                )
            recheck = evidence.conflict_recheck
            if (
                recheck.artifact_id != post.assessment_id
                or recheck.contract_version != post.contract_version
            ):
                raise ArbitrationStoreError("conflict_recheck_not_persisted")
            persisted_recheck = session.execute(
                select(rfp_conflict_assessments).where(
                    rfp_conflict_assessments.c.assessment_id == recheck.artifact_id,
                    rfp_conflict_assessments.c.approval_run_id == approval_run_id,
                    rfp_conflict_assessments.c.ticket_id == ticket_id,
                    rfp_conflict_assessments.c.assessment_hash
                    == recheck.payload_hash,
                    rfp_conflict_assessments.c.outcome == "no_conflict",
                )
                .with_for_update()
            ).mappings().one_or_none()
            recheck_scope = (
                original.department.value
                if original.department is not None
                else "proposal"
            )
            if (
                persisted_recheck is None
                or _deserialize_assessed_content_hashes(
                    persisted_recheck["assessed_content_hashes"]
                ).get(recheck_scope)
                != evidence.corrected_content_hash
            ):
                raise ArbitrationStoreError("conflict_recheck_not_persisted")

    session.execute(
        insert(rfp_arbitration_resolutions).values(
            resolution_id=resolution.resolution_id,
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            conflict_id=resolution.conflict_id,
            trigger_id=resolution.trigger,
            conflict_fingerprint=resolution.conflict_fingerprint,
            idempotency_key=idempotency_key,
            action=resolution.action,
            targets=canonical_value(resolution.targets),
            authorized_input_ids=[str(item) for item in resolution.authorized_input_ids],
            reason=resolution.reason,
            actor_principal_id=identity.principal_id,
            actor_binding_id=identity.binding_id,
            authorization_id=authorization.authorization_id,
            authorization_version=authorization.authorization_version,
            environment=identity.environment,
            post_assessment_id=post.assessment_id if post else None,
            post_assessment_outcome=post.outcome if post else None,
            resolution_hash=resolution.resolution_hash,
            decided_at=resolution.execution.decided_at,
        )
    )
    for evidence in resolution.corrected_evidence:
        original = evidence.original_target
        session.execute(
            insert(rfp_arbitration_resolution_evidence).values(
                evidence_id=uuid4(),
                resolution_id=resolution.resolution_id,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                original_department_revision_id=original.department_revision_id,
                original_proposal_revision_id=original.proposal_revision_id,
                corrected_department_revision_id=(
                    evidence.corrected_revision_id
                    if original.department_revision_id is not None else None
                ),
                corrected_proposal_revision_id=(
                    evidence.corrected_revision_id
                    if original.proposal_revision_id is not None else None
                ),
                corrected_content_hash=evidence.corrected_content_hash,
                revision_validation_artifact_id=evidence.revision_validation.artifact_id,
                revision_validation_hash=evidence.revision_validation.payload_hash,
                conflict_recheck_artifact_id=evidence.conflict_recheck.artifact_id,
                conflict_recheck_hash=evidence.conflict_recheck.payload_hash,
            )
        )
    new_status = "changes_requested" if resolution.action == "request_changes" else "resolved"
    changed = session.execute(
        update(rfp_arbitration_conflicts)
        .where(
            rfp_arbitration_conflicts.c.conflict_id == resolution.conflict_id,
            rfp_arbitration_conflicts.c.state_version == conflict["state_version"],
            rfp_arbitration_conflicts.c.status == conflict["status"],
        )
        .values(
            status=new_status,
            state_version=rfp_arbitration_conflicts.c.state_version + 1,
            resolved_at=(func.clock_timestamp() if new_status == "resolved" else None),
        )
    ).rowcount
    if changed != 1:
        raise ArbitrationStoreError("conflict_state_cas_failed")

    sequence = _next_sequence(session, approval_run_id, ticket_id)
    _event(
        session,
        event_key="arbitration-resolution:" + resolution.resolution_hash,
        run_id=approval_run_id,
        ticket_id=ticket_id,
        execution_id=execution_id,
        sequence_no=sequence,
        event_type="arbitration_resolution_authorized",
        safe_input={
            "resolution_id": str(resolution.resolution_id),
            "action": resolution.action,
        },
        safe_output={"conflict_status": new_status},
        conflict_id=resolution.conflict_id,
        identity=identity,
        authorization=authorization,
    )
    _event(
        session,
        event_key="arbitration-conflict-state:" + resolution.resolution_hash,
        run_id=approval_run_id,
        ticket_id=ticket_id,
        execution_id=execution_id,
        sequence_no=sequence + 1,
        event_type="arbitration_conflict_state_changed",
        safe_input={
            "conflict_id": str(resolution.conflict_id),
            "previous_state_version": conflict["state_version"],
        },
        safe_output={
            "conflict_status": new_status,
            "state_version": conflict["state_version"] + 1,
        },
        conflict_id=resolution.conflict_id,
        identity=identity,
        authorization=authorization,
    )
    return ResolutionRecord(
        resolution.resolution_id,
        resolution.conflict_id,
        resolution.action,
        new_status,
        False,
    )
