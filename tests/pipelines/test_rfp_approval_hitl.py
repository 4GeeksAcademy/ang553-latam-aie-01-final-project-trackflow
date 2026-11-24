from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from inspect import signature
from types import SimpleNamespace
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import (
    ImmutableArtifactReference,
    ReviewInterruptPayload,
    ReviewPreparationReceipt,
    interrupt_payload_hash,
    review_audit_event_key,
)
from pipelines.rfp_approval.hitl import (
    ApprovalCoordinationReceipt,
    ApprovalWorkflowError,
    DepartmentApplicationReceipt,
    DepartmentApprovalTarget,
    DepartmentApprovalWorkflow,
    DepartmentDecisionReceipt,
    HumanResumeEnvelope,
    PostgresDepartmentApprovalBackend,
    PostgresWarehouseApprovalBackend,
    PreparedWarehouseReview,
    WarehouseApprovalTarget,
    WarehouseApprovalWorkflow,
    WarehouseDecisionReceipt,
)
from services.rfp_approval.identity import (
    ResolvedIdentity,
    VerifiedAuthenticationContext,
)
from services.rfp_approval.store import ApprovalStoreError, DecisionRecord
from services.rfp_approval.store import claim_next_decision


NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
RUN_ID = UUID(int=1)
TICKET_ID = UUID(int=2)
REVISION_ID = UUID(int=3)
DRAFT_ID = UUID(int=4)
PREVIEW_ID = UUID(int=5)
PREPARATION_ID = UUID(int=6)
AUTHORIZATION_ID = UUID(int=7)
PRINCIPAL_ID = UUID(int=8)
BINDING_ID = UUID(int=9)
H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64


def _target() -> WarehouseApprovalTarget:
    return WarehouseApprovalTarget(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        revision_id=REVISION_ID,
        department=DepartmentId.warehouse,
    )


def _prepared() -> PreparedWarehouseReview:
    preview = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=PREVIEW_ID,
        contract_version="rfp-safe-preview-v1",
        payload_hash=H64_B,
        evaluated_content_hash=H64,
    )
    actions = ["approve", "reject", "request_changes"]
    receipt = ReviewPreparationReceipt(
        preparation_id=PREPARATION_ID,
        approval_run_id=RUN_ID,
        revision_id=REVISION_ID,
        draft_id=DRAFT_ID,
        content_hash=H64,
        validation_hash=H64_C,
        audit_event_key=review_audit_event_key(RUN_ID, REVISION_ID, H64),
        prepared_at=NOW,
    )
    payload = ReviewInterruptPayload(
        preparation_id=PREPARATION_ID,
        revision_id=REVISION_ID,
        draft_id=DRAFT_ID,
        expected_content_hash=H64,
        allowed_actions=actions,
        safe_preview_ref=preview,
        interrupt_payload_hash=interrupt_payload_hash(
            preparation_id=PREPARATION_ID,
            revision_id=REVISION_ID,
            draft_id=DRAFT_ID,
            expected_content_hash=H64,
            allowed_actions=actions,
            safe_preview_ref=preview,
        ),
    )
    return PreparedWarehouseReview(receipt=receipt, interrupt_payload=payload)


def _authentication(subject: str = "ana-subject") -> VerifiedAuthenticationContext:
    user_ids = {
        "ana-subject": "ana-qa",
        "carlos-subject": "carlos-qa",
        "sofia-subject": "sofia-qa",
    }
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc",
        subject=subject,
        trackflow_user_id=user_ids.get(subject, "other-qa"),
        general_role="manager",
    )


def _resume(decision: int = 20) -> HumanResumeEnvelope:
    return HumanResumeEnvelope(
        action="approve",
        reason=None,
        decision_id=UUID(int=decision),
        idempotency_key=UUID(int=decision + 1),
        decided_at=NOW,
    )


