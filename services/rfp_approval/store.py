"""Transactional PostgreSQL store for TrackFlow human approval decisions.

Every public operation participates in the caller's transaction.  This module
never commits or rolls back.  Callers must open a transaction and treat raised
exceptions as a reason to roll it back.

Idempotency is envelope-exact: the same persisted identifiers, timestamps and
canonical fingerprint replay the stored outcome.  Reconstructing the same
business intention with new identifiers or timestamps is a different command,
not an idempotent replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Sequence
from uuid import UUID, uuid4

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from data.pipelines.rfp_response.contracts import RFPResponseHandoff
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalRunStart,
    DecisionApplicationCommand,
    DepartmentRevision,
    acknowledges_indicative_commercial_items,
)
from services.rfp_approval.authorization import (
    AuthorizationReference,
    AuthorizationTarget,
    EffectiveAuthorization,
    resolve_effective_authorization,
)
from services.rfp_approval.identity import ResolvedIdentity, _is_resolved_identity
from services.rfp_approval.models import (
    rfp_arbitration_conflicts,
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_bundles,
    rfp_authorizations,
    rfp_human_decisions,
    rfp_identity_bindings,
    rfp_principals,
    rfp_workflow_events,
    rfp_conflict_assessments,
)
from services.rfp_approval.arbitration import _deserialize_assessed_content_hashes
from services.rfp_intake.models import rfp_tickets
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_runs,
)


DecisionStatus = Literal["queued", "claimed", "applied", "rejected_stale"]


class ApprovalStoreError(RuntimeError):
    """Fail-closed store error with a stable, non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        super().__init__("RFP approval persistence operation was rejected")
        self.code = code


_REVISION_CREATION_REASONS = frozenset(
    {
        "part2_import",
        "authorized_input",
        "human_request_changes",
        "arbitration_request_changes",
    }
)


def _persisted_revision_creation_reason(row: dict[str, Any]) -> str:
    """Read the contract field from its canonical persisted JSONB envelope."""

    payload = row.get("content_payload")
    reason = payload.get("creation_reason") if isinstance(payload, dict) else None
    if reason not in _REVISION_CREATION_REASONS:
        raise ApprovalStoreError("revision_creation_reason_invalid")
    return reason


@dataclass(frozen=True, slots=True)
class ApprovalRunRecord:
    approval_run_id: UUID
    ticket_id: UUID
    response_run_id: UUID
    status: str
    state_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class InitialValidationLinkRecord:
    approval_run_id: UUID
    revision_id: UUID
    validation_bundle_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: UUID
    approval_run_id: UUID
    revision_id: UUID
    processing_status: DecisionStatus
    application_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class DecisionClaim:
    decision_id: UUID
    approval_run_id: UUID
    revision_id: UUID
    claim_token: UUID
    claim_epoch: int
    lease_until: datetime
    expected_revision_state_version: int


@dataclass(frozen=True, slots=True)
class DecisionApplicationResult:
    decision_id: UUID
    revision_id: UUID
    action: str
    processing_status: DecisionStatus
    revision_status: str
    revision_state_version: int
    replayed: bool


