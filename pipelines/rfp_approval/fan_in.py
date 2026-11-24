"""Fail-closed fan-in readiness reconstructed from PostgreSQL state.

This module does not synthesize a document and never transitions a ticket to
``terminado``.  It provides the read-side gate that a later finalization
transaction must run immediately before persisting the final document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import and_, select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import (
    acknowledges_indicative_commercial_items,
    indicative_warnings_are_reviewable,
)
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_arbitration_conflicts,
    rfp_human_decisions,
)


_DEPARTMENTS = tuple(DepartmentId)
_BLOCKING_REVISION_STATUSES = {
    "rejected",
    "changes_requested",
    "blocked_conflict",
    "blocked_compliance",
    "blocked_input",
    "limit_reached",
}
_OPEN_CONFLICT_STATUSES = {"open", "changes_requested"}


@dataclass(frozen=True, slots=True)
class ApprovedDepartmentRevision:
    department: DepartmentId
    revision_id: UUID
    decision_id: UUID


@dataclass(frozen=True, slots=True)
class FanInReadiness:
    """Immutable result of one persisted fan-in snapshot."""

    ready_for_synthesis: bool
    blockers: tuple[str, ...]
    approved_revisions: tuple[ApprovedDepartmentRevision, ...]
    proposal_revision_id: UUID | None
    required_ticket_status: str = "esperando_aprobación"


def _fan_in_query(*, approval_run_id: UUID, ticket_id: UUID):
    revision = rfp_approval_department_revisions
    decision = rfp_human_decisions
    proposal = rfp_approval_proposal_revisions
    conflict = rfp_arbitration_conflicts

    return (
        select(
            rfp_approval_runs.c.approval_run_id.label("run_id"),
            rfp_approval_runs.c.ticket_id.label("run_ticket_id"),
            rfp_approval_runs.c.status.label("run_status"),
            rfp_approval_runs.c.active_departments,
            revision.c.revision_id,
            revision.c.department_id,
            revision.c.status.label("revision_status"),
            revision.c.is_current,
            revision.c.superseded_by_revision_id,
            revision.c.content_hash.label("revision_content_hash"),
            revision.c.content_payload.label("revision_content_payload"),
            revision.c.validation_payload.label("revision_validation_payload"),
            revision.c.applied_decision_id,
            decision.c.decision_id,
            decision.c.revision_id.label("decision_revision_id"),
            decision.c.department_id.label("decision_department_id"),
            decision.c.expected_content_hash.label("decision_content_hash"),
            decision.c.action.label("decision_action"),
            decision.c.reason.label("decision_reason"),
            decision.c.processing_status.label("decision_processing_status"),
            proposal.c.proposal_revision_id,
            proposal.c.is_current.label("proposal_is_current"),
            proposal.c.superseded_by_revision_id.label(
                "proposal_superseded_by_revision_id"
            ),
            proposal.c.warehouse_revision_id,
            proposal.c.lastmile_revision_id,
            proposal.c.reverse_revision_id,
            proposal.c.content_payload.label("proposal_content_payload"),
            proposal.c.validation_payload.label("proposal_validation_payload"),
            proposal.c.eligible_for_synthesis,
            conflict.c.conflict_id,
            conflict.c.status.label("conflict_status"),
        )
        .select_from(
            rfp_approval_runs.outerjoin(
                revision,
                and_(
                    revision.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    revision.c.ticket_id == rfp_approval_runs.c.ticket_id,
                    revision.c.is_current.is_(True),
                ),
            )
            .outerjoin(
                decision,
                and_(
                    decision.c.decision_id == revision.c.applied_decision_id,
                    decision.c.revision_id == revision.c.revision_id,
                    decision.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    decision.c.ticket_id == rfp_approval_runs.c.ticket_id,
                    decision.c.department_id == revision.c.department_id,
                ),
            )
            .outerjoin(
                proposal,
                and_(
                    proposal.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    proposal.c.ticket_id == rfp_approval_runs.c.ticket_id,
                    proposal.c.is_current.is_(True),
                ),
            )
            .outerjoin(
                conflict,
                and_(
                    conflict.c.approval_run_id
                    == rfp_approval_runs.c.approval_run_id,
                    conflict.c.ticket_id == rfp_approval_runs.c.ticket_id,
                ),
            )
        )
        .where(
            rfp_approval_runs.c.approval_run_id == approval_run_id,
            rfp_approval_runs.c.ticket_id == ticket_id,
        )
    )


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _uuid_set(value: Any) -> set[UUID]:
    try:
        return {item if isinstance(item, UUID) else UUID(str(item)) for item in _list(value)}
    except (TypeError, ValueError, AttributeError):
        return set()


def _valid_volume_discount_row(value: Any) -> bool:
    """Validate the publication shape without inventing commercial terms."""

    row = _dict(value)
    if not row or not isinstance(row.get("condition"), str):
        return False
    evidence = _list(row.get("evidence"))
    if not evidence:
        return False
    status = row.get("status")
    percentage = row.get("discount_percentage")
    if status == "pending":
        return percentage is None
    if status != "documented" or isinstance(percentage, bool):
        return False
    return isinstance(percentage, (int, float)) and 0 <= percentage <= 100


def evaluate_fan_in_rows(rows: Iterable[Mapping[str, Any]]) -> FanInReadiness:
    """Evaluate rows returned by the single-statement PostgreSQL snapshot."""

    materialized = list(rows)
    if not materialized:
        return FanInReadiness(False, ("approval_run_not_found",), (), None)

    blockers: set[str] = set()
    first = materialized[0]
    if first["run_status"] not in {"active", "finalizing"}:
        blockers.add("approval_run_not_synthesizable")

    raw_active = _list(first["active_departments"])
    try:
        active = tuple(DepartmentId(item) for item in raw_active)
    except (TypeError, ValueError):
        active = ()
        blockers.add("active_departments_invalid")
    if not active or len(active) != len(set(active)):
        blockers.add("active_departments_invalid")
    if any(item not in _DEPARTMENTS for item in active):
        blockers.add("active_departments_invalid")

    revisions: dict[UUID, Mapping[str, Any]] = {}
    conflicts: dict[UUID, str] = {}
    proposals: dict[UUID, Mapping[str, Any]] = {}
    for row in materialized:
        if row["revision_id"] is not None:
            revisions[row["revision_id"]] = row
        if row["conflict_id"] is not None:
            conflicts[row["conflict_id"]] = row["conflict_status"]
        if row["proposal_revision_id"] is not None:
            proposals[row["proposal_revision_id"]] = row

    by_department: dict[DepartmentId, list[Mapping[str, Any]]] = {
        department: [] for department in active
    }
    for row in revisions.values():
        try:
            department = DepartmentId(row["department_id"])
        except (TypeError, ValueError):
            blockers.add("unexpected_current_department")
            continue
        if department not in by_department:
            blockers.add("unexpected_current_department")
            continue
        by_department[department].append(row)

    approved: list[ApprovedDepartmentRevision] = []
    for department in active:
        department_rows = by_department.get(department, [])
        if len(department_rows) != 1:
            blockers.add(f"current_revision_count:{department.value}")
            continue
        row = department_rows[0]
        status = row["revision_status"]
        if status in _BLOCKING_REVISION_STATUSES or status != "approved":
            blockers.add(f"department_not_approved:{department.value}:{status}")
        if not row["is_current"] or row["superseded_by_revision_id"] is not None:
            blockers.add(f"department_revision_obsolete:{department.value}")

        content = _dict(row["revision_content_payload"])
        validation = _dict(row["revision_validation_payload"])
        pending_items = [
            _dict(item) for item in _list(content.get("pending_commercial_items"))
        ]
        pending_codes = [
            item["code"] for item in pending_items if isinstance(item.get("code"), str)
        ]
        department_validation_valid = (
            validation.get("eligible_for_approval") is True
            and validation.get("mandatory_compliance_passed") is True
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=_list(validation.get("blocking_rule_ids")),
                unresolved_input_codes=_list(
                    validation.get("unresolved_input_codes")
                ),
            )
            and not _list(validation.get("open_conflict_ids"))
        )
        if not department_validation_valid:
            blockers.add(f"department_validation_ineligible:{department.value}")

        decision_valid = (
            row["applied_decision_id"] is not None
            and row["decision_id"] == row["applied_decision_id"]
            and row["decision_revision_id"] == row["revision_id"]
            and row["decision_department_id"] == department.value
            and row["decision_action"] == "approve"
            and row["decision_processing_status"] == "applied"
            and row["decision_content_hash"] == row["revision_content_hash"]
        )
        pending_are_reviewable = indicative_warnings_are_reviewable(
            blocking_rule_ids=(), unresolved_input_codes=pending_codes
        )
        if pending_codes and not pending_are_reviewable:
            blockers.add(f"commercial_input_pending:{department.value}")
        if not decision_valid:
            blockers.add(f"applied_approval_invalid:{department.value}")
        elif pending_codes and pending_are_reviewable and not acknowledges_indicative_commercial_items(
            row.get("decision_reason"), pending_codes
        ):
            blockers.add(
                f"indicative_commercial_items_not_acknowledged:{department.value}"
            )
        elif status == "approved":
            approved.append(
                ApprovedDepartmentRevision(
                    department=department,
                    revision_id=row["revision_id"],
                    decision_id=row["decision_id"],
                )
            )

    if any(status in _OPEN_CONFLICT_STATUSES for status in conflicts.values()):
        blockers.add("open_arbitration_conflict")

    proposal_id: UUID | None = None
    if len(proposals) != 1:
        blockers.add("current_proposal_count")
    else:
        proposal_id, proposal = next(iter(proposals.items()))
        if (
            not proposal["proposal_is_current"]
            or proposal["proposal_superseded_by_revision_id"] is not None
        ):
            blockers.add("current_proposal_obsolete")

        expected = {item.department.value: item.revision_id for item in approved}
        persisted = {
            "warehouse": proposal["warehouse_revision_id"],
            "lastmile": proposal["lastmile_revision_id"],
            "reverse": proposal["reverse_revision_id"],
        }
        persisted = {key: value for key, value in persisted.items() if value is not None}
        if set(expected) != {item.value for item in active} or persisted != expected:
            blockers.add("proposal_department_sources_mismatch")

        proposal_content = _dict(proposal["proposal_content_payload"])
        content_sources = _uuid_set(
            proposal_content.get("source_department_revision_ids")
        )
        if content_sources != set(expected.values()):
            blockers.add("proposal_content_sources_mismatch")

        currency = _dict(proposal_content.get("currency"))
        sla = _dict(proposal_content.get("sla"))
        discounts = _list(proposal_content.get("volume_discounts"))
        validation = _dict(proposal["proposal_validation_payload"])
        unresolved_input_codes = {
            item for item in _list(validation.get("unresolved_input_codes"))
            if isinstance(item, str)
        }
        commercial_terms_complete = (
            currency.get("status") == "documented"
            and currency.get("value") in {"EUR", "USD"}
            and sla.get("applicability") in {"applicable", "not_applicable"}
            and bool(discounts)
            and all(_valid_volume_discount_row(item) for item in discounts)
        )
        if not commercial_terms_complete:
            blockers.add("global_commercial_terms_incomplete")

        globally_eligible = (
            proposal["eligible_for_synthesis"] is True
            and validation.get("contract_version")
            == "rfp-proposal-revision-validation-v2"
            and validation.get("source_departments_approved") is True
            and validation.get("eligible_for_synthesis") is True
            and validation.get("mandatory_compliance_passed") is True
            and indicative_warnings_are_reviewable(
                blocking_rule_ids=_list(validation.get("blocking_rule_ids")),
                unresolved_input_codes=list(unresolved_input_codes),
            )
            and not _list(validation.get("open_conflict_ids"))
        )
        if not globally_eligible:
            blockers.add("global_validation_ineligible")

    ordered_approved = tuple(
        sorted(approved, key=lambda item: active.index(item.department))
    )
    ordered_blockers = tuple(sorted(blockers))
    return FanInReadiness(
        ready_for_synthesis=not ordered_blockers,
        blockers=ordered_blockers,
        approved_revisions=ordered_approved,
        proposal_revision_id=proposal_id,
    )


def determine_fan_in_readiness(
    session: Session, *, approval_run_id: UUID, ticket_id: UUID
) -> FanInReadiness:
    """Read and evaluate fan-in state with one database statement.

    PostgreSQL gives this statement one consistent MVCC snapshot.  The future
    final-document writer must invoke it in the same finalization transaction
    that locks/rechecks the source rows and persists the document; this read
    alone intentionally makes no closure claim.
    """

    rows = session.execute(
        _fan_in_query(approval_run_id=approval_run_id, ticket_id=ticket_id)
    ).mappings()
    return evaluate_fan_in_rows(rows)