class _OfflineBackend:
    """A test double: it does not claim to emulate PostgreSQL transactions."""

    def __init__(self) -> None:
        self.preparations = 0
        self.decisions: list[HumanResumeEnvelope] = []
        self.applications: list[UUID] = []
        self.current = True
        self.authorization_active = True
        self.ticket_status = "esperando_aprobación"

    def prepare_review(self, target):
        assert target == _target()
        self.preparations += 1
        if not self.current:
            raise ApprovalWorkflowError("revision_obsolete")
        return _prepared()

    def register_decision(
        self, *, target, prepared, resume, authentication, interrupt_id
    ):
        assert target == _target()
        assert prepared == _prepared()
        assert interrupt_id.startswith(target.thread_id)
        if authentication.subject != "ana-subject":
            raise ApprovalWorkflowError("no_effective_authorization")
        if not self.authorization_active:
            raise ApprovalWorkflowError("no_effective_authorization")
        if not self.current:
            raise ApprovalWorkflowError("revision_obsolete")
        self.decisions.append(resume)
        return WarehouseDecisionReceipt(
            decision_id=resume.decision_id,
            revision_id=target.revision_id,
            processing_status="queued",
            application_key="decision-apply:" + "d" * 64,
            replayed=False,
        )

    def apply_decision(self, *, decision_id, authentication):
        resume = next(item for item in self.decisions if item.decision_id == decision_id)
        self.applications.append(decision_id)
        status = {
            "approve": "approved",
            "reject": "rejected",
            "request_changes": "changes_requested",
        }[resume.action]
        return DepartmentApplicationReceipt(
            decision_id=decision_id,
            revision_id=REVISION_ID,
            action=resume.action,
            processing_status="applied",
            revision_status=status,
            revision_state_version=5,
            replayed=False,
        )

    def coordinate_completion(self, target):
        return ApprovalCoordinationReceipt(
            approval_run_id=target.approval_run_id,
            ticket_id=target.ticket_id,
            outcome="waiting",
            blockers=("offline_fixture_not_finalizable",),
            final_document_id=None,
            replayed=False,
            coordinated_at=NOW,
        )


def _workflow(backend=None, saver=None):
    backend = backend or _OfflineBackend()
    saver = saver or InMemorySaver()
    return WarehouseApprovalWorkflow(backend=backend, checkpointer=saver), backend, saver


def test_real_interrupt_persists_checkpoint_without_decision() -> None:
    workflow, backend, _ = _workflow()

    result = workflow.start(_target())

    assert result["status"] == "awaiting_human_decision"
    assert len(result["__interrupt__"]) == 1
    interruption = result["__interrupt__"][0]
    assert interruption.value["kind"] == "department_approval"
    assert interruption.value["department"] == "warehouse"
    assert interruption.value["thread_id"] == _target().thread_id
    assert interruption.value["request"]["safe_preview_ref"]["artifact_id"] == PREVIEW_ID
    assert backend.decisions == []
    snapshot = workflow.state(_target().thread_id)
    assert snapshot.values["status"] == "awaiting_human_decision"
    assert snapshot.tasks[0].interrupts


def test_command_resume_registers_and_applies_authorized_decision() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())

    result = workflow.resume(
        thread_id=_target().thread_id,
        command=_resume(),
        authentication=_authentication(),
    )

    assert result["status"] == "decision_applied"
    assert result["decision"]["decision_id"] == str(UUID(int=20))
    assert result["application"]["revision_status"] == "approved"
    assert backend.decisions == [_resume()]
    assert backend.applications == [UUID(int=20)]
    final = workflow.state(_target().thread_id)
    assert final.next == ()
    assert final.values["status"] == "decision_applied"


def test_wrong_actor_is_rejected_and_interrupt_remains_recoverable() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=_target().thread_id,
            command=_resume(),
            authentication=_authentication("carlos-subject"),
        )

    assert raised.value.code == "no_effective_authorization"
    assert backend.decisions == []
    assert workflow.state(_target().thread_id).tasks[0].interrupts


def test_obsolete_revision_is_rechecked_after_interrupt() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())
    backend.current = False

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=_target().thread_id,
            command=_resume(),
            authentication=_authentication(),
        )

    assert raised.value.code == "revision_obsolete"
    assert backend.decisions == []


def test_revoked_authorization_cannot_queue_decision() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())
    backend.authorization_active = False

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=_target().thread_id,
            command=_resume(),
            authentication=_authentication(),
        )

    assert raised.value.code == "no_effective_authorization"
    assert backend.decisions == []


def test_duplicate_resume_does_not_create_another_decision() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())
    workflow.resume(
        thread_id=_target().thread_id,
        command=_resume(),
        authentication=_authentication(),
    )

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=_target().thread_id,
            command=_resume(),
            authentication=_authentication(),
        )

    assert raised.value.code == "workflow_not_interrupted"
    assert len(backend.decisions) == 1


