"""LangGraph HITL orchestration for independent departmental approval branches.

The executable branches are Warehouse, Last Mile, and Reverse.  Persistence and
authorization stay in the already validated PostgreSQL services; this module
only coordinates a real LangGraph ``interrupt`` and its authenticated
``Command(resume=...)``.

The graph registers and applies the exact decision through the existing fenced
store, then enters an explicit coordinator node.  That node opens a new
transaction, reconstructs fan-in from PostgreSQL, and delegates atomic document
creation to the validated finalization service.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, select
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import (
    RESPONSIBLE_BY_DEPARTMENT,
    DepartmentId,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    AuthenticatedPrincipal,
    AuthorizedHumanActor,
    DecisionApplicationCommand,
    HumanDecision,
    ImmutableArtifactReference,
    ReviewAction,
    ReviewInterruptPayload,
    ReviewPreparationReceipt,
    decision_application_key,
    interrupt_payload_hash,
    review_audit_event_key,
)
from pipelines.rfp_approval.fan_in import determine_fan_in_readiness
from services.rfp_approval.authorization import (
    AuthorizationDenied,
    AuthorizationReference,
    AuthorizationTarget,
    resolve_effective_authorization,
)
from services.rfp_approval.application import AuthenticatedDecisionApplicationService
from services.rfp_approval.finalization import (
    FinalizationCommand,
    finalize_approved_proposal,
)
from services.rfp_approval.identity import (
    VerifiedAuthenticationContext,
    resolve_authenticated_identity,
)
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_bundles,
    rfp_final_documents,
)
from services.rfp_approval.proposal_coordinator import (
    materialize_delegated_initial_proposal,
)
from services.rfp_approval.store import (
    ApprovalStoreError,
    DecisionRecord,
    register_human_decision,
)
from services.rfp_intake.models import rfp_tickets


WAREHOUSE = DepartmentId.warehouse
LAST_MILE = DepartmentId.lastmile
REVERSE = DepartmentId.reverse
SupportedDepartment = Literal[
    DepartmentId.warehouse,
    DepartmentId.lastmile,
    DepartmentId.reverse,
]
_OFFICIAL_RESPONSIBLE = RESPONSIBLE_BY_DEPARTMENT


class ApprovalWorkflowError(RuntimeError):
    """Fail-closed orchestration error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__("RFP approval workflow operation was rejected")
        self.code = code


class DepartmentApprovalTarget(BaseModel):
    """Immutable identity of the persisted revision reviewed by this branch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    approval_run_id: UUID
    ticket_id: UUID
    revision_id: UUID
    department: SupportedDepartment

    @property
    def thread_id(self) -> str:
        return (
            f"rfp-{self.ticket_id}:{self.department.value}:"
            f"{self.approval_run_id}:{self.revision_id}"
        )


class HumanResumeEnvelope(BaseModel):
    """Server-created command envelope; it intentionally carries no identity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: ReviewAction
    reason: str | None = Field(default=None, max_length=2_000)
    decision_id: UUID
    idempotency_key: UUID
    decided_at: datetime

    @model_validator(mode="after")
    def validate_reason_and_time(self) -> "HumanResumeEnvelope":
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        if self.action in {"reject", "request_changes"} and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("reject and request_changes require a reason")
        return self


def new_human_resume(
    *,
    action: ReviewAction,
    reason: str | None = None,
    decided_at: datetime | None = None,
) -> HumanResumeEnvelope:
    """Create an internal resume envelope after authentication succeeds."""

    return HumanResumeEnvelope(
        action=action,
        reason=reason,
        decision_id=uuid4(),
        idempotency_key=uuid4(),
        decided_at=decided_at or datetime.now(timezone.utc),
    )


@dataclass(frozen=True, slots=True)
class PreparedDepartmentReview:
    receipt: ReviewPreparationReceipt
    interrupt_payload: ReviewInterruptPayload


@dataclass(frozen=True, slots=True)
class DepartmentDecisionReceipt:
    decision_id: UUID
    revision_id: UUID
    processing_status: str
    application_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class DepartmentApplicationReceipt:
    decision_id: UUID
    revision_id: UUID
    action: str
    processing_status: str
    revision_status: str
    revision_state_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApprovalCoordinationReceipt:
    """Safe result of one persisted fan-in/finalization reconciliation."""

    approval_run_id: UUID
    ticket_id: UUID
    outcome: Literal["waiting", "finalized"]
    blockers: tuple[str, ...]
    final_document_id: UUID | None
    replayed: bool
    coordinated_at: datetime