_SAFE_REF_ALLOWLISTS: dict[str, frozenset[str]] = {
    "run_start_input": frozenset(
        {
            "response_run_id",
            "response_handoff_version",
            "response_handoff_hash",
            "idempotency_key",
            "request_hash",
            "active_departments",
            "automate_proposal_materialization",
        }
    ),
    "run_start_output": frozenset({"approval_run_id", "status", "state_version"}),
    "initial_validation_link_input": frozenset(
        {"revision_id", "validation_bundle_id", "content_hash", "validation_hash"}
    ),
    "initial_validation_link_output": frozenset(
        {"revision_id", "validation_bundle_id", "status"}
    ),
    "decision_input": frozenset(
        {
            "decision_id",
            "revision_id",
            "department_id",
            "expected_content_hash",
            "action",
            "idempotency_key",
            "request_fingerprint",
        }
    ),
    "decision_output": frozenset(
        {"decision_id", "processing_status", "revision_state_version"}
    ),
    "claim_input": frozenset({"decision_id", "previous_epoch"}),
    "claim_output": frozenset(
        {"decision_id", "claim_epoch", "lease_until", "revision_state_version"}
    ),
    "application_input": frozenset(
        {
            "decision_id",
            "revision_id",
            "expected_content_hash",
            "action",
            "claim_epoch",
            "expected_revision_state_version",
        }
    ),
    "application_output": frozenset(
        {"decision_id", "processing_status", "revision_status", "state_version"}
    ),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ApprovalStoreError("unsafe_audit_reference_value")


def _safe_ref(kind: str, **values: Any) -> dict[str, Any]:
    allowed = _SAFE_REF_ALLOWLISTS.get(kind)
    if allowed is None or set(values) != set(values) & allowed:
        raise ApprovalStoreError("unsafe_audit_reference_key")
    if kind == "run_start_input" and not isinstance(
        values.get("automate_proposal_materialization"), bool
    ):
        raise ApprovalStoreError("unsafe_audit_reference_value")
    return {key: _json_safe(value) for key, value in values.items()}


def _insert_event(
    session: Session,
    *,
    event_key: str,
    approval_run_id: UUID,
    ticket_id: UUID,
    thread_id: str,
    execution_id: UUID,
    sequence_no: int,
    node_name: str,
    event_type: str,
    safe_input_ref: dict[str, Any],
    safe_output_ref: dict[str, Any],
    result: str,
    from_state: str | None,
    to_state: str | None,
    revision_id: UUID | None = None,
    decision_id: UUID | None = None,
    identity: ResolvedIdentity | None = None,
    authorization: EffectiveAuthorization | None = None,
) -> None:
    values: dict[str, Any] = {
        "event_id": uuid4(),
        "event_key": event_key,
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "thread_id": thread_id,
        "execution_id": execution_id,
        "sequence_no": sequence_no,
        "node_name": node_name,
        "agent_name": "rfp_approval_store",
        "event_type": event_type,
        "safe_input_ref": safe_input_ref,
        "safe_output_ref": safe_output_ref,
        "result": result,
        "from_state": from_state,
        "to_state": to_state,
        "department_revision_id": revision_id,
        "decision_id": decision_id,
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
    statement = pg_insert(rfp_workflow_events).values(**values).on_conflict_do_nothing(
        index_elements=[rfp_workflow_events.c.event_key]
    )
    inserted = session.execute(statement)
    if inserted.rowcount == 1:
        return
    existing = session.execute(
        select(rfp_workflow_events).where(
            rfp_workflow_events.c.event_key == event_key
        )
    ).mappings().one_or_none()
    if existing is None:
        raise ApprovalStoreError("audit_event_insert_failed")
    comparable = {
        key: value
        for key, value in values.items()
        if key not in {"event_id", "started_at", "finished_at"}
    }
    if any(existing[key] != value for key, value in comparable.items()):
        raise ApprovalStoreError("audit_event_collision")


def approval_start_request_hash(
    start: ApprovalRunStart,
    initial_revisions: Sequence[DepartmentRevision],
    idempotency_key: UUID,
) -> str:
    return canonical_hash(
        {
            "start": start,
            "initial_revisions": list(initial_revisions),
            "idempotency_key": idempotency_key,
        }
    )


def _assert_identity_matches_principal(
    identity: ResolvedIdentity, principal: Any
) -> None:
    if not _is_resolved_identity(identity):
        raise ApprovalStoreError("unresolved_identity")
    expected = (
        identity.issuer,
        identity.subject,
        identity.trackflow_user_id,
        identity.auth_source,
        identity.general_role,
        identity.synthetic,
    )
    received = (
        principal.issuer,
        principal.subject,
        principal.trackflow_user_id,
        principal.auth_source,
        principal.general_role,
        principal.synthetic,
    )
    if expected != received:
        raise ApprovalStoreError("authenticated_principal_mismatch")


def _lock_authorization_rows(
    session: Session,
    *,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> None:
    """Acquire stable identity/grant locks before the time-sensitive recheck."""

    principal = session.execute(
        select(rfp_principals.c.principal_id)
        .where(
            rfp_principals.c.principal_id == identity.principal_id,
            rfp_principals.c.environment == identity.environment,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if principal is None:
        raise ApprovalStoreError("principal_not_found")
    binding = session.execute(
        select(rfp_identity_bindings.c.binding_id)
        .where(
            rfp_identity_bindings.c.binding_id == identity.binding_id,
            rfp_identity_bindings.c.principal_id == identity.principal_id,
            rfp_identity_bindings.c.environment == identity.environment,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if binding is None:
        raise ApprovalStoreError("binding_not_found")
    authorization = session.execute(
        select(rfp_authorizations.c.authorization_id)
        .where(
            rfp_authorizations.c.authorization_id == reference.authorization_id,
            rfp_authorizations.c.principal_id == identity.principal_id,
            rfp_authorizations.c.environment == identity.environment,
            rfp_authorizations.c.authorization_version
            == reference.authorization_version,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if authorization is None:
        raise ApprovalStoreError("authorization_not_found")


def _authorize_after_locks(
    session: Session,
    *,
    identity: ResolvedIdentity,
    target: AuthorizationTarget,
    reference: AuthorizationReference,
) -> EffectiveAuthorization:
    _lock_authorization_rows(session, identity=identity, reference=reference)
    # This is intentionally a later statement.  Its statement_timestamp() is
    # taken after any wait needed to acquire the three locks above.
    return resolve_effective_authorization(
        session,
        identity=identity,
        target=target,
        reference=reference,
        lock_rows=False,
    )


def _validate_initial_revisions(
    start: ApprovalRunStart, revisions: Sequence[DepartmentRevision]
) -> None:
    if len(revisions) != len(start.initial_revisions):
        raise ApprovalStoreError("initial_revision_scope_mismatch")
    for reference, revision in zip(start.initial_revisions, revisions, strict=True):
        expected = (
            start.approval_run_id,
            start.ticket_id,
            reference.department,
            reference.source_draft_id,
            reference.source_draft_hash,
            reference.source_evaluation_id,
            reference.source_evaluation_hash,
            reference.source_part2_iteration,
        )
        received = (
            revision.approval_run_id,
            revision.ticket_id,
            revision.department,
            revision.source_part2_draft_id,
            revision.source_part2_draft_hash,
            revision.source_part2_evaluation_id,
            revision.source_part2_evaluation_hash,
            revision.source_part2_iteration,
        )
        if expected != received:
            raise ApprovalStoreError("initial_revision_reference_mismatch")
        if (
            revision.review_revision_no != 1
            or revision.creation_reason != "part2_import"
            or revision.supersedes_revision_id is not None
            or revision.created_by is not None
        ):
            raise ApprovalStoreError("invalid_initial_revision")


def _run_record(row: dict[str, Any], *, replayed: bool) -> ApprovalRunRecord:
    return ApprovalRunRecord(
        approval_run_id=row["approval_run_id"],
        ticket_id=row["ticket_id"],
        response_run_id=row["response_run_id"],
        status=row["status"],
        state_version=row["state_version"],
        replayed=replayed,
    )


def _assert_start_replay(
    row: dict[str, Any], start: ApprovalRunStart, request_hash: str
) -> None:
    if (
        row["approval_run_id"] != start.approval_run_id
        or row["ticket_id"] != start.ticket_id
        or row["response_run_id"] != start.response_run_id
        or row["response_handoff_hash"] != start.response_handoff_hash
        or row["start_request_hash"] != request_hash
    ):
        raise ApprovalStoreError("start_idempotency_key_reused")


def _authorized_start_replay(
    session: Session,
    *,
    row: dict[str, Any],
    start: ApprovalRunStart,
    request_hash: str,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> ApprovalRunRecord:
    """Authorize result access without repeating the confirmed start."""

    _assert_start_replay(row, start, request_hash)
    locked = session.execute(
        select(rfp_approval_runs)
        .where(rfp_approval_runs.c.approval_run_id == row["approval_run_id"])
        .with_for_update(key_share=True)
    ).mappings().one()
    persisted = dict(locked)
    _assert_start_replay(persisted, start, request_hash)
    if (
        persisted["started_by_principal_id"] != identity.principal_id
        or persisted["started_by_binding_id"] != identity.binding_id
        or persisted["environment"] != identity.environment
    ):
        raise ApprovalStoreError("start_replay_actor_mismatch")
    if (
        persisted["start_authorization_id"] != reference.authorization_id
        or persisted["authorization_version"] != reference.authorization_version
    ):
        raise ApprovalStoreError("start_replay_authorization_mismatch")
    _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(permission=start.started_by.permission),
        reference=reference,
    )
    return _run_record(persisted, replayed=True)


def start_approval_run(
    session: Session,
    *,
    start: ApprovalRunStart,
    initial_revisions: Sequence[DepartmentRevision],
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    idempotency_key: UUID,
    request_hash: str,
    execution_id: UUID,
) -> ApprovalRunRecord:
    """Atomically import validated Part 2 artifacts and start Parte 3."""

    expected_hash = approval_start_request_hash(start, initial_revisions, idempotency_key)
    if request_hash != expected_hash:
        raise ApprovalStoreError("start_request_hash_mismatch")
    _assert_identity_matches_principal(identity, start.started_by.principal)
    if start.started_by.authorization_id != authorization_reference.authorization_id:
        raise ApprovalStoreError("start_authorization_reference_mismatch")
    _validate_initial_revisions(start, initial_revisions)

    existing = session.execute(
        select(rfp_approval_runs).where(
            rfp_approval_runs.c.start_idempotency_key == idempotency_key
        )
    ).mappings().one_or_none()
    if existing is not None:
        row = dict(existing)
        return _authorized_start_replay(
            session,
            row=row,
            start=start,
            request_hash=request_hash,
            identity=identity,
            reference=authorization_reference,
        )

    ticket = session.execute(
        select(rfp_tickets.c.status)
        .where(rfp_tickets.c.ticket_id == start.ticket_id)
        .with_for_update()
    ).scalar_one_or_none()
    if ticket is None:
        raise ApprovalStoreError("ticket_not_found")
    # A concurrent starter may have committed while this transaction waited on
    # the ticket lock.  Recheck after the wait before validating the old ticket
    # state or attempting any insert.
    concurrent_existing = session.execute(
        select(rfp_approval_runs).where(
            rfp_approval_runs.c.start_idempotency_key == idempotency_key
        )
    ).mappings().one_or_none()
    if concurrent_existing is not None:
        row = dict(concurrent_existing)
        return _authorized_start_replay(
            session,
            row=row,
            start=start,
            request_hash=request_hash,
            identity=identity,
            reference=authorization_reference,
        )

    handoff_row = session.execute(
        select(rfp_response_handoffs).where(
            rfp_response_handoffs.c.run_id == start.response_run_id,
            rfp_response_handoffs.c.ticket_id == start.ticket_id,
            rfp_response_handoffs.c.contract_version
            == start.response_handoff_version,
            rfp_response_handoffs.c.payload_hash == start.response_handoff_hash,
        )
    ).mappings().one_or_none()
    if handoff_row is None:
        raise ApprovalStoreError("response_handoff_not_found")
    try:
        handoff = RFPResponseHandoff.model_validate(handoff_row["payload"], strict=False)
    except Exception as exc:
        raise ApprovalStoreError("invalid_response_handoff") from exc
    if canonical_hash(handoff_row["payload"]) != handoff_row["payload_hash"]:
        raise ApprovalStoreError("response_handoff_hash_mismatch")
    if (
        handoff.contract_version != start.response_handoff_version
        or handoff.ticket_id != start.ticket_id
        or handoff.run_id != start.response_run_id
        or [item.value for item in handoff.selected_departments]
        != [item.value for item in start.active_departments]
    ):
        raise ApprovalStoreError("response_handoff_scope_mismatch")
    handoff_results = {item.department: item for item in handoff.department_results}
    for reference in start.initial_revisions:
        result = handoff_results.get(reference.department)
        if (
            result is None
            or result.final_draft_id != reference.source_draft_id
            or result.evaluation_result_id != reference.source_evaluation_id
            or result.iteration_count != reference.source_part2_iteration
        ):
            raise ApprovalStoreError("response_handoff_revision_reference_mismatch")
    response_status = session.execute(
        select(rfp_response_runs.c.status).where(
            rfp_response_runs.c.run_id == start.response_run_id,
            rfp_response_runs.c.ticket_id == start.ticket_id,
        )
    ).scalar_one_or_none()
    if response_status != handoff.outcome:
        raise ApprovalStoreError("response_run_not_terminal")
    expected_ticket_status = (
        "en_evaluación" if handoff.outcome == "ready_for_part3" else "needs_human_review"
    )
    if ticket != expected_ticket_status:
        raise ApprovalStoreError("ticket_state_conflict")

    for reference in start.initial_revisions:
        draft_row = session.execute(
            select(
                rfp_response_drafts.c.payload_hash,
                rfp_response_drafts.c.iteration_count,
            ).where(
                rfp_response_drafts.c.draft_id == reference.source_draft_id,
                rfp_response_drafts.c.run_id == start.response_run_id,
                rfp_response_drafts.c.scope_key == reference.department.value,
            )
        ).mappings().one_or_none()
        evaluation = session.execute(
            select(rfp_response_evaluations.c.payload_hash).where(
                rfp_response_evaluations.c.evaluation_id
                == reference.source_evaluation_id,
                rfp_response_evaluations.c.run_id == start.response_run_id,
                rfp_response_evaluations.c.draft_id == reference.source_draft_id,
                rfp_response_evaluations.c.scope_key == reference.department.value,
            )
        ).scalar_one_or_none()
        if (
            draft_row is None
            or draft_row["payload_hash"] != reference.source_draft_hash
            or draft_row["iteration_count"] != reference.source_part2_iteration
        ):
            raise ApprovalStoreError("source_draft_reference_mismatch")
        if evaluation != reference.source_evaluation_hash:
            raise ApprovalStoreError("source_evaluation_hash_mismatch")

    authorization = _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(permission=start.started_by.permission),
        reference=authorization_reference,
    )

    created = session.execute(
        pg_insert(rfp_approval_runs)
        .values(
            approval_run_id=start.approval_run_id,
            ticket_id=start.ticket_id,
            response_run_id=start.response_run_id,
            contract_version=start.contract_version,
            response_handoff_version=start.response_handoff_version,
            response_handoff_hash=start.response_handoff_hash,
            active_departments=[item.value for item in start.active_departments],
            max_review_revisions=start.max_review_revisions,
            status="active",
            state_version=0,
            start_idempotency_key=idempotency_key,
            start_request_hash=request_hash,
            started_by_principal_id=identity.principal_id,
            started_by_binding_id=identity.binding_id,
            start_authorization_id=authorization.authorization_id,
            authorization_version=authorization.authorization_version,
            started_by_general_role=(
                identity.general_role
                if getattr(start, "automate_proposal_materialization", False)
                else None
            ),
            automate_proposal_materialization=(
                getattr(start, "automate_proposal_materialization", False)
            ),
            environment=identity.environment,
            started_at=start.started_at,
        )
        .on_conflict_do_nothing()
        .returning(rfp_approval_runs.c.approval_run_id)
    ).scalar_one_or_none()
    if created is None:
        concurrent = session.execute(
            select(rfp_approval_runs).where(
                rfp_approval_runs.c.start_idempotency_key == idempotency_key
            )
        ).mappings().one_or_none()
        if concurrent is None:
            raise ApprovalStoreError("approval_run_conflict")
        row = dict(concurrent)
        return _authorized_start_replay(
            session,
            row=row,
            start=start,
            request_hash=request_hash,
            identity=identity,
            reference=authorization_reference,
        )

    for revision in initial_revisions:
        session.execute(
            insert(rfp_approval_department_revisions).values(
                revision_id=revision.revision_id,
                approval_run_id=start.approval_run_id,
                ticket_id=start.ticket_id,
                response_run_id=start.response_run_id,
                department_id=revision.department.value,
                review_revision_no=revision.review_revision_no,
                source_part2_draft_id=revision.source_part2_draft_id,
                source_part2_draft_hash=revision.source_part2_draft_hash,
                source_part2_evaluation_id=revision.source_part2_evaluation_id,
                source_part2_evaluation_hash=revision.source_part2_evaluation_hash,
                source_part2_iteration=revision.source_part2_iteration,
                supersedes_revision_id=None,
                superseded_by_revision_id=None,
                content_payload=canonical_value(
                    {
                        "contract_version": revision.contract_version,
                        "draft_id": revision.draft_id,
                        "content": revision.content,
                        "commercial_terms": revision.commercial_terms,
                        "pending_commercial_items": revision.pending_commercial_items,
                        "evidence": revision.evidence,
                        "creation_reason": revision.creation_reason,
                    }
                ),
                content_hash=revision.content_hash,
                validation_payload=canonical_value(revision.validation),
                validation_hash=revision.validation.references.validation_hash,
                evaluated_content_hash=revision.content_hash,
                status=revision.status,
                is_current=True,
                state_version=0,
                applied_decision_id=None,
                created_by_principal_id=None,
                created_at=revision.created_at,
            )
        )

    changed = session.execute(
        update(rfp_tickets)
        .where(
            rfp_tickets.c.ticket_id == start.ticket_id,
            rfp_tickets.c.status == expected_ticket_status,
        )
        .values(status="esperando_aprobación", updated_at=func.clock_timestamp())
    ).rowcount
    if changed != 1:
        raise ApprovalStoreError("ticket_compare_and_set_failed")
    _insert_event(
        session,
        event_key=f"approval-run-started:{start.approval_run_id}:{request_hash}",
        approval_run_id=start.approval_run_id,
        ticket_id=start.ticket_id,
        thread_id=f"approval-run:{start.approval_run_id}",
        execution_id=execution_id,
        sequence_no=1,
        node_name="start_approval_run",
        event_type="approval_run_started",
        safe_input_ref=_safe_ref(
            "run_start_input",
            response_run_id=start.response_run_id,
            response_handoff_version=start.response_handoff_version,
            response_handoff_hash=start.response_handoff_hash,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            active_departments=[item.value for item in start.active_departments],
            automate_proposal_materialization=getattr(
                start, "automate_proposal_materialization", False
            ),
        ),
        safe_output_ref=_safe_ref(
            "run_start_output",
            approval_run_id=start.approval_run_id,
            status="active",
            state_version=0,
        ),
        result="created",
        from_state=expected_ticket_status,
        to_state="esperando_aprobación",
        identity=identity,
        authorization=authorization,
    )
    return ApprovalRunRecord(
        approval_run_id=start.approval_run_id,
        ticket_id=start.ticket_id,
        response_run_id=start.response_run_id,
        status="active",
        state_version=0,
        replayed=False,
    )


def link_initial_validation_bundle(
    session: Session,
    *,
    approval_run_id: UUID,
    ticket_id: UUID,
    revision_id: UUID,
    validation_bundle_id: UUID,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    permission: Literal["start_approval_run", "manage_approval_run"],
    execution_id: UUID,
) -> InitialValidationLinkRecord:
    """Atomically make one imported revision reviewable by linking its bundle.

    The approval run, artifacts and bundle may all have been inserted earlier in
    the caller's still-open transaction.  This operation validates their exact
    identity and hashes before exposing the revision to the HITL graph.  It is
    replay-safe and never commits independently.
    """

    if not _is_resolved_identity(identity):
        raise ApprovalStoreError("unresolved_identity")
    run = session.execute(
        select(rfp_approval_runs)
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
            rfp_approval_runs.c.environment == identity.environment,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if run is None:
        raise ApprovalStoreError("approval_run_not_found_or_environment_mismatch")
    if run["status"] != "active":
        raise ApprovalStoreError("approval_run_not_active")
    revision_rows = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.approval_run_id == approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == ticket_id,
            rfp_approval_department_revisions.c.is_current.is_(True),
        )
        .order_by(rfp_approval_department_revisions.c.department_id)
        .with_for_update()
    ).mappings().all()
    revision = next(
        (row for row in revision_rows if row["revision_id"] == revision_id), None
    )
    if revision is None:
        raise ApprovalStoreError("initial_revision_not_found")
    if (
        revision["review_revision_no"] != 1
        or _persisted_revision_creation_reason(revision) != "part2_import"
        or not revision["is_current"]
        or revision["applied_decision_id"] is not None
    ):
        raise ApprovalStoreError("initial_revision_not_linkable")
    expected_hashes = {
        row["department_id"]: row["content_hash"] for row in revision_rows
    }
    assessments = session.execute(
        select(
            rfp_conflict_assessments.c.trigger_id,
            rfp_conflict_assessments.c.assessed_content_hashes,
        ).where(
            rfp_conflict_assessments.c.approval_run_id == approval_run_id,
            rfp_conflict_assessments.c.ticket_id == ticket_id,
            rfp_conflict_assessments.c.outcome == "no_conflict",
        )
    ).mappings().all()
    cleared: set[str] = set()
    for assessment in assessments:
        assessed = _deserialize_assessed_content_hashes(
            assessment["assessed_content_hashes"]
        )
        if all(assessed.get(scope) == value for scope, value in expected_hashes.items()):
            cleared.add(assessment["trigger_id"])
    required_triggers = {
        "volume-vs-capacity",
        "returns-sla-breach",
        "currency-mismatch",
    }
    if cleared != required_triggers:
        raise ApprovalStoreError("preapproval_conflict_clearance_required")
    unresolved_conflict = session.execute(
        select(rfp_arbitration_conflicts.c.conflict_id).where(
            rfp_arbitration_conflicts.c.approval_run_id == approval_run_id,
            rfp_arbitration_conflicts.c.ticket_id == ticket_id,
            rfp_arbitration_conflicts.c.status.in_(["open", "changes_requested"]),
        ).limit(1)
    ).scalar_one_or_none()
    if unresolved_conflict is not None:
        raise ApprovalStoreError("preapproval_conflict_unresolved")
    authorization = _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(permission=permission),
        reference=authorization_reference,
    )
    bundle = session.execute(
        select(rfp_approval_validation_bundles)
        .where(
            rfp_approval_validation_bundles.c.validation_bundle_id
            == validation_bundle_id,
            rfp_approval_validation_bundles.c.approval_run_id == approval_run_id,
            rfp_approval_validation_bundles.c.ticket_id == ticket_id,
            rfp_approval_validation_bundles.c.scope_key == revision["department_id"],
            rfp_approval_validation_bundles.c.content_hash
            == revision["content_hash"],
            rfp_approval_validation_bundles.c.revision_validation_hash
            == revision["validation_hash"],
            rfp_approval_validation_bundles.c.eligible.is_(True),
        )
        .with_for_update(key_share=True)
    ).mappings().one_or_none()
    if bundle is None:
        raise ApprovalStoreError("initial_validation_bundle_mismatch")
    linked = revision["validation_bundle_id"]
    if linked is not None and linked != validation_bundle_id:
        raise ApprovalStoreError("initial_validation_bundle_collision")
    replayed = linked == validation_bundle_id
    if (not replayed and revision["status"] != "pending_assessment") or (
        replayed and revision["status"] != "pending_approval"
    ):
        raise ApprovalStoreError("initial_revision_not_linkable")
    if not replayed:
        changed = session.execute(
            update(rfp_approval_department_revisions)
            .where(
                rfp_approval_department_revisions.c.revision_id == revision_id,
                rfp_approval_department_revisions.c.approval_run_id
                == approval_run_id,
                rfp_approval_department_revisions.c.ticket_id == ticket_id,
                rfp_approval_department_revisions.c.validation_bundle_id.is_(None),
                rfp_approval_department_revisions.c.status == "pending_assessment",
                rfp_approval_department_revisions.c.state_version
                == revision["state_version"],
            )
            .values(
                validation_bundle_id=validation_bundle_id,
                status="pending_approval",
                state_version=rfp_approval_department_revisions.c.state_version + 1,
            )
        ).rowcount
        if changed != 1:
            raise ApprovalStoreError("initial_validation_bundle_compare_and_set_failed")
    thread_id = (
        f"approval-run:{approval_run_id}:department:{revision['department_id']}:"
        f"revision:{revision_id}"
    )
    _insert_event(
        session,
        event_key=f"initial-validation-linked:{revision_id}:{validation_bundle_id}",
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        thread_id=thread_id,
        execution_id=execution_id,
        sequence_no=1,
        node_name="link_initial_validation_bundle",
        event_type="initial_validation_bundle_linked",
        safe_input_ref=_safe_ref(
            "initial_validation_link_input",
            revision_id=revision_id,
            validation_bundle_id=validation_bundle_id,
            content_hash=revision["content_hash"],
            validation_hash=revision["validation_hash"],
        ),
        safe_output_ref=_safe_ref(
            "initial_validation_link_output",
            revision_id=revision_id,
            validation_bundle_id=validation_bundle_id,
            status="pending_approval",
        ),
        result="linked",
        from_state="pending_assessment",
        to_state="pending_approval",
        revision_id=revision_id,
        identity=identity,
        authorization=authorization,
    )
    return InitialValidationLinkRecord(
        approval_run_id=approval_run_id,
        revision_id=revision_id,
        validation_bundle_id=validation_bundle_id,
        replayed=replayed,
    )


def _assert_decision_replay(row: dict[str, Any], command: DecisionApplicationCommand) -> None:
    decision = command.decision
    if (
        row["decision_id"] != decision.decision_id
        or row["revision_id"] != decision.revision_id
        or row["department_id"] != decision.department.value
        or row["expected_content_hash"] != decision.expected_content_hash
        or row["action"] != decision.action
        or row["reason"] != decision.reason
        or row["application_key"] != command.application_key
    ):
        raise ApprovalStoreError("decision_idempotency_key_reused")


def _decision_record(row: dict[str, Any], *, replayed: bool) -> DecisionRecord:
    return DecisionRecord(
        decision_id=row["decision_id"],
        approval_run_id=row["approval_run_id"],
        revision_id=row["revision_id"],
        processing_status=row["processing_status"],
        application_key=row["application_key"],
        replayed=replayed,
    )


def _authorize_persisted_decision(
    session: Session,
    *,
    decision: dict[str, Any],
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> EffectiveAuthorization:
    if (
        decision["actor_principal_id"] != identity.principal_id
        or decision["actor_binding_id"] != identity.binding_id
        or decision["environment"] != identity.environment
    ):
        raise ApprovalStoreError("decision_actor_mismatch")
    if (
        decision["authorization_id"] != reference.authorization_id
        or decision["authorization_version"] != reference.authorization_version
    ):
        raise ApprovalStoreError("decision_authorization_reference_mismatch")
    return _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(
            permission="approve_department",
            department_scope=decision["department_id"],
            target_policy="department_exact",
            affected_departments=frozenset({decision["department_id"]}),
        ),
        reference=reference,
    )


def _authorized_decision_replay(
    session: Session,
    *,
    row: dict[str, Any],
    command: DecisionApplicationCommand,
    identity: ResolvedIdentity,
    reference: AuthorizationReference,
) -> DecisionRecord:
    """Lock in the normal order, authorize access, and perform no mutation."""

    _assert_decision_replay(row, command)
    session.execute(
        select(rfp_approval_runs.c.approval_run_id)
        .where(rfp_approval_runs.c.approval_run_id == row["approval_run_id"])
        .with_for_update(key_share=True)
    ).scalar_one()
    session.execute(
        select(rfp_approval_department_revisions.c.revision_id)
        .where(
            rfp_approval_department_revisions.c.revision_id == row["revision_id"],
            rfp_approval_department_revisions.c.approval_run_id
            == row["approval_run_id"],
        )
        .with_for_update(key_share=True)
    ).scalar_one()
    locked = session.execute(
        select(rfp_human_decisions)
        .where(rfp_human_decisions.c.decision_id == row["decision_id"])
        .with_for_update()
    ).mappings().one()
    persisted = dict(locked)
    _assert_decision_replay(persisted, command)
    _authorize_persisted_decision(
        session, decision=persisted, identity=identity, reference=reference
    )
    return _decision_record(persisted, replayed=True)


def register_human_decision(
    session: Session,
    *,
    command: DecisionApplicationCommand,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    execution_id: UUID,
) -> DecisionRecord:
    """Durably enqueue one exact, authorized human decision.

    The application key binds the declared preparation and interrupt envelope,
    but this gate has no persisted LangGraph preparation/interrupt record.  API
    approval must remain disabled until a later HITL gate can prove that link.
    """

    decision = command.decision
    _assert_identity_matches_principal(identity, decision.actor.principal)
    if decision.actor.authorization_id != authorization_reference.authorization_id:
        raise ApprovalStoreError("decision_authorization_reference_mismatch")
    existing = session.execute(
        select(rfp_human_decisions).where(
            rfp_human_decisions.c.approval_run_id == decision.approval_run_id,
            rfp_human_decisions.c.idempotency_key == decision.idempotency_key,
        )
    ).mappings().one_or_none()
    if existing is not None:
        row = dict(existing)
        return _authorized_decision_replay(
            session,
            row=row,
            command=command,
            identity=identity,
            reference=authorization_reference,
        )

    run = session.execute(
        select(rfp_approval_runs.c.status)
        .where(
            rfp_approval_runs.c.approval_run_id == decision.approval_run_id,
            rfp_approval_runs.c.ticket_id == decision.ticket_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if run != "active":
        raise ApprovalStoreError("approval_run_not_active")
    revision_row = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id == decision.revision_id,
            rfp_approval_department_revisions.c.approval_run_id
            == decision.approval_run_id,
            rfp_approval_department_revisions.c.ticket_id == decision.ticket_id,
            rfp_approval_department_revisions.c.department_id
            == decision.department.value,
        )
        .with_for_update()
    ).mappings().one_or_none()
    if revision_row is None:
        raise ApprovalStoreError("revision_not_found")
    revision = dict(revision_row)
    if not revision["is_current"]:
        raise ApprovalStoreError("revision_obsolete")
    if revision["status"] != "pending_approval":
        raise ApprovalStoreError("revision_not_pending_approval")
    if revision["content_hash"] != decision.expected_content_hash:
        raise ApprovalStoreError("revision_content_hash_mismatch")
    if revision["validation_payload"].get("eligible_for_approval") is not True:
        raise ApprovalStoreError("revision_not_eligible")
    content_payload = revision.get("content_payload")
    pending_items = (
        content_payload.get("pending_commercial_items", [])
        if isinstance(content_payload, dict)
        else []
    )
    pending_codes = [
        item["code"]
        for item in pending_items
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    ]
    if (
        decision.action == "approve"
        and pending_codes
        and not acknowledges_indicative_commercial_items(
            decision.reason, pending_codes
        )
    ):
        raise ApprovalStoreError("indicative_commercial_items_not_acknowledged")

    authorization = _authorize_after_locks(
        session,
        identity=identity,
        target=AuthorizationTarget(
            permission="approve_department",
            department_scope=decision.department.value,
            target_policy="department_exact",
            affected_departments=frozenset({decision.department.value}),
        ),
        reference=authorization_reference,
    )
    created = session.execute(
        pg_insert(rfp_human_decisions)
        .values(
            decision_id=decision.decision_id,
            approval_run_id=decision.approval_run_id,
            ticket_id=decision.ticket_id,
            revision_id=decision.revision_id,
            department_id=decision.department.value,
            idempotency_key=decision.idempotency_key,
            application_key=command.application_key,
            expected_content_hash=decision.expected_content_hash,
            action=decision.action,
            reason=decision.reason,
            processing_status="queued",
            actor_principal_id=identity.principal_id,
            actor_binding_id=identity.binding_id,
            authorization_id=authorization.authorization_id,
            authorization_version=authorization.authorization_version,
            environment=identity.environment,
            claim_epoch=0,
            decided_at=decision.decided_at,
        )
        .on_conflict_do_nothing()
        .returning(rfp_human_decisions.c.decision_id)
    ).scalar_one_or_none()
    if created is None:
        concurrent = session.execute(
            select(rfp_human_decisions).where(
                rfp_human_decisions.c.approval_run_id == decision.approval_run_id,
                rfp_human_decisions.c.idempotency_key == decision.idempotency_key,
            )
        ).mappings().one_or_none()
        if concurrent is None:
            raise ApprovalStoreError("decision_conflict")
        row = dict(concurrent)
        return _authorized_decision_replay(
            session,
            row=row,
            command=command,
            identity=identity,
            reference=authorization_reference,
        )

    _insert_event(
        session,
        event_key=f"decision-queued:{decision.decision_id}:{command.application_key}",
        approval_run_id=decision.approval_run_id,
        ticket_id=decision.ticket_id,
        thread_id=f"decision:{decision.decision_id}",
        execution_id=execution_id,
        sequence_no=1,
        node_name="register_human_decision",
        event_type="human_decision_queued",
        safe_input_ref=_safe_ref(
            "decision_input",
            decision_id=decision.decision_id,
            revision_id=decision.revision_id,
            department_id=decision.department.value,
            expected_content_hash=decision.expected_content_hash,
            action=decision.action,
            idempotency_key=decision.idempotency_key,
            request_fingerprint=command.application_key,
        ),
        safe_output_ref=_safe_ref(
            "decision_output",
            decision_id=decision.decision_id,
            processing_status="queued",
            revision_state_version=revision["state_version"],
        ),
        result="queued",
        from_state="pending_approval",
        to_state="pending_approval",
        revision_id=decision.revision_id,
        decision_id=decision.decision_id,
        identity=identity,
        authorization=authorization,
    )
    return DecisionRecord(
        decision_id=decision.decision_id,
        approval_run_id=decision.approval_run_id,
        revision_id=decision.revision_id,
        processing_status="queued",
        application_key=command.application_key,
        replayed=False,
    )


def _claim_candidate(
    session: Session,
    *,
    candidate: Any,
    execution_id: UUID,
    lease_seconds: int,
    claim_token: UUID | None,
) -> DecisionClaim | None:
    token = claim_token or uuid4()
    lease_expression = func.clock_timestamp() + text(
        "(:lease_seconds * interval '1 second')"
    )
    claimed = session.execute(
        update(rfp_human_decisions)
        .where(
            rfp_human_decisions.c.decision_id
            == select(candidate.c.decision_id).scalar_subquery()
        )
        .values(
            processing_status="claimed",
            claim_token=token,
            claim_epoch=rfp_human_decisions.c.claim_epoch + 1,
            claimed_at=func.clock_timestamp(),
            lease_until=lease_expression,
        )
        .returning(rfp_human_decisions),
        {"lease_seconds": lease_seconds},
    ).mappings().one_or_none()
    if claimed is None:
        return None
    row = dict(claimed)
    revision_version = _registered_revision_state_version(
        session, row["decision_id"], row["application_key"]
    )
    _insert_event(
        session,
        event_key=f"decision-claimed:{row['decision_id']}:{row['claim_epoch']}",
        approval_run_id=row["approval_run_id"],
        ticket_id=row["ticket_id"],
        thread_id=f"decision:{row['decision_id']}",
        execution_id=execution_id,
        sequence_no=2 * row["claim_epoch"],
        node_name="claim_human_decision",
        event_type="human_decision_claimed",
        safe_input_ref=_safe_ref(
            "claim_input",
            decision_id=row["decision_id"],
            previous_epoch=row["claim_epoch"] - 1,
        ),
        safe_output_ref=_safe_ref(
            "claim_output",
            decision_id=row["decision_id"],
            claim_epoch=row["claim_epoch"],
            lease_until=row["lease_until"],
            revision_state_version=revision_version,
        ),
        result="claimed",
        from_state="queued_or_expired_claim",
        to_state="claimed",
        revision_id=row["revision_id"],
        decision_id=row["decision_id"],
    )
    return DecisionClaim(
        decision_id=row["decision_id"],
        approval_run_id=row["approval_run_id"],
        revision_id=row["revision_id"],
        claim_token=token,
        claim_epoch=row["claim_epoch"],
        lease_until=row["lease_until"],
        expected_revision_state_version=revision_version,
    )


def _claimable_decision_query():
    return select(rfp_human_decisions.c.decision_id).where(
        or_(
            rfp_human_decisions.c.processing_status == "queued",
            (
                (rfp_human_decisions.c.processing_status == "claimed")
                & (rfp_human_decisions.c.lease_until <= func.clock_timestamp())
            ),
        )
    )


def claim_next_decision(
    session: Session,
    *,
    execution_id: UUID,
    lease_seconds: int = 60,
    claim_token: UUID | None = None,
) -> DecisionClaim | None:
    """Claim the oldest queued or expired decision using SKIP LOCKED."""

    if lease_seconds < 1 or lease_seconds > 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    candidate = (
        _claimable_decision_query()
        .order_by(rfp_human_decisions.c.created_at, rfp_human_decisions.c.decision_id)
        .with_for_update(skip_locked=True)
        .limit(1)
        .cte("claimable_decision")
    )
    return _claim_candidate(
        session,
        candidate=candidate,
        execution_id=execution_id,
        lease_seconds=lease_seconds,
        claim_token=claim_token,
    )


def claim_decision_by_id(
    session: Session,
    *,
    decision_id: UUID,
    execution_id: UUID,
    lease_seconds: int = 60,
    claim_token: UUID | None = None,
) -> DecisionClaim | None:
    """Claim exactly one queued or expired decision without touching another row."""

    if lease_seconds < 1 or lease_seconds > 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    candidate = (
        _claimable_decision_query()
        .where(rfp_human_decisions.c.decision_id == decision_id)
        .with_for_update(skip_locked=True)
        .limit(1)
        .cte("claimable_decision")
    )
    return _claim_candidate(
        session,
        candidate=candidate,
        execution_id=execution_id,
        lease_seconds=lease_seconds,
        claim_token=claim_token,
    )


def _load_locked_application_rows(
    session: Session, decision_id: UUID
) -> tuple[dict[str, Any], dict[str, Any], str]:
    locator = session.execute(
        select(
            rfp_human_decisions.c.approval_run_id,
            rfp_human_decisions.c.revision_id,
        ).where(rfp_human_decisions.c.decision_id == decision_id)
    ).mappings().one_or_none()
    if locator is None:
        raise ApprovalStoreError("decision_not_found")
    run_status = session.execute(
        select(rfp_approval_runs.c.status)
        .where(rfp_approval_runs.c.approval_run_id == locator["approval_run_id"])
        .with_for_update(key_share=True)
    ).scalar_one()
    revision = session.execute(
        select(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id == locator["revision_id"],
            rfp_approval_department_revisions.c.approval_run_id
            == locator["approval_run_id"],
        )
        .with_for_update(key_share=True)
    ).mappings().one()
    decision = session.execute(
        select(rfp_human_decisions)
        .where(rfp_human_decisions.c.decision_id == decision_id)
        .with_for_update()
    ).mappings().one()
    return dict(decision), dict(revision), run_status


def _registered_revision_state_version(
    session: Session, decision_id: UUID, application_key: str
) -> int:
    event_key = f"decision-queued:{decision_id}:{application_key}"
    version = session.execute(
        select(
            rfp_workflow_events.c.safe_output_ref["revision_state_version"].as_integer()
        ).where(
            rfp_workflow_events.c.decision_id == decision_id,
            rfp_workflow_events.c.event_type == "human_decision_queued",
            rfp_workflow_events.c.event_key == event_key,
        )
    ).scalar_one_or_none()
    if version is None:
        raise ApprovalStoreError("decision_registration_event_missing")
    return version


def apply_claimed_decision(
    session: Session,
    *,
    decision_id: UUID,
    claim_token: UUID,
    claim_epoch: int,
    expected_revision_state_version: int,
    identity: ResolvedIdentity,
    authorization_reference: AuthorizationReference,
    execution_id: UUID,
) -> DecisionApplicationResult:
    """Apply an authorized decision with locks, fencing and a revision CAS."""

    decision, revision, run_status = _load_locked_application_rows(session, decision_id)
    target_status = {
        "approve": "approved",
        "reject": "rejected",
        "request_changes": "changes_requested",
    }[decision["action"]]
    if decision["processing_status"] == "applied":
        if (
            revision["applied_decision_id"] == decision_id
            and revision["status"] == target_status
        ):
            _authorize_persisted_decision(
                session,
                decision=decision,
                identity=identity,
                reference=authorization_reference,
            )
            return DecisionApplicationResult(
                decision_id=decision_id,
                revision_id=decision["revision_id"],
                action=decision["action"],
                processing_status="applied",
                revision_status=revision["status"],
                revision_state_version=revision["state_version"],
                replayed=True,
            )
        raise ApprovalStoreError("applied_decision_inconsistent")
    if decision["processing_status"] == "rejected_stale":
        _authorize_persisted_decision(
            session,
            decision=decision,
            identity=identity,
            reference=authorization_reference,
        )
        return DecisionApplicationResult(
            decision_id=decision_id,
            revision_id=decision["revision_id"],
            action=decision["action"],
            processing_status="rejected_stale",
            revision_status=revision["status"],
            revision_state_version=revision["state_version"],
            replayed=True,
        )
    if run_status != "active":
        raise ApprovalStoreError("approval_run_not_active")
    if decision["processing_status"] != "claimed":
        raise ApprovalStoreError("decision_not_claimed")
    if decision["claim_token"] != claim_token:
        raise ApprovalStoreError("claim_token_mismatch")
    if decision["claim_epoch"] != claim_epoch:
        raise ApprovalStoreError("claim_epoch_mismatch")
    registered_version = _registered_revision_state_version(
        session, decision_id, decision["application_key"]
    )
    if expected_revision_state_version != registered_version:
        raise ApprovalStoreError("revision_state_version_reference_mismatch")
    stale = (
        not revision["is_current"]
        or revision["status"] != "pending_approval"
        or revision["content_hash"] != decision["expected_content_hash"]
        or revision["state_version"] != registered_version
        or revision["validation_payload"].get("eligible_for_approval") is not True
    )
    if stale:
        lease_active = session.execute(
            select(decision["lease_until"] > func.clock_timestamp())
        ).scalar_one()
        if not lease_active:
            raise ApprovalStoreError("claim_lease_expired")
        changed = session.execute(
            update(rfp_human_decisions)
            .where(
                rfp_human_decisions.c.decision_id == decision_id,
                rfp_human_decisions.c.processing_status == "claimed",
                rfp_human_decisions.c.claim_token == claim_token,
                rfp_human_decisions.c.claim_epoch == claim_epoch,
                rfp_human_decisions.c.lease_until > func.clock_timestamp(),
            )
            .values(
                processing_status="rejected_stale",
                claim_token=None,
                lease_until=None,
                applied_at=func.clock_timestamp(),
            )
        ).rowcount
        if changed != 1:
            raise ApprovalStoreError("decision_fence_lost")
        _insert_event(
            session,
            event_key=f"decision-stale:{decision_id}:{claim_epoch}",
            approval_run_id=decision["approval_run_id"],
            ticket_id=decision["ticket_id"],
            thread_id=f"decision:{decision_id}",
            execution_id=execution_id,
            sequence_no=2 * claim_epoch + 1,
            node_name="apply_human_decision",
            event_type="human_decision_rejected_stale",
            safe_input_ref=_safe_ref(
                "application_input",
                decision_id=decision_id,
                revision_id=decision["revision_id"],
                expected_content_hash=decision["expected_content_hash"],
                action=decision["action"],
                claim_epoch=claim_epoch,
                expected_revision_state_version=expected_revision_state_version,
            ),
            safe_output_ref=_safe_ref(
                "application_output",
                decision_id=decision_id,
                processing_status="rejected_stale",
                revision_status=revision["status"],
                state_version=revision["state_version"],
            ),
            result="rejected_stale",
            from_state=revision["status"],
            to_state=revision["status"],
            revision_id=decision["revision_id"],
            decision_id=decision_id,
        )
        return DecisionApplicationResult(
            decision_id=decision_id,
            revision_id=decision["revision_id"],
            action=decision["action"],
            processing_status="rejected_stale",
            revision_status=revision["status"],
            revision_state_version=revision["state_version"],
            replayed=False,
        )

    authorization = _authorize_persisted_decision(
        session,
        decision=decision,
        identity=identity,
        reference=authorization_reference,
    )
    lease_active = session.execute(
        select(decision["lease_until"] > func.clock_timestamp())
    ).scalar_one()
    if not lease_active:
        raise ApprovalStoreError("claim_lease_expired")

    revision_changed = session.execute(
        update(rfp_approval_department_revisions)
        .where(
            rfp_approval_department_revisions.c.revision_id == decision["revision_id"],
            rfp_approval_department_revisions.c.approval_run_id
            == decision["approval_run_id"],
            rfp_approval_department_revisions.c.is_current.is_(True),
            rfp_approval_department_revisions.c.status == "pending_approval",
            rfp_approval_department_revisions.c.content_hash
            == decision["expected_content_hash"],
            rfp_approval_department_revisions.c.state_version
            == registered_version,
            rfp_approval_department_revisions.c.applied_decision_id.is_(None),
        )
        .values(
            status=target_status,
            applied_decision_id=decision_id,
            state_version=rfp_approval_department_revisions.c.state_version + 1,
        )
    ).rowcount
    if revision_changed != 1:
        raise ApprovalStoreError("revision_compare_and_set_failed")
    decision_changed = session.execute(
        update(rfp_human_decisions)
        .where(
            rfp_human_decisions.c.decision_id == decision_id,
            rfp_human_decisions.c.processing_status == "claimed",
            rfp_human_decisions.c.claim_token == claim_token,
            rfp_human_decisions.c.claim_epoch == claim_epoch,
            rfp_human_decisions.c.lease_until > func.clock_timestamp(),
        )
        .values(
            processing_status="applied",
            claim_token=None,
            lease_until=None,
            applied_at=func.clock_timestamp(),
        )
    ).rowcount
    if decision_changed != 1:
        raise ApprovalStoreError("decision_fence_lost")
    new_version = registered_version + 1
    _insert_event(
        session,
        event_key=f"decision-applied:{decision_id}:{claim_epoch}",
        approval_run_id=decision["approval_run_id"],
        ticket_id=decision["ticket_id"],
        thread_id=f"decision:{decision_id}",
        execution_id=execution_id,
        sequence_no=2 * claim_epoch + 1,
        node_name="apply_human_decision",
        event_type="human_decision_applied",
        safe_input_ref=_safe_ref(
            "application_input",
            decision_id=decision_id,
            revision_id=decision["revision_id"],
            expected_content_hash=decision["expected_content_hash"],
            action=decision["action"],
            claim_epoch=claim_epoch,
            expected_revision_state_version=expected_revision_state_version,
        ),
        safe_output_ref=_safe_ref(
            "application_output",
            decision_id=decision_id,
            processing_status="applied",
            revision_status=target_status,
            state_version=new_version,
        ),
        result="applied",
        from_state="pending_approval",
        to_state=target_status,
        revision_id=decision["revision_id"],
        decision_id=decision_id,
        identity=identity,
        authorization=authorization,
    )
    return DecisionApplicationResult(
        decision_id=decision_id,
        revision_id=decision["revision_id"],
        action=decision["action"],
        processing_status="applied",
        revision_status=target_status,
        revision_state_version=new_version,
        replayed=False,
    )