def test_department_application_does_not_finish_ticket_or_synthesize_document() -> None:
    workflow, backend, _ = _workflow()
    workflow.start(_target())
    result = workflow.resume(
        thread_id=_target().thread_id,
        command=_resume(),
        authentication=_authentication(),
    )

    assert result["decision"]["processing_status"] == "queued"
    assert result["application"]["processing_status"] == "applied"
    assert backend.ticket_status == "esperando_aprobación"
    assert "final_document" not in result


def test_checkpoint_recovers_interrupt_with_new_compiled_graph_instance() -> None:
    backend = _OfflineBackend()
    saver = InMemorySaver()
    first = WarehouseApprovalWorkflow(backend=backend, checkpointer=saver)
    first.start(_target())

    recovered = WarehouseApprovalWorkflow(backend=backend, checkpointer=saver)
    snapshot = recovered.state(_target().thread_id)
    assert snapshot.values["status"] == "awaiting_human_decision"
    assert snapshot.tasks[0].interrupts
    result = recovered.resume(
        thread_id=_target().thread_id,
        command=_resume(),
        authentication=_authentication(),
    )

    assert result["status"] == "decision_applied"
    assert len(backend.decisions) == 1


def test_thread_id_is_namespaced_by_ticket_department_run_and_revision() -> None:
    thread_id = _target().thread_id
    assert str(TICKET_ID) in thread_id
    assert ":warehouse:" in thread_id
    assert str(RUN_ID) in thread_id
    assert str(REVISION_ID) in thread_id


class _SessionContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin(self):
        return nullcontext()


class _EmptyScalarResult:
    def scalar_one_or_none(self):
        return None


class _CompletionSessionContext(_SessionContext):
    def execute(self, statement):
        return _EmptyScalarResult()


def test_postgres_coordinator_materializes_delegated_proposal_before_finalizing(
    monkeypatch,
) -> None:
    calls: list[str] = []
    readiness = iter(
        (
            SimpleNamespace(
                ready_for_synthesis=False,
                blockers=("current_proposal_count",),
            ),
            SimpleNamespace(ready_for_synthesis=True, blockers=()),
        )
    )
    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.determine_fan_in_readiness",
        lambda *args, **kwargs: next(readiness),
    )
    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.materialize_delegated_initial_proposal",
        lambda *args, **kwargs: calls.append("materialized"),
    )
    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.finalize_approved_proposal",
        lambda *args, **kwargs: SimpleNamespace(
            document=SimpleNamespace(final_document_id=UUID(int=999)),
            replayed=False,
        ),
    )
    backend = PostgresDepartmentApprovalBackend(lambda: _CompletionSessionContext())

    receipt = backend.coordinate_completion(_target())

    assert calls == ["materialized"]
    assert receipt.outcome == "finalized"
    assert receipt.final_document_id == UUID(int=999)


@pytest.mark.parametrize(
    "blocker",
    [
        "proposal_automation_not_delegated",
        "part2_global_source_not_eligible_for_import",
    ],
)
def test_postgres_coordinator_keeps_unmaterializable_global_waiting(
    monkeypatch, blocker
) -> None:
    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.determine_fan_in_readiness",
        lambda *args, **kwargs: SimpleNamespace(
            ready_for_synthesis=False,
            blockers=("current_proposal_count",),
        ),
    )

    def reject(*args, **kwargs):
        raise ApprovalStoreError(blocker)

    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.materialize_delegated_initial_proposal",
        reject,
    )
    backend = PostgresDepartmentApprovalBackend(lambda: _CompletionSessionContext())

    receipt = backend.coordinate_completion(_target())

    assert receipt.outcome == "waiting"
    assert receipt.blockers == (blocker,)


