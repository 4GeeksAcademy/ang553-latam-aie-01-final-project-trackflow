"""Explicit LangGraph arbitration routing for TrackFlow's three fixed triggers."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Callable, Protocol, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.graph import END, START, StateGraph

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import (
    ArbitrationTarget,
    ArbitrationConflict,
    ConflictAssessment,
    CurrencyMismatchFacts,
    ReturnsSLAFacts,
    VolumeCapacityFacts,
    arbitration_conflict_fingerprint,
)
from services.rfp_approval.arbitration import (
    ConflictRecord,
    persist_conflict_assessment,
)
from sqlmodel import Session


class ArbitrationWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("RFP arbitration workflow operation was rejected")
        self.code = code


class ArbitrationState(TypedDict, total=False):
    assessment: dict[str, Any]
    targets: list[dict[str, Any]]
    outcome: str
    resulting_status: str
    required_arbitrators: list[str]
    recommendation: dict[str, Any]
    persistence: dict[str, Any]
    conflict_record: dict[str, Any]
    conflict_records: list[dict[str, Any]]


class ArbitrationPersistenceBackend(Protocol):
    def persist(
        self,
        *,
        approval_run_id: UUID,
        ticket_id: UUID,
        assessment: ConflictAssessment,
        assessed_content_hashes: dict[str, str],
        conflict: ArbitrationConflict | None,
        execution_id: UUID,
    ) -> ConflictRecord: ...


class PostgresArbitrationBackend:
    def __init__(
        self, session_factory: Callable[[], AbstractContextManager[Session]]
    ) -> None:
        self._session_factory = session_factory

    def persist(self, **kwargs) -> ConflictRecord:
        with self._session_factory() as session, session.begin():
            return persist_conflict_assessment(session, **kwargs)


def _authority_conflicts(
    conflict: ArbitrationConflict | None,
) -> list[ArbitrationConflict | None]:
    if conflict is None or not isinstance(conflict.facts, ReturnsSLAFacts):
        return [conflict]
    reverse = [
        target for target in conflict.affected_targets
        if target.department == DepartmentId.reverse
    ]
    other = [
        target for target in conflict.affected_targets
        if target.department != DepartmentId.reverse
    ]
    if not reverse or not other:
        return [conflict]
    partitions = []
    for authority, targets in (("sofia", reverse), ("miguel", other)):
        partitions.append(
            conflict.model_copy(
                update={
                    "conflict_id": uuid5(
                        NAMESPACE_URL,
                        f"trackflow-arbitration:{conflict.conflict_id}:{authority}",
                    ),
                    "affected_targets": targets,
                    "fingerprint": arbitration_conflict_fingerprint(
                        trigger=conflict.trigger,
                        facts=conflict.facts,
                        affected_targets=targets,
                    ),
                }
            )
        )
    return partitions


def _route_assessment(state: ArbitrationState) -> ArbitrationState:
    assessment = ConflictAssessment.model_validate_json(json.dumps(state["assessment"]))
    targets = [
        ArbitrationTarget.model_validate_json(json.dumps(item))
        for item in state["targets"]
    ]
    if assessment.outcome == "no_conflict":
        return {
            "outcome": "no_conflict",
            "resulting_status": "resolved",
            "required_arbitrators": [],
            "recommendation": {"action": "continue"},
        }
    if assessment.outcome == "insufficient_information":
        return {
            "outcome": "request_input",
            "resulting_status": "blocked_input",
            "required_arbitrators": [],
            "recommendation": {
                "action": "request_authorized_input",
                "missing_input_codes": assessment.missing_input_codes,
            },
        }

    facts = assessment.facts
    if isinstance(facts, VolumeCapacityFacts):
        recommendation = {
            "action": "request_changes",
            "rule": "limit_volume_to_documented_capacity",
            "requires_lastmile_volume_cost_review": True,
            "capacity_input_id": str(facts.capacity_input_id),
        }
        arbitrators = ["Miguel Torres"]
    elif isinstance(facts, CurrencyMismatchFacts):
        recommendation = {
            "action": "request_changes",
            "expected_currency": facts.expected_currency,
            "offending_revision_ids": [str(item) for item in facts.mismatched_revision_ids],
            "conversion_requires_authorized_input": True,
        }
        arbitrators = ["Miguel Torres"]
    elif isinstance(facts, ReturnsSLAFacts):
        target_by_revision = {
            item.department_revision_id: item.department for item in targets
            if item.department_revision_id is not None
        }
        violating_departments = {
            target_by_revision[item] for item in facts.violating_revision_ids
            if item in target_by_revision
        }
        if any(item not in target_by_revision for item in facts.violating_revision_ids):
            raise ArbitrationWorkflowError("return_sla_target_mismatch")
        arbitrators = []
        if DepartmentId.reverse in violating_departments:
            arbitrators.append("Sofía Ramos")
        if any(item != DepartmentId.reverse for item in violating_departments):
            arbitrators.append("Miguel Torres")
        recommendation = {
            "action": "request_changes",
            "minimum_returns_sla_hours": 48,
            "offending_revision_ids": [str(item) for item in facts.violating_revision_ids],
        }
    else:  # pragma: no cover - the discriminated contract is closed
        raise ArbitrationWorkflowError("unsupported_arbitration_trigger")
    return {
        "outcome": "route_to_arbitration",
        "resulting_status": "pending_arbitration",
        "required_arbitrators": arbitrators,
        "recommendation": recommendation,
    }


class ArbitrationWorkflow:
    """Real, named graph node; it detects/routes but never impersonates a human."""

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        backend: ArbitrationPersistenceBackend | None = None,
    ) -> None:
        self._backend = backend
        builder = StateGraph(ArbitrationState)
        builder.add_node("arbitrate_conflict", self._arbitrate_conflict)
        builder.add_edge(START, "arbitrate_conflict")
        builder.add_edge("arbitrate_conflict", END)
        self.graph = builder.compile(
            checkpointer=checkpointer, name="trackflow_explicit_arbitration"
        )

    def _arbitrate_conflict(self, state: ArbitrationState) -> ArbitrationState:
        routed = _route_assessment(state)
        persistence = state.get("persistence")
        if persistence is None:
            return routed
        if self._backend is None:
            raise ArbitrationWorkflowError("arbitration_persistence_backend_required")
        assessment = ConflictAssessment.model_validate_json(
            json.dumps(state["assessment"])
        )
        conflict_data = persistence.get("conflict")
        conflict = (
            ArbitrationConflict.model_validate_json(json.dumps(conflict_data))
            if conflict_data is not None else None
        )
        records = [
            self._backend.persist(
                approval_run_id=UUID(persistence["approval_run_id"]),
                ticket_id=UUID(persistence["ticket_id"]),
                assessment=assessment,
                assessed_content_hashes=persistence["assessed_content_hashes"],
                conflict=authority_conflict,
                execution_id=UUID(persistence["execution_id"]),
            )
            for authority_conflict in _authority_conflicts(conflict)
        ]
        serialized = [
            {
                "assessment_id": str(record.assessment_id),
                "conflict_id": str(record.conflict_id) if record.conflict_id else None,
                "outcome": record.outcome,
                "conflict_status": record.conflict_status,
                "replayed": record.replayed,
            }
            for record in records
        ]
        return {
            **routed,
            "conflict_record": serialized[0],
            "conflict_records": serialized,
        }

    def invoke(
        self,
        *,
        assessment: ConflictAssessment,
        targets: list[ArbitrationTarget],
        thread_id: str,
    ) -> ArbitrationState:
        return self.graph.invoke(
            {
                "assessment": assessment.model_dump(mode="json"),
                "targets": [item.model_dump(mode="json") for item in targets],
            },
            {"configurable": {"thread_id": thread_id}},
        )

    def invoke_persisted(
        self,
        *,
        approval_run_id: UUID,
        ticket_id: UUID,
        assessment: ConflictAssessment,
        assessed_content_hashes: dict[str, str],
        conflict: ArbitrationConflict | None,
        targets: list[ArbitrationTarget],
        execution_id: UUID,
        thread_id: str,
    ) -> ArbitrationState:
        return self.graph.invoke(
            {
                "assessment": assessment.model_dump(mode="json"),
                "targets": [item.model_dump(mode="json") for item in targets],
                "persistence": {
                    "approval_run_id": str(approval_run_id),
                    "ticket_id": str(ticket_id),
                    "assessed_content_hashes": assessed_content_hashes,
                    "conflict": conflict.model_dump(mode="json") if conflict else None,
                    "execution_id": str(execution_id),
                },
            },
            {"configurable": {"thread_id": thread_id}},
        )


def revision_limit_allows_correction(review_revision_no: int, *, maximum: int = 3) -> bool:
    """The existing 1..3 business revision limit; never resets Part 2 counters."""

    if maximum != 3 or not 1 <= review_revision_no <= maximum:
        raise ArbitrationWorkflowError("invalid_revision_limit")
    return review_revision_no < maximum