class DepartmentApprovalBackend(Protocol):
    """Persistence seam used by the graph and replaced only in offline tests."""

    def prepare_review(
        self, target: DepartmentApprovalTarget
    ) -> PreparedDepartmentReview: ...

    def register_decision(
        self,
        *,
        target: DepartmentApprovalTarget,
        prepared: PreparedDepartmentReview,
        resume: HumanResumeEnvelope,
        authentication: VerifiedAuthenticationContext,
        interrupt_id: str,
    ) -> DepartmentDecisionReceipt: ...

    def apply_decision(
        self,
        *,
        decision_id: UUID,
        authentication: VerifiedAuthenticationContext,
    ) -> DepartmentApplicationReceipt: ...

    def coordinate_completion(
        self, target: DepartmentApprovalTarget
    ) -> ApprovalCoordinationReceipt: ...


class ApprovalCompletionCoordinator(Protocol):
    """Durable coordinator invoked only after decision application returns."""

    def coordinate_completion(
        self, target: DepartmentApprovalTarget
    ) -> ApprovalCoordinationReceipt: ...


SessionFactory = Callable[[], AbstractContextManager[Session]]


class PostgresDepartmentApprovalBackend:
    """Adapter from the graph to existing PostgreSQL identity/auth/store code."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._application = AuthenticatedDecisionApplicationService(session_factory)

    def prepare_review(
        self, target: DepartmentApprovalTarget
    ) -> PreparedDepartmentReview:
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(
                    rfp_approval_runs.c.status.label("run_status"),
                    rfp_tickets.c.status.label("ticket_status"),
                    rfp_approval_department_revisions.c.is_current,
                    rfp_approval_department_revisions.c.status.label("revision_status"),
                    rfp_approval_department_revisions.c.content_hash,
                    rfp_approval_department_revisions.c.validation_hash,
                    rfp_approval_department_revisions.c.validation_payload,
                    rfp_approval_department_revisions.c.validation_bundle_id,
                    rfp_approval_validation_bundles.c.corrected_draft_id,
                    rfp_approval_validation_bundles.c.safe_preview_artifact_id,
                    rfp_approval_validation_bundles.c.safe_preview_contract_version,
                    rfp_approval_validation_bundles.c.safe_preview_payload_hash,
                    rfp_approval_validation_bundles.c.eligible.label("bundle_eligible"),
                    rfp_approval_validation_bundles.c.validated_at,
                )
                .select_from(
                    rfp_approval_runs.join(
                        rfp_tickets,
                        rfp_tickets.c.ticket_id == rfp_approval_runs.c.ticket_id,
                    ).join(
                        rfp_approval_department_revisions,
                        and_(
                            rfp_approval_department_revisions.c.approval_run_id
                            == rfp_approval_runs.c.approval_run_id,
                            rfp_approval_department_revisions.c.ticket_id
                            == rfp_approval_runs.c.ticket_id,
                        ),
                    ).join(
                        rfp_approval_validation_bundles,
                        rfp_approval_validation_bundles.c.validation_bundle_id
                        == rfp_approval_department_revisions.c.validation_bundle_id,
                    )
                )
                .where(
                    rfp_approval_runs.c.approval_run_id == target.approval_run_id,
                    rfp_approval_runs.c.ticket_id == target.ticket_id,
                    rfp_approval_department_revisions.c.revision_id
                    == target.revision_id,
                    rfp_approval_department_revisions.c.department_id
                    == target.department.value,
                    rfp_approval_validation_bundles.c.scope_key
                    == target.department.value,
                )
                .with_for_update(key_share=True)
            ).mappings().one_or_none()
            if row is None:
                raise ApprovalWorkflowError("review_target_not_found")
            values = dict(row)
            if values["run_status"] != "active":
                raise ApprovalWorkflowError("approval_run_not_active")
            if values["ticket_status"] != "esperando_aprobación":
                raise ApprovalWorkflowError("ticket_not_waiting_for_approval")
            if not values["is_current"]:
                raise ApprovalWorkflowError("revision_obsolete")
            if values["revision_status"] != "pending_approval":
                raise ApprovalWorkflowError("revision_not_pending_approval")
            if (
                values["validation_payload"].get("eligible_for_approval") is not True
                or values["bundle_eligible"] is not True
            ):
                raise ApprovalWorkflowError("revision_not_eligible")

            preparation_id = uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "trackflow-rfp-review-v1",
                        str(target.approval_run_id),
                        str(target.revision_id),
                        values["content_hash"],
                        values["validation_hash"],
                    )
                ),
            )
            receipt = ReviewPreparationReceipt(
                preparation_id=preparation_id,
                approval_run_id=target.approval_run_id,
                revision_id=target.revision_id,
                draft_id=values["corrected_draft_id"],
                content_hash=values["content_hash"],
                validation_hash=values["validation_hash"],
                audit_event_key=review_audit_event_key(
                    target.approval_run_id,
                    target.revision_id,
                    values["content_hash"],
                ),
                prepared_at=values["validated_at"],
            )
            preview = ImmutableArtifactReference(
                artifact_type="safe_preview",
                artifact_id=values["safe_preview_artifact_id"],
                contract_version=values["safe_preview_contract_version"],
                payload_hash=values["safe_preview_payload_hash"],
                evaluated_content_hash=values["content_hash"],
            )
            actions: list[ReviewAction] = ["approve", "reject", "request_changes"]
            payload = ReviewInterruptPayload(
                preparation_id=preparation_id,
                revision_id=target.revision_id,
                draft_id=values["corrected_draft_id"],
                expected_content_hash=values["content_hash"],
                allowed_actions=actions,
                safe_preview_ref=preview,
                interrupt_payload_hash=interrupt_payload_hash(
                    preparation_id=preparation_id,
                    revision_id=target.revision_id,
                    draft_id=values["corrected_draft_id"],
                    expected_content_hash=values["content_hash"],
                    allowed_actions=actions,
                    safe_preview_ref=preview,
                ),
            )
            return PreparedDepartmentReview(receipt=receipt, interrupt_payload=payload)

    def register_decision(
        self,
        *,
        target: DepartmentApprovalTarget,
        prepared: PreparedDepartmentReview,
        resume: HumanResumeEnvelope,
        authentication: VerifiedAuthenticationContext,
        interrupt_id: str,
    ) -> DepartmentDecisionReceipt:
        with self._session_factory() as session, session.begin():
            identity = resolve_authenticated_identity(session, context=authentication)
            authorization = resolve_effective_authorization(
                session,
                identity=identity,
                target=AuthorizationTarget(
                    permission="approve_department",
                    department_scope=target.department.value,
                    target_policy="department_exact",
                    affected_departments=frozenset({target.department.value}),
                ),
            )
            principal = AuthenticatedPrincipal(
                issuer=identity.issuer,
                subject=identity.subject,
                trackflow_user_id=identity.trackflow_user_id,
                auth_source=identity.auth_source,
                general_role=identity.general_role,
                synthetic=identity.synthetic,
            )
            actor = AuthorizedHumanActor(
                principal=principal,
                authorization_id=authorization.authorization_id,
                official_responsible=_OFFICIAL_RESPONSIBLE[target.department],
                permission="approve_department",
                department_scope=target.department,
            )
            decision = HumanDecision(
                decision_id=resume.decision_id,
                idempotency_key=resume.idempotency_key,
                ticket_id=target.ticket_id,
                approval_run_id=target.approval_run_id,
                department=target.department,
                revision_id=target.revision_id,
                draft_id=prepared.receipt.draft_id,
                expected_content_hash=prepared.receipt.content_hash,
                interrupt_id=interrupt_id,
                action=resume.action,
                reason=resume.reason,
                actor=actor,
                decided_at=resume.decided_at,
            )
            command = DecisionApplicationCommand(
                decision=decision,
                preparation_id=prepared.receipt.preparation_id,
                application_key=decision_application_key(
                    decision, prepared.receipt.preparation_id
                ),
            )
            record: DecisionRecord = register_human_decision(
                session,
                command=command,
                identity=identity,
                authorization_reference=AuthorizationReference(
                    authorization.authorization_id,
                    authorization.authorization_version,
                ),
                execution_id=uuid5(
                    NAMESPACE_URL,
                    f"trackflow-hitl-register:{command.application_key}",
                ),
            )
            return DepartmentDecisionReceipt(
                decision_id=record.decision_id,
                revision_id=record.revision_id,
                processing_status=record.processing_status,
                application_key=record.application_key,
                replayed=record.replayed,
            )

    def apply_decision(
        self,
        *,
        decision_id: UUID,
        authentication: VerifiedAuthenticationContext,
    ) -> DepartmentApplicationReceipt:
        result = self._application.apply(
            decision_id=decision_id,
            authentication=authentication,
            execution_id=uuid5(
                NAMESPACE_URL, f"trackflow-hitl-apply:{decision_id}"
            ),
        )
        return DepartmentApplicationReceipt(
            decision_id=result.decision_id,
            revision_id=result.revision_id,
            action=result.action,
            processing_status=result.processing_status,
            revision_status=result.revision_status,
            revision_state_version=result.revision_state_version,
            replayed=result.replayed,
        )

    def coordinate_completion(
        self, target: DepartmentApprovalTarget
    ) -> ApprovalCoordinationReceipt:
        """Rebuild fan-in and finalize in a transaction after application commit."""

        coordinated_at = datetime.now(timezone.utc)
        final_document_id = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "trackflow-rfp-final-document-v1",
                    str(target.approval_run_id),
                    str(target.ticket_id),
                )
            ),
        )
        command = FinalizationCommand(
            approval_run_id=target.approval_run_id,
            ticket_id=target.ticket_id,
            final_document_id=final_document_id,
            execution_id=uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "trackflow-rfp-finalization-v1",
                        str(target.approval_run_id),
                        str(target.ticket_id),
                    )
                ),
            ),
            generated_at=coordinated_at,
        )
        with self._session_factory() as session, session.begin():
            existing_document_id = session.execute(
                select(rfp_final_documents.c.final_document_id).where(
                    rfp_final_documents.c.approval_run_id
                    == target.approval_run_id,
                    rfp_final_documents.c.ticket_id == target.ticket_id,
                )
            ).scalar_one_or_none()
            if existing_document_id is None:
                readiness = determine_fan_in_readiness(
                    session,
                    approval_run_id=target.approval_run_id,
                    ticket_id=target.ticket_id,
                )
                if not readiness.ready_for_synthesis:
                    if readiness.blockers == ("current_proposal_count",):
                        try:
                            materialize_delegated_initial_proposal(
                                session,
                                approval_run_id=target.approval_run_id,
                                ticket_id=target.ticket_id,
                            )
                        except AuthorizationDenied:
                            return ApprovalCoordinationReceipt(
                                approval_run_id=target.approval_run_id,
                                ticket_id=target.ticket_id,
                                outcome="waiting",
                                blockers=(
                                    "proposal_automation_authorization_invalid",
                                ),
                                final_document_id=None,
                                replayed=False,
                                coordinated_at=coordinated_at,
                            )
                        except ApprovalStoreError as error:
                            if error.code in {
                                "proposal_automation_not_delegated",
                                "proposal_automation_delegation_invalid",
                                "part2_global_source_not_eligible_for_import",
                            }:
                                return ApprovalCoordinationReceipt(
                                    approval_run_id=target.approval_run_id,
                                    ticket_id=target.ticket_id,
                                    outcome="waiting",
                                    blockers=(error.code,),
                                    final_document_id=None,
                                    replayed=False,
                                    coordinated_at=coordinated_at,
                                )
                            raise
                        readiness = determine_fan_in_readiness(
                            session,
                            approval_run_id=target.approval_run_id,
                            ticket_id=target.ticket_id,
                        )
                    if readiness.ready_for_synthesis:
                        pass
                    else:
                        return ApprovalCoordinationReceipt(
                            approval_run_id=target.approval_run_id,
                            ticket_id=target.ticket_id,
                            outcome="waiting",
                            blockers=readiness.blockers,
                            final_document_id=None,
                            replayed=False,
                            coordinated_at=coordinated_at,
                        )

            result = finalize_approved_proposal(session, command=command)
            return ApprovalCoordinationReceipt(
                approval_run_id=target.approval_run_id,
                ticket_id=target.ticket_id,
                outcome="finalized",
                blockers=(),
                final_document_id=result.document.final_document_id,
                replayed=result.replayed,
                coordinated_at=coordinated_at,
            )


class DepartmentApprovalState(TypedDict, total=False):
    target: dict[str, Any]
    preparation: dict[str, Any]
    interrupt_payload: dict[str, Any]
    resume: dict[str, Any]
    decision: dict[str, Any]
    application: dict[str, Any]
    coordination: dict[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class ApprovalRuntimeContext:
    authentication: VerifiedAuthenticationContext | None = None


def _prepared_from_state(state: DepartmentApprovalState) -> PreparedDepartmentReview:
    return PreparedDepartmentReview(
        receipt=ReviewPreparationReceipt.model_validate(state["preparation"]),
        interrupt_payload=ReviewInterruptPayload.model_validate(
            state["interrupt_payload"]
        ),
    )


def _target_from_state(state: DepartmentApprovalState) -> DepartmentApprovalTarget:
    # JSON-mode state avoids persisting application enum classes in a
    # checkpointer serializer while retaining strict validation on recovery.
    return DepartmentApprovalTarget.model_validate_json(json.dumps(state["target"]))


class DepartmentApprovalWorkflow:
    """Compiled, checkpointer-backed departmental HITL workflow."""

    def __init__(self, *, backend: DepartmentApprovalBackend, checkpointer: Any) -> None:
        if checkpointer is None:
            raise ValueError("a persistent-capable LangGraph checkpointer is required")
        self._backend = backend
        if not callable(getattr(backend, "coordinate_completion", None)):
            raise ValueError("a durable completion coordinator is required")
        self._coordinator: ApprovalCompletionCoordinator = backend
        builder = StateGraph(
            DepartmentApprovalState, context_schema=ApprovalRuntimeContext
        )
        builder.add_node("prepare_review", self._prepare_review)
        builder.add_node("interrupt_and_register", self._interrupt_and_register)
        builder.add_node("apply_registered_decision", self._apply_registered_decision)
        builder.add_node("decision_applied", self._decision_applied)
        builder.add_node(
            "coordinate_fan_in_and_finalize", self._coordinate_fan_in_and_finalize
        )
        builder.add_edge(START, "prepare_review")
        builder.add_edge("prepare_review", "interrupt_and_register")
        builder.add_edge("interrupt_and_register", "apply_registered_decision")
        builder.add_edge("apply_registered_decision", "decision_applied")
        builder.add_edge("decision_applied", "coordinate_fan_in_and_finalize")
        builder.add_edge("coordinate_fan_in_and_finalize", END)
        self.graph = builder.compile(
            checkpointer=checkpointer, name="trackflow_department_approval_hitl"
        )

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    def _prepare_review(
        self, state: DepartmentApprovalState
    ) -> DepartmentApprovalState:
        target = _target_from_state(state)
        prepared = self._backend.prepare_review(target)
        return {
            "preparation": prepared.receipt.model_dump(mode="python"),
            "interrupt_payload": prepared.interrupt_payload.model_dump(mode="python"),
            "status": "awaiting_human_decision",
        }

    def _interrupt_and_register(
        self,
        state: DepartmentApprovalState,
        runtime: Runtime[ApprovalRuntimeContext],
    ) -> DepartmentApprovalState:
        target = _target_from_state(state)
        prepared = _prepared_from_state(state)
        interrupt_id = (
            f"{target.thread_id}:{prepared.interrupt_payload.interrupt_payload_hash}"
        )
        resume_value = interrupt(
            {
                "kind": "department_approval",
                "department": target.department.value,
                "thread_id": target.thread_id,
                "interrupt_id": interrupt_id,
                "request": prepared.interrupt_payload.model_dump(mode="python"),
            }
        )
        resume = HumanResumeEnvelope.model_validate(resume_value)
        if runtime.context is None or runtime.context.authentication is None:
            raise ApprovalWorkflowError("verified_authentication_required")
        record = self._backend.register_decision(
            target=target,
            prepared=prepared,
            resume=resume,
            authentication=runtime.context.authentication,
            interrupt_id=interrupt_id,
        )
        return {
            "resume": resume.model_dump(mode="python"),
            "decision": {
                **asdict(record),
                "decision_id": str(record.decision_id),
                "revision_id": str(record.revision_id),
            },
            "status": "decision_registered",
        }

    def _apply_registered_decision(
        self,
        state: DepartmentApprovalState,
        runtime: Runtime[ApprovalRuntimeContext],
    ) -> DepartmentApprovalState:
        if state.get("decision", {}).get("processing_status") != "queued":
            raise ApprovalWorkflowError("decision_not_queued")
        if runtime.context is None or runtime.context.authentication is None:
            raise ApprovalWorkflowError("verified_authentication_required")
        result = self._backend.apply_decision(
            decision_id=UUID(state["decision"]["decision_id"]),
            authentication=runtime.context.authentication,
        )
        return {
            "application": {
                **asdict(result),
                "decision_id": str(result.decision_id),
                "revision_id": str(result.revision_id),
            },
            "status": "decision_applied",
        }

    @staticmethod
    def _decision_applied(state: DepartmentApprovalState) -> DepartmentApprovalState:
        processing_status = state.get("application", {}).get("processing_status")
        if processing_status not in {"applied", "rejected_stale"}:
            raise ApprovalWorkflowError("decision_not_applied")
        return {
            "status": (
                "decision_applied"
                if processing_status == "applied"
                else "decision_rejected_stale"
            )
        }

    @staticmethod
    def _coordination_state(
        receipt: ApprovalCoordinationReceipt,
    ) -> DepartmentApprovalState:
        coordination = {
            "approval_run_id": str(receipt.approval_run_id),
            "ticket_id": str(receipt.ticket_id),
            "outcome": receipt.outcome,
            "blockers": list(receipt.blockers),
            "final_document_id": (
                str(receipt.final_document_id)
                if receipt.final_document_id is not None
                else None
            ),
            "replayed": receipt.replayed,
            "coordinated_at": receipt.coordinated_at.isoformat(),
        }
        return {
            "coordination": coordination,
            "status": (
                "finalized" if receipt.outcome == "finalized" else "decision_applied"
            ),
        }

    def _coordinate_fan_in_and_finalize(
        self, state: DepartmentApprovalState
    ) -> DepartmentApprovalState:
        if state.get("application", {}).get("processing_status") != "applied":
            return {"status": state["status"]}
        receipt = self._coordinator.coordinate_completion(_target_from_state(state))
        return self._coordination_state(receipt)

    def start(self, target: DepartmentApprovalTarget) -> DepartmentApprovalState:
        config = self._config(target.thread_id)
        existing = self.graph.get_state(config)
        if existing.values:
            raise ApprovalWorkflowError("workflow_thread_already_exists")
        return self.graph.invoke(
            {"target": target.model_dump(mode="json"), "status": "starting"},
            config,
            context=ApprovalRuntimeContext(),
        )

    def resume(
        self,
        *,
        thread_id: str,
        command: HumanResumeEnvelope,
        authentication: VerifiedAuthenticationContext,
    ) -> DepartmentApprovalState:
        if not isinstance(authentication, VerifiedAuthenticationContext):
            raise ApprovalWorkflowError("verified_authentication_required")
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise ApprovalWorkflowError("workflow_thread_not_found")
        target = _target_from_state(snapshot.values)
        if target.thread_id != thread_id:
            raise ApprovalWorkflowError("workflow_thread_target_mismatch")
        if not any(task.interrupts for task in snapshot.tasks):
            raise ApprovalWorkflowError("workflow_not_interrupted")
        return self.graph.invoke(
            Command(resume=command.model_dump(mode="python")),
            config,
            context=ApprovalRuntimeContext(authentication=authentication),
        )

    def state(self, thread_id: str) -> Any:
        """Return the native LangGraph snapshot for diagnostics/recovery."""

        return self.graph.get_state(self._config(thread_id))

    def reconcile(self, thread_id: str) -> DepartmentApprovalState:
        """Recover finalization from persisted business state without a new decision."""

        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise ApprovalWorkflowError("workflow_thread_not_found")
        if not snapshot.values.get("decision"):
            raise ApprovalWorkflowError("durable_decision_not_registered")
        receipt = self._coordinator.coordinate_completion(
            _target_from_state(snapshot.values)
        )
        update = self._coordination_state(receipt)
        if receipt.outcome == "finalized":
            self.graph.update_state(
                config, update, as_node="coordinate_fan_in_and_finalize"
            )
            return self.graph.get_state(config).values
        return {**snapshot.values, **update}


def resume_fingerprint(command: HumanResumeEnvelope) -> str:
    """Safe diagnostic hash; never includes authenticated identity or secrets."""

    return canonical_hash(command.model_dump(mode="json"))


# Backward-compatible names for the first Warehouse MVP.  They now point to the
# same three-department implementation instead of maintaining a parallel graph.
WarehouseApprovalTarget = DepartmentApprovalTarget
PreparedWarehouseReview = PreparedDepartmentReview
WarehouseDecisionReceipt = DepartmentDecisionReceipt
WarehouseApprovalBackend = DepartmentApprovalBackend
PostgresWarehouseApprovalBackend = PostgresDepartmentApprovalBackend
WarehouseApprovalState = DepartmentApprovalState
WarehouseApprovalWorkflow = DepartmentApprovalWorkflow