@pytest.mark.parametrize(
    "department,subject,user_id,responsible_code,official_responsible",
    [
        (
            DepartmentId.warehouse,
            "ana-subject",
            "ana-qa",
            "ana_whitfield",
            "Ana Whitfield",
        ),
        (
            DepartmentId.lastmile,
            "carlos-subject",
            "carlos-qa",
            "carlos_vega",
            "Carlos Vega",
        ),
        (
            DepartmentId.reverse,
            "sofia-subject",
            "sofia-qa",
            "sofia_ramos",
            "Sofía Ramos",
        ),
    ],
)
def test_postgres_adapter_resolves_identity_authorization_and_existing_store(
    monkeypatch, department, subject, user_id, responsible_code, official_responsible
) -> None:
    identity = ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc",
        subject=subject,
        trackflow_user_id=user_id,
        general_role="manager",
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        responsible_code=responsible_code,
        synthetic=True,
    )
    captured = {}
    authorization_target = {}

    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.resolve_authenticated_identity",
        lambda session, *, context: identity,
    )
    def resolve_authorization(*args, **kwargs):
        authorization_target["value"] = kwargs["target"]
        return SimpleNamespace(
            authorization_id=AUTHORIZATION_ID, authorization_version=3
        )

    monkeypatch.setattr(
        "pipelines.rfp_approval.hitl.resolve_effective_authorization",
        resolve_authorization,
    )

    def register(session, **kwargs):
        captured.update(kwargs)
        return DecisionRecord(
            decision_id=kwargs["command"].decision.decision_id,
            approval_run_id=RUN_ID,
            revision_id=REVISION_ID,
            processing_status="queued",
            application_key=kwargs["command"].application_key,
            replayed=False,
        )

    monkeypatch.setattr("pipelines.rfp_approval.hitl.register_human_decision", register)
    backend = PostgresWarehouseApprovalBackend(lambda: _SessionContext())
    target = (
        _target()
        if department == DepartmentId.warehouse
        else DepartmentApprovalTarget(
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            revision_id=REVISION_ID,
            department=department,
        )
    )
    prepared = (
        _prepared()
        if department == DepartmentId.warehouse
        else _department_prepared(target)
    )

    result = backend.register_decision(
        target=target,
        prepared=prepared,
        resume=_resume(),
        authentication=_authentication(subject),
        interrupt_id="real-langgraph-interrupt-envelope",
    )

    command = captured["command"]
    assert result.processing_status == "queued"
    assert command.decision.actor.official_responsible == official_responsible
    assert command.decision.actor.department_scope == department
    assert command.decision.interrupt_id == "real-langgraph-interrupt-envelope"
    assert captured["identity"] is identity
    assert captured["authorization_reference"].authorization_id == AUTHORIZATION_ID
    assert authorization_target["value"].department_scope == department.value


def test_workflow_requires_an_injected_checkpointer() -> None:
    with pytest.raises(ValueError, match="checkpointer"):
        WarehouseApprovalWorkflow(backend=_OfflineBackend(), checkpointer=None)


def _department_target(
    department: DepartmentId, revision_id: UUID
) -> DepartmentApprovalTarget:
    return DepartmentApprovalTarget(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        revision_id=revision_id,
        department=department,
    )


def _department_prepared(target: DepartmentApprovalTarget) -> PreparedWarehouseReview:
    offset = {
        DepartmentId.warehouse: 100,
        DepartmentId.lastmile: 200,
        DepartmentId.reverse: 300,
    }[target.department]
    draft_id = UUID(int=offset + 1)
    preview = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=UUID(int=offset + 2),
        contract_version="rfp-safe-preview-v1",
        payload_hash=H64_B,
        evaluated_content_hash=H64,
    )
    actions = ["approve", "reject", "request_changes"]
    preparation_id = UUID(int=offset + 3)
    receipt = ReviewPreparationReceipt(
        preparation_id=preparation_id,
        approval_run_id=target.approval_run_id,
        revision_id=target.revision_id,
        draft_id=draft_id,
        content_hash=H64,
        validation_hash=H64_C,
        audit_event_key=review_audit_event_key(
            target.approval_run_id, target.revision_id, H64
        ),
        prepared_at=NOW,
    )
    payload = ReviewInterruptPayload(
        preparation_id=preparation_id,
        revision_id=target.revision_id,
        draft_id=draft_id,
        expected_content_hash=H64,
        allowed_actions=actions,
        safe_preview_ref=preview,
        interrupt_payload_hash=interrupt_payload_hash(
            preparation_id=preparation_id,
            revision_id=target.revision_id,
            draft_id=draft_id,
            expected_content_hash=H64,
            allowed_actions=actions,
            safe_preview_ref=preview,
        ),
    )
    return PreparedWarehouseReview(receipt=receipt, interrupt_payload=payload)


class _IndependentDepartmentBackend:
    def __init__(self) -> None:
        self.decisions: list[tuple[DepartmentId, HumanResumeEnvelope]] = []
        self.applications: list[tuple[DepartmentId, UUID]] = []
        self.ticket_status = "esperando_aprobación"

    def prepare_review(self, target):
        return _department_prepared(target)

    def register_decision(
        self, *, target, prepared, resume, authentication, interrupt_id
    ):
        expected_subject = {
            DepartmentId.warehouse: "ana-subject",
            DepartmentId.lastmile: "carlos-subject",
            DepartmentId.reverse: "sofia-subject",
        }[target.department]
        if authentication.subject != expected_subject:
            raise ApprovalWorkflowError("no_effective_authorization")
        assert prepared == _department_prepared(target)
        assert interrupt_id.startswith(target.thread_id)
        self.decisions.append((target.department, resume))
        return DepartmentDecisionReceipt(
            decision_id=resume.decision_id,
            revision_id=target.revision_id,
            processing_status="queued",
            application_key="decision-apply:" + "e" * 64,
            replayed=False,
        )

    def apply_decision(self, *, decision_id, authentication):
        department, resume = next(
            item for item in self.decisions if item[1].decision_id == decision_id
        )
        self.applications.append((department, decision_id))
        status = {
            "approve": "approved",
            "reject": "rejected",
            "request_changes": "changes_requested",
        }[resume.action]
        revision_id = {
            DepartmentId.warehouse: UUID(int=301),
            DepartmentId.lastmile: UUID(int=302),
            DepartmentId.reverse: UUID(int=303),
        }[department]
        return DepartmentApplicationReceipt(
            decision_id=decision_id,
            revision_id=revision_id,
            action=resume.action,
            processing_status="applied",
            revision_status=status,
            revision_state_version=5,
            replayed=False,
        )

    def coordinate_completion(self, target):
        return ApprovalCoordinationReceipt(
            approval_run_id=target.approval_run_id,
            ticket_id=target.ticket_id,
            outcome="waiting",
            blockers=("offline_fixture_not_finalizable",),
            final_document_id=None,
            replayed=False,
            coordinated_at=NOW,
        )


def test_three_departments_interrupt_and_resume_independently() -> None:
    backend = _IndependentDepartmentBackend()
    workflow = DepartmentApprovalWorkflow(
        backend=backend, checkpointer=InMemorySaver()
    )
    warehouse = _department_target(DepartmentId.warehouse, UUID(int=301))
    last_mile = _department_target(DepartmentId.lastmile, UUID(int=302))
    reverse = _department_target(DepartmentId.reverse, UUID(int=303))

    warehouse_start = workflow.start(warehouse)
    last_mile_start = workflow.start(last_mile)
    reverse_start = workflow.start(reverse)
    assert warehouse_start["__interrupt__"][0].value["department"] == "warehouse"
    assert last_mile_start["__interrupt__"][0].value["department"] == "lastmile"
    assert reverse_start["__interrupt__"][0].value["department"] == "reverse"

    last_mile_result = workflow.resume(
        thread_id=last_mile.thread_id,
        command=_resume(310),
        authentication=_authentication("carlos-subject"),
    )

    assert last_mile_result["status"] == "decision_applied"
    assert last_mile_result["decision"]["processing_status"] == "queued"
    assert last_mile_result["application"]["revision_status"] == "approved"
    assert workflow.state(last_mile.thread_id).next == ()
    for pending in (warehouse, reverse):
        pending_state = workflow.state(pending.thread_id)
        assert pending_state.values["status"] == "awaiting_human_decision"
        assert pending_state.tasks[0].interrupts

    warehouse_result = workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(320),
        authentication=_authentication("ana-subject"),
    )
    assert warehouse_result["application"]["revision_status"] == "approved"
    assert workflow.state(warehouse.thread_id).next == ()
    assert workflow.state(reverse.thread_id).tasks[0].interrupts

    reverse_result = workflow.resume(
        thread_id=reverse.thread_id,
        command=_resume(330),
        authentication=_authentication("sofia-subject"),
    )
    assert reverse_result["application"]["revision_status"] == "approved"
    assert workflow.state(reverse.thread_id).next == ()
    assert backend.decisions == [
        (DepartmentId.lastmile, _resume(310)),
        (DepartmentId.warehouse, _resume(320)),
        (DepartmentId.reverse, _resume(330)),
    ]
    assert backend.applications == [
        (DepartmentId.lastmile, UUID(int=310)),
        (DepartmentId.warehouse, UUID(int=320)),
        (DepartmentId.reverse, UUID(int=330)),
    ]
    assert backend.ticket_status == "esperando_aprobación"


@pytest.mark.parametrize(
    "department,subject",
    [
        (DepartmentId.reverse, "carlos-subject"),
        (DepartmentId.warehouse, "sofia-subject"),
    ],
)
def test_cross_department_resume_is_rejected(department, subject) -> None:
    backend = _IndependentDepartmentBackend()
    workflow = DepartmentApprovalWorkflow(
        backend=backend, checkpointer=InMemorySaver()
    )
    target = _department_target(department, UUID(int=350))
    workflow.start(target)

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=target.thread_id,
            command=_resume(351),
            authentication=_authentication(subject),
        )

    assert raised.value.code == "no_effective_authorization"
    assert backend.decisions == []
    assert workflow.state(target.thread_id).tasks[0].interrupts


@pytest.mark.parametrize(
    "action,reason",
    [("reject", "La sección no es aprobable."), ("request_changes", "Corregir SLA.")],
)
def test_reject_and_request_changes_remain_distinct_applied_actions(
    action, reason
) -> None:
    backend = _IndependentDepartmentBackend()
    workflow = DepartmentApprovalWorkflow(
        backend=backend, checkpointer=InMemorySaver()
    )
    target = _department_target(DepartmentId.lastmile, UUID(int=320))
    workflow.start(target)
    command = HumanResumeEnvelope(
        action=action,
        reason=reason,
        decision_id=UUID(int=321),
        idempotency_key=UUID(int=322),
        decided_at=NOW,
    )

    result = workflow.resume(
        thread_id=target.thread_id,
        command=command,
        authentication=_authentication("carlos-subject"),
    )

    assert result["status"] == "decision_applied"
    assert result["resume"]["action"] == action
    assert result["resume"]["action"] != "approve"
    assert result["application"]["revision_status"] == {
        "reject": "rejected",
        "request_changes": "changes_requested",
    }[action]
    assert backend.decisions == [(DepartmentId.lastmile, command)]


def test_carlos_cannot_resume_warehouse_branch() -> None:
    backend = _IndependentDepartmentBackend()
    workflow = DepartmentApprovalWorkflow(
        backend=backend, checkpointer=InMemorySaver()
    )
    warehouse = _department_target(DepartmentId.warehouse, UUID(int=330))
    workflow.start(warehouse)

    with pytest.raises(ApprovalWorkflowError) as raised:
        workflow.resume(
            thread_id=warehouse.thread_id,
            command=_resume(331),
            authentication=_authentication("carlos-subject"),
        )

    assert raised.value.code == "no_effective_authorization"
    assert backend.decisions == []
    assert workflow.state(warehouse.thread_id).tasks[0].interrupts


def test_stale_application_is_not_reported_as_applied() -> None:
    state = DepartmentApprovalWorkflow._decision_applied(
        {"application": {"processing_status": "rejected_stale"}}
    )

    assert state["status"] == "decision_rejected_stale"


def test_checkpoint_history_traces_transitions_without_authenticated_identity() -> None:
    backend = _IndependentDepartmentBackend()
    workflow = DepartmentApprovalWorkflow(
        backend=backend, checkpointer=InMemorySaver()
    )
    target = _department_target(DepartmentId.lastmile, UUID(int=340))
    workflow.start(target)
    workflow.resume(
        thread_id=target.thread_id,
        command=_resume(341),
        authentication=_authentication("carlos-subject"),
    )

    history = list(
        workflow.graph.get_state_history(
            {"configurable": {"thread_id": target.thread_id}}
        )
    )
    statuses = {snapshot.values.get("status") for snapshot in history}
    assert {
        "starting",
        "awaiting_human_decision",
        "decision_registered",
        "decision_applied",
    } <= statuses
    assert {snapshot.metadata["source"] for snapshot in history} <= {
        "input",
        "loop",
    }
    assert all(isinstance(snapshot.metadata["step"], int) for snapshot in history)
    assert all(snapshot.created_at for snapshot in history)
    assert all("authentication" not in snapshot.values for snapshot in history)


def test_existing_claim_api_cannot_select_a_target_decision() -> None:
    parameters = signature(claim_next_decision).parameters
    assert "decision_id" not in parameters
    assert {"session", "execution_id", "lease_seconds", "claim_token"} <= set(
        parameters
    )


class _CompletionBackend(_IndependentDepartmentBackend):
    """Offline orchestration double; PostgreSQL correctness has separate gates."""

    def __init__(self, *, conflict_open: bool = False) -> None:
        super().__init__()
        self.active_departments = {
            DepartmentId.warehouse,
            DepartmentId.lastmile,
        }
        self.approved_departments: set[DepartmentId] = set()
        self.conflict_open = conflict_open
        self.final_document_id = UUID(int=390)
        self.final_document_count = 0
        self.finalization_event_count = 0
        self.run_status = "active"
        self.coordination_calls = 0
        self.fail_ready_coordination_once = False

    def apply_decision(self, *, decision_id, authentication):
        department, resume = next(
            item for item in self.decisions if item[1].decision_id == decision_id
        )
        result = super().apply_decision(
            decision_id=decision_id, authentication=authentication
        )
        if resume.action == "approve":
            self.approved_departments.add(department)
        else:
            self.approved_departments.discard(department)
        return result

    def coordinate_completion(self, target):
        self.coordination_calls += 1
        blockers = {
            f"department_not_approved:{department.value}"
            for department in self.active_departments - self.approved_departments
        }
        if self.conflict_open:
            blockers.add("open_arbitration_conflict")
        if blockers:
            return ApprovalCoordinationReceipt(
                approval_run_id=target.approval_run_id,
                ticket_id=target.ticket_id,
                outcome="waiting",
                blockers=tuple(sorted(blockers)),
                final_document_id=None,
                replayed=False,
                coordinated_at=NOW,
            )
        if self.fail_ready_coordination_once:
            self.fail_ready_coordination_once = False
            raise ApprovalWorkflowError("synthetic_post_commit_interruption")
        replayed = self.final_document_count == 1
        self.final_document_count = 1
        if not replayed:
            self.finalization_event_count = 3
            self.ticket_status = "terminado"
            self.run_status = "completed"
        return ApprovalCoordinationReceipt(
            approval_run_id=target.approval_run_id,
            ticket_id=target.ticket_id,
            outcome="finalized",
            blockers=(),
            final_document_id=self.final_document_id,
            replayed=replayed,
            coordinated_at=NOW,
        )


def _completion_workflow(*, conflict_open: bool = False, saver=None):
    backend = _CompletionBackend(conflict_open=conflict_open)
    checkpoint = saver or InMemorySaver()
    workflow = DepartmentApprovalWorkflow(backend=backend, checkpointer=checkpoint)
    warehouse = _department_target(DepartmentId.warehouse, UUID(int=401))
    last_mile = _department_target(DepartmentId.lastmile, UUID(int=402))
    workflow.start(warehouse)
    workflow.start(last_mile)
    return workflow, backend, checkpoint, warehouse, last_mile


def test_partial_then_last_approval_runs_explicit_fan_in_and_finalization_node() -> None:
    workflow, backend, _, warehouse, last_mile = _completion_workflow()

    partial = workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(410),
        authentication=_authentication("ana-subject"),
    )

    assert partial["status"] == "decision_applied"
    assert partial["coordination"]["outcome"] == "waiting"
    assert partial["coordination"]["blockers"] == [
        "department_not_approved:lastmile"
    ]
    assert partial["coordination"]["final_document_id"] is None
    assert backend.final_document_count == 0
    assert backend.finalization_event_count == 0
    assert backend.ticket_status == "esperando_aprobación"
    assert backend.run_status == "active"
    assert workflow.state(last_mile.thread_id).tasks[0].interrupts

    finalized = workflow.resume(
        thread_id=last_mile.thread_id,
        command=_resume(420),
        authentication=_authentication("carlos-subject"),
    )

    assert finalized["status"] == "finalized"
    assert finalized["coordination"] == {
        "approval_run_id": str(RUN_ID),
        "ticket_id": str(TICKET_ID),
        "outcome": "finalized",
        "blockers": [],
        "final_document_id": str(backend.final_document_id),
        "replayed": False,
        "coordinated_at": NOW.isoformat(),
    }
    assert backend.final_document_count == 1
    assert backend.finalization_event_count == 3
    assert backend.ticket_status == "terminado"
    assert backend.run_status == "completed"
    history = list(
        workflow.graph.get_state_history(
            {"configurable": {"thread_id": last_mile.thread_id}}
        )
    )
    assert any(
        snapshot.values.get("coordination", {}).get("outcome") == "finalized"
        for snapshot in history
    )


def test_open_conflict_keeps_all_approvals_and_blocks_finalization() -> None:
    workflow, backend, _, warehouse, last_mile = _completion_workflow(
        conflict_open=True
    )
    workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(430),
        authentication=_authentication("ana-subject"),
    )

    blocked = workflow.resume(
        thread_id=last_mile.thread_id,
        command=_resume(440),
        authentication=_authentication("carlos-subject"),
    )

    assert blocked["status"] == "decision_applied"
    assert blocked["coordination"]["outcome"] == "waiting"
    assert blocked["coordination"]["blockers"] == ["open_arbitration_conflict"]
    assert backend.approved_departments == backend.active_departments
    assert backend.final_document_count == 0
    assert backend.finalization_event_count == 0
    assert backend.ticket_status == "esperando_aprobación"
    assert backend.run_status == "active"


@pytest.mark.parametrize(
    "action,reason,expected_status",
    [
        ("reject", "No aprobar.", "rejected"),
        ("request_changes", "Corregir sección.", "changes_requested"),
    ],
)
def test_non_approval_actions_never_trigger_final_document(
    action, reason, expected_status
) -> None:
    workflow, backend, _, warehouse, last_mile = _completion_workflow()
    workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(450),
        authentication=_authentication("ana-subject"),
    )
    command = HumanResumeEnvelope(
        action=action,
        reason=reason,
        decision_id=UUID(int=460),
        idempotency_key=UUID(int=461),
        decided_at=NOW,
    )

    result = workflow.resume(
        thread_id=last_mile.thread_id,
        command=command,
        authentication=_authentication("carlos-subject"),
    )

    assert result["application"]["revision_status"] == expected_status
    assert result["coordination"]["outcome"] == "waiting"
    assert result["coordination"]["blockers"] == [
        "department_not_approved:lastmile"
    ]
    assert backend.final_document_count == 0
    assert backend.finalization_event_count == 0
    assert backend.ticket_status == "esperando_aprobación"
    assert backend.run_status == "active"


def test_reconcile_replays_finalization_without_duplicate_decisions() -> None:
    workflow, backend, _, warehouse, last_mile = _completion_workflow()
    workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(470),
        authentication=_authentication("ana-subject"),
    )
    workflow.resume(
        thread_id=last_mile.thread_id,
        command=_resume(480),
        authentication=_authentication("carlos-subject"),
    )

    first_replay = workflow.reconcile(last_mile.thread_id)
    second_replay = workflow.reconcile(last_mile.thread_id)

    assert first_replay["coordination"]["replayed"] is True
    assert second_replay["coordination"]["replayed"] is True
    assert backend.final_document_count == 1
    assert backend.finalization_event_count == 3
    assert backend.ticket_status == "terminado"
    assert backend.run_status == "completed"
    assert len(backend.decisions) == 2
    assert len(backend.applications) == 2
    with pytest.raises(ApprovalWorkflowError) as duplicate_resume:
        workflow.resume(
            thread_id=last_mile.thread_id,
            command=_resume(480),
            authentication=_authentication("carlos-subject"),
        )
    assert duplicate_resume.value.code == "workflow_not_interrupted"


def test_reconcile_recovers_after_commit_before_finalization_without_new_decision() -> None:
    workflow, backend, saver, warehouse, last_mile = _completion_workflow()
    workflow.resume(
        thread_id=warehouse.thread_id,
        command=_resume(490),
        authentication=_authentication("ana-subject"),
    )
    backend.fail_ready_coordination_once = True

    with pytest.raises(ApprovalWorkflowError) as interrupted:
        workflow.resume(
            thread_id=last_mile.thread_id,
            command=_resume(500),
            authentication=_authentication("carlos-subject"),
        )
    assert interrupted.value.code == "synthetic_post_commit_interruption"
    assert backend.approved_departments == backend.active_departments
    assert backend.final_document_count == 0
    assert backend.finalization_event_count == 0
    assert backend.ticket_status == "esperando_aprobación"
    assert backend.run_status == "active"
    decision_count = len(backend.decisions)
    application_count = len(backend.applications)

    recovered = DepartmentApprovalWorkflow(backend=backend, checkpointer=saver)
    result = recovered.reconcile(last_mile.thread_id)

    assert result["status"] == "finalized"
    assert result["coordination"]["final_document_id"] == str(
        backend.final_document_id
    )
    assert backend.final_document_count == 1
    assert backend.finalization_event_count == 3
    assert backend.ticket_status == "terminado"
    assert backend.run_status == "completed"
    assert len(backend.decisions) == decision_count
    assert len(backend.applications) == application_count
