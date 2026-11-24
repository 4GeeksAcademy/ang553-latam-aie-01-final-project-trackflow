from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.contracts import (
    ApprovalRunStart,
    AuthenticatedPrincipal,
    AuthorizedHumanActor,
    DecisionApplicationCommand,
    DepartmentRevision,
    HumanDecision,
    decision_application_key,
    indicative_approval_reason,
)
from services.rfp_approval.authorization import (
    AuthorizationDenied,
    AuthorizationReference,
    EffectiveAuthorization,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.store import (
    ApprovalStoreError,
    _insert_event,
    _persisted_revision_creation_reason,
    _safe_ref,
    apply_claimed_decision,
    approval_start_request_hash,
    claim_decision_by_id,
    claim_next_decision,
    link_initial_validation_bundle,
    register_human_decision,
    start_approval_run,
)


NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
TICKET_ID = UUID("10000000-0000-0000-0000-000000000002")
RESPONSE_RUN_ID = UUID("10000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("10000000-0000-0000-0000-000000000004")
DRAFT_ID = UUID("10000000-0000-0000-0000-000000000005")
EVALUATION_ID = UUID("10000000-0000-0000-0000-000000000006")
DECISION_ID = UUID("10000000-0000-0000-0000-000000000007")
PRINCIPAL_ID = UUID("10000000-0000-0000-0000-000000000008")
BINDING_ID = UUID("10000000-0000-0000-0000-000000000009")
AUTHORIZATION_ID = UUID("10000000-0000-0000-0000-000000000010")
IDEMPOTENCY_ID = UUID("10000000-0000-0000-0000-000000000011")
PREPARATION_ID = UUID("10000000-0000-0000-0000-000000000012")
EXECUTION_ID = UUID("10000000-0000-0000-0000-000000000013")
CLAIM_TOKEN = UUID("10000000-0000-0000-0000-000000000014")
BUNDLE_ID = UUID("10000000-0000-0000-0000-000000000015")
H64 = "a" * 64
H64_B = "b" * 64


class _Result:
    def __init__(self, value=None, *, rowcount: int = 1) -> None:
        self.value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def one(self):
        if self.value is None:
            raise AssertionError("expected one row")
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        if self.value is None:
            raise AssertionError("expected one scalar")
        return self.value

    def all(self):
        return self.value


class _Session:
    def __init__(self, *results: _Result) -> None:
        self.results = list(results)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if not self.results:
            raise AssertionError(f"unexpected SQL statement: {statement}")
        return self.results.pop(0)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        issuer="https://identity.trackflow.example/realms/trackflow",
        subject="subject-ana",
        trackflow_user_id="user-ana",
        auth_source="oidc",
        general_role="user",
    )


def _identity(
    responsible: str = "ana_whitfield",
    *,
    principal_id: UUID = PRINCIPAL_ID,
    binding_id: UUID = BINDING_ID,
) -> ResolvedIdentity:
    return ResolvedIdentity._from_persisted_binding(
        environment="production",
        auth_source="oidc",
        issuer=_principal().issuer,
        subject=_principal().subject,
        trackflow_user_id=_principal().trackflow_user_id,
        general_role="user",
        principal_id=principal_id,
        binding_id=binding_id,
        responsible_code=responsible,
        synthetic=False,
    )


def _authorization() -> EffectiveAuthorization:
    return EffectiveAuthorization(
        authorization_id=AUTHORIZATION_ID,
        authorization_version=3,
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        environment="production",
        permission="approve_department",
        department_scope="warehouse",
        trigger_scope=None,
        target_policy="department_exact",
        evaluated_at=NOW,
    )


def _command(*, action: str = "approve", reason: str | None = None):
    actor = AuthorizedHumanActor(
        principal=_principal(),
        authorization_id=AUTHORIZATION_ID,
        official_responsible="Ana Whitfield",
        permission="approve_department",
        department_scope=DepartmentId.warehouse,
    )
    decision = HumanDecision(
        decision_id=DECISION_ID,
        idempotency_key=IDEMPOTENCY_ID,
        ticket_id=TICKET_ID,
        approval_run_id=RUN_ID,
        department=DepartmentId.warehouse,
        revision_id=REVISION_ID,
        draft_id=DRAFT_ID,
        expected_content_hash=H64,
        interrupt_id="interrupt-warehouse-1",
        action=action,
        reason=reason,
        actor=actor,
        decided_at=NOW,
    )
    provisional = DecisionApplicationCommand.model_construct(
        decision=decision,
        preparation_id=PREPARATION_ID,
        expected_revision_state="pending_approval",
        application_key="decision-apply:" + "0" * 64,
    )
    return DecisionApplicationCommand(
        decision=decision,
        preparation_id=PREPARATION_ID,
        application_key=decision_application_key(decision, provisional.preparation_id),
    )


def _revision_row(**changes):
    row = {
        "revision_id": REVISION_ID,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "department_id": "warehouse",
        "is_current": True,
        "status": "pending_approval",
        "content_hash": H64,
        "state_version": 4,
        "applied_decision_id": None,
        "validation_payload": {"eligible_for_approval": True},
    }
    row.update(changes)
    return row


def _queued_row(command=None, **changes):
    command = command or _command()
    row = {
        "decision_id": DECISION_ID,
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "revision_id": REVISION_ID,
        "department_id": "warehouse",
        "idempotency_key": IDEMPOTENCY_ID,
        "application_key": command.application_key,
        "expected_content_hash": H64,
        "action": command.decision.action,
        "reason": command.decision.reason,
        "processing_status": "queued",
        "actor_principal_id": PRINCIPAL_ID,
        "actor_binding_id": BINDING_ID,
        "authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "production",
        "claim_token": None,
        "claim_epoch": 0,
        "lease_until": None,
    }
    row.update(changes)
    return row


def _claimed_row(**changes):
    row = _queued_row(
        processing_status="claimed",
        claim_token=CLAIM_TOKEN,
        claim_epoch=2,
        claimed_at=NOW,
        lease_until=NOW + timedelta(minutes=1),
    )
    row.update(changes)
    return row


def _patch_authorization(monkeypatch, result=None):
    value = result or _authorization()
    monkeypatch.setattr(
        "services.rfp_approval.store._authorize_after_locks",
        lambda *args, **kwargs: value,
    )


def test_safe_audit_projection_is_closed_and_rejects_nested_payloads() -> None:
    safe = _safe_ref(
        "claim_output",
        decision_id=DECISION_ID,
        claim_epoch=2,
        lease_until=NOW,
        revision_state_version=4,
    )
    assert set(safe) == {
        "decision_id",
        "claim_epoch",
        "lease_until",
        "revision_state_version",
    }
    assert safe["decision_id"] == str(DECISION_ID)
    with pytest.raises(ApprovalStoreError) as unexpected:
        _safe_ref("claim_output", token="secret")
    assert unexpected.value.code == "unsafe_audit_reference_key"
    with pytest.raises(ApprovalStoreError) as nested:
        _safe_ref("claim_output", decision_id={"prompt": "raw"})
    assert nested.value.code == "unsafe_audit_reference_value"


@pytest.mark.parametrize("automate", [False, True])
def test_run_start_audit_projection_preserves_boolean_and_traceability(
    automate,
) -> None:
    safe = _safe_ref(
        "run_start_input",
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_version="rfp-response-v1",
        response_handoff_hash=H64,
        idempotency_key=IDEMPOTENCY_ID,
        request_hash=H64_B,
        active_departments=["warehouse"],
        automate_proposal_materialization=automate,
    )
    assert safe == {
        "response_run_id": str(RESPONSE_RUN_ID),
        "response_handoff_version": "rfp-response-v1",
        "response_handoff_hash": H64,
        "idempotency_key": str(IDEMPOTENCY_ID),
        "request_hash": H64_B,
        "active_departments": ["warehouse"],
        "automate_proposal_materialization": automate,
    }
    assert type(safe["automate_proposal_materialization"]) is bool


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", "true", []])
def test_run_start_audit_projection_requires_boolean_automation(invalid) -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        _safe_ref(
            "run_start_input",
            response_run_id=RESPONSE_RUN_ID,
            response_handoff_version="rfp-response-v1",
            response_handoff_hash=H64,
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64_B,
            active_departments=["warehouse"],
            automate_proposal_materialization=invalid,
        )
    assert raised.value.code == "unsafe_audit_reference_value"


def test_run_start_audit_projection_rejects_additional_keys() -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        _safe_ref(
            "run_start_input",
            response_run_id=RESPONSE_RUN_ID,
            response_handoff_version="rfp-response-v1",
            response_handoff_hash=H64,
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64_B,
            active_departments=["warehouse"],
            automate_proposal_materialization=False,
            raw_payload={"secret": "not-a-reference"},
        )
    assert raised.value.code == "unsafe_audit_reference_key"


def test_start_hash_binds_contract_revisions_and_idempotency_key() -> None:
    start = ApprovalRunStart.model_construct(approval_run_id=RUN_ID)
    revision = DepartmentRevision.model_construct(revision_id=REVISION_ID)
    first = approval_start_request_hash(start, [revision], IDEMPOTENCY_ID)
    second = approval_start_request_hash(
        start,
        [DepartmentRevision.model_construct(revision_id=UUID(int=99))],
        IDEMPOTENCY_ID,
    )
    automated = approval_start_request_hash(
        start.model_copy(update={"automate_proposal_materialization": True}),
        [revision],
        IDEMPOTENCY_ID,
    )
    assert len(first) == 64
    assert first != second
    assert first != automated


def test_initial_bundle_link_is_authorized_hash_bound_and_replay_safe(
    monkeypatch,
) -> None:
    management = EffectiveAuthorization(
        authorization_id=AUTHORIZATION_ID,
        authorization_version=3,
        principal_id=PRINCIPAL_ID,
        binding_id=BINDING_ID,
        environment="production",
        permission="start_approval_run",
        department_scope=None,
        trigger_scope=None,
        target_policy="run",
        evaluated_at=NOW,
    )
    _patch_authorization(monkeypatch, management)
    events = []
    monkeypatch.setattr(
        "services.rfp_approval.store._insert_event",
        lambda *args, **kwargs: events.append(kwargs),
    )
    run = {"status": "active"}
    revision = {
        "revision_id": REVISION_ID,
        "review_revision_no": 1,
        "content_payload": {"creation_reason": "part2_import"},
        "is_current": True,
        "status": "pending_assessment",
        "applied_decision_id": None,
        "department_id": "warehouse",
        "content_hash": H64,
        "validation_hash": H64_B,
        "validation_bundle_id": None,
        "state_version": 0,
    }
    assessments = [
        {
            "trigger_id": trigger,
            "assessed_content_hashes": [
                {"scope_key": "warehouse", "content_hash": H64}
            ],
        }
        for trigger in (
            "volume-vs-capacity",
            "returns-sla-breach",
            "currency-mismatch",
        )
    ]
    session = _Session(
        _Result(run),
        _Result([revision]),
        _Result(assessments),
        _Result(None),
        _Result({"validation_bundle_id": BUNDLE_ID}),
        _Result(rowcount=1),
    )
    result = link_initial_validation_bundle(
        session,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        revision_id=REVISION_ID,
        validation_bundle_id=BUNDLE_ID,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        permission="start_approval_run",
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is False
    assert result.validation_bundle_id == BUNDLE_ID
    assert events[0]["event_type"] == "initial_validation_bundle_linked"
    assert events[0]["safe_input_ref"] == {
        "revision_id": str(REVISION_ID),
        "validation_bundle_id": str(BUNDLE_ID),
        "content_hash": H64,
        "validation_hash": H64_B,
    }
    compiled = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement, _params in session.statements
    ]
    assert any("FOR UPDATE" in sql for sql in compiled[:2])
    assert "validation_bundle_id IS NULL" in compiled[5]

    replay_revision = {
        **revision,
        "validation_bundle_id": BUNDLE_ID,
        "status": "pending_approval",
        "state_version": 1,
    }
    replay = link_initial_validation_bundle(
        _Session(
            _Result(run),
            _Result([replay_revision]),
            _Result(assessments),
            _Result(None),
            _Result({"validation_bundle_id": BUNDLE_ID}),
        ),
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        revision_id=REVISION_ID,
        validation_bundle_id=BUNDLE_ID,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        permission="start_approval_run",
        execution_id=EXECUTION_ID,
    )
    assert replay.replayed is True


def test_initial_bundle_link_rejects_cross_content_or_ineligible_bundle(
    monkeypatch,
) -> None:
    _patch_authorization(
        monkeypatch,
        EffectiveAuthorization(
            authorization_id=AUTHORIZATION_ID,
            authorization_version=3,
            principal_id=PRINCIPAL_ID,
            binding_id=BINDING_ID,
            environment="production",
            permission="start_approval_run",
            department_scope=None,
            trigger_scope=None,
            target_policy="run",
            evaluated_at=NOW,
        ),
    )
    revision = {
        "revision_id": REVISION_ID,
        "review_revision_no": 1,
        "content_payload": {"creation_reason": "part2_import"},
        "is_current": True,
        "status": "pending_assessment",
        "applied_decision_id": None,
        "department_id": "warehouse",
        "content_hash": H64,
        "validation_hash": H64_B,
        "validation_bundle_id": None,
        "state_version": 0,
    }
    assessments = [
        {
            "trigger_id": trigger,
            "assessed_content_hashes": [
                {"scope_key": "warehouse", "content_hash": H64}
            ],
        }
        for trigger in (
            "volume-vs-capacity",
            "returns-sla-breach",
            "currency-mismatch",
        )
    ]
    with pytest.raises(ApprovalStoreError) as raised:
        link_initial_validation_bundle(
            _Session(
                _Result({"status": "active"}),
                _Result([revision]),
                _Result(assessments),
                _Result(None),
                _Result(None),
            ),
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            revision_id=REVISION_ID,
            validation_bundle_id=BUNDLE_ID,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            permission="start_approval_run",
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "initial_validation_bundle_mismatch"


def test_initial_bundle_link_requires_all_three_current_conflict_clearances(
    monkeypatch,
) -> None:
    revision = {
        "revision_id": REVISION_ID,
        "review_revision_no": 1,
        "content_payload": {"creation_reason": "part2_import"},
        "is_current": True,
        "status": "pending_assessment",
        "applied_decision_id": None,
        "department_id": "warehouse",
        "content_hash": H64,
        "validation_hash": H64_B,
        "validation_bundle_id": None,
        "state_version": 0,
    }
    only_two = [
        {
            "trigger_id": trigger,
            "assessed_content_hashes": [
                {"scope_key": "warehouse", "content_hash": H64}
            ],
        }
        for trigger in ("volume-vs-capacity", "returns-sla-breach")
    ]
    with pytest.raises(ApprovalStoreError) as raised:
        link_initial_validation_bundle(
            _Session(
                _Result({"status": "active"}),
                _Result([revision]),
                _Result(only_two),
            ),
            approval_run_id=RUN_ID,
            ticket_id=TICKET_ID,
            revision_id=REVISION_ID,
            validation_bundle_id=BUNDLE_ID,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            permission="start_approval_run",
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "preapproval_conflict_clearance_required"


def test_persisted_revision_creation_reason_is_jsonb_bound_and_fail_closed() -> None:
    assert _persisted_revision_creation_reason(
        {"content_payload": {"creation_reason": "part2_import"}}
    ) == "part2_import"

    for row in (
        {"creation_reason": "part2_import"},
        {"content_payload": {}},
        {"content_payload": {"creation_reason": "unknown"}},
    ):
        with pytest.raises(ApprovalStoreError) as raised:
            _persisted_revision_creation_reason(row)
        assert raised.value.code == "revision_creation_reason_invalid"


def test_incorrect_handoff_is_rejected_before_any_insert(monkeypatch) -> None:
    start = SimpleNamespace(
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        initial_revisions=[SimpleNamespace(department=DepartmentId.warehouse)],
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_version="rfp-response-v1",
        response_handoff_hash=H64,
    )
    revision = SimpleNamespace(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        department=DepartmentId.warehouse,
        source_part2_draft_id=DRAFT_ID,
        source_part2_draft_hash=H64,
        source_part2_evaluation_id=EVALUATION_ID,
        source_part2_evaluation_hash=H64_B,
        source_part2_iteration=1,
        review_revision_no=1,
        creation_reason="part2_import",
        supersedes_revision_id=None,
        created_by=None,
    )
    start.initial_revisions[0].source_draft_id = DRAFT_ID
    start.initial_revisions[0].source_draft_hash = H64
    start.initial_revisions[0].source_evaluation_id = EVALUATION_ID
    start.initial_revisions[0].source_evaluation_hash = H64_B
    start.initial_revisions[0].source_part2_iteration = 1
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64
    )
    session = _Session(
        _Result(None), _Result("en_evaluación"), _Result(None), _Result(None)
    )
    with pytest.raises(ApprovalStoreError) as raised:
        start_approval_run(
            session,
            start=start,
            initial_revisions=[revision],
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64,
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "response_handoff_not_found"
    assert len(session.statements) == 4


def test_identical_start_replay_returns_existing_run(monkeypatch) -> None:
    start = SimpleNamespace(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_hash=H64,
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        initial_revisions=[],
    )
    existing = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_RUN_ID,
        "response_handoff_hash": H64,
        "start_request_hash": H64_B,
        "status": "active",
        "state_version": 0,
        "started_by_principal_id": PRINCIPAL_ID,
        "started_by_binding_id": BINDING_ID,
        "start_authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "production",
    }
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64_B
    )
    _patch_authorization(monkeypatch)
    session = _Session(_Result(existing), _Result(existing))
    result = start_approval_run(
        session,
        start=start,
        initial_revisions=[],
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        idempotency_key=IDEMPOTENCY_ID,
        request_hash=H64_B,
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is True
    assert not any(
        "INSERT INTO rfp_workflow_events" in str(statement)
        for statement, _params in session.statements
    )


def test_start_rechecks_idempotency_after_waiting_for_ticket_lock(monkeypatch) -> None:
    start = SimpleNamespace(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_hash=H64,
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        initial_revisions=[],
    )
    existing = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_RUN_ID,
        "response_handoff_hash": H64,
        "start_request_hash": H64_B,
        "status": "active",
        "state_version": 0,
        "started_by_principal_id": PRINCIPAL_ID,
        "started_by_binding_id": BINDING_ID,
        "start_authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "production",
    }
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64_B
    )
    _patch_authorization(monkeypatch)
    session = _Session(
        _Result(None),
        _Result("en_evaluación"),
        _Result(existing),
        _Result(existing),
    )
    result = start_approval_run(
        session,
        start=start,
        initial_revisions=[],
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        idempotency_key=IDEMPOTENCY_ID,
        request_hash=H64_B,
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is True
    assert len(session.statements) == 4


def test_start_idempotency_key_reuse_with_other_request_is_rejected(monkeypatch) -> None:
    start = SimpleNamespace(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_hash=H64,
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        initial_revisions=[],
    )
    existing = {
        "approval_run_id": UUID(int=999),
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_RUN_ID,
        "response_handoff_hash": H64,
        "start_request_hash": H64_B,
    }
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64_B
    )
    with pytest.raises(ApprovalStoreError) as raised:
        start_approval_run(
            _Session(_Result(existing)),
            start=start,
            initial_revisions=[],
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64_B,
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "start_idempotency_key_reused"


@pytest.mark.parametrize(
    "revision_change,code",
    [
        ({"is_current": False}, "revision_obsolete"),
        ({"status": "approved"}, "revision_not_pending_approval"),
        ({"content_hash": H64_B}, "revision_content_hash_mismatch"),
        ({"validation_payload": {"eligible_for_approval": False}}, "revision_not_eligible"),
    ],
)
def test_decision_registration_rejects_invalid_revision_state(
    revision_change, code
) -> None:
    session = _Session(_Result(None), _Result("active"), _Result(_revision_row(**revision_change)))
    with pytest.raises(ApprovalStoreError) as raised:
        register_human_decision(
            session,
            command=_command(),
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == code


def test_decision_registration_rejects_department_without_effective_grant(monkeypatch) -> None:
    def denied(*args, **kwargs):
        raise AuthorizationDenied("no_effective_authorization")

    monkeypatch.setattr("services.rfp_approval.store._authorize_after_locks", denied)
    session = _Session(_Result(None), _Result("active"), _Result(_revision_row()))
    with pytest.raises(AuthorizationDenied) as raised:
        register_human_decision(
            session,
            command=_command(),
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "no_effective_authorization"


def test_decision_registration_requires_durable_ack_for_indicative_approval(
    monkeypatch,
) -> None:
    pending_code = "MONTHLY_STORAGE_VOLUME_REQUIRED"
    revision = _revision_row(
        content_payload={
            "pending_commercial_items": [{"code": pending_code}]
        }
    )
    with pytest.raises(ApprovalStoreError) as missing:
        register_human_decision(
            _Session(_Result(None), _Result("active"), _Result(revision)),
            command=_command(),
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert missing.value.code == "indicative_commercial_items_not_acknowledged"

    _patch_authorization(monkeypatch)
    session = _Session(
        _Result(None),
        _Result("active"),
        _Result(revision),
        _Result(DECISION_ID),
        _Result(),
    )
    accepted = register_human_decision(
        session,
        command=_command(reason=indicative_approval_reason([pending_code])),
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert accepted.processing_status == "queued"


def test_stale_official_assignment_uses_real_query_failure_code(monkeypatch) -> None:
    def denied(*args, **kwargs):
        raise AuthorizationDenied("no_effective_authorization")

    monkeypatch.setattr("services.rfp_approval.store._authorize_after_locks", denied)
    session = _Session(_Result(None), _Result("active"), _Result(_revision_row()))
    with pytest.raises(AuthorizationDenied) as raised:
        register_human_decision(
            session,
            command=_command(),
            identity=_identity("ana_whitfield"),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "no_effective_authorization"
    assert raised.value.code != "official_assignment_changed"


def test_decision_idempotency_replay_and_conflicting_reuse(monkeypatch) -> None:
    command = _command()
    _patch_authorization(monkeypatch)
    persisted = _queued_row(command)
    same = register_human_decision(
        _Session(
            _Result(persisted),
            _Result(RUN_ID),
            _Result(REVISION_ID),
            _Result(persisted),
        ),
        command=command,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert same.replayed is True
    conflicting = _queued_row(command, expected_content_hash=H64_B)
    with pytest.raises(ApprovalStoreError) as raised:
        register_human_decision(
            _Session(_Result(conflicting)),
            command=command,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "decision_idempotency_key_reused"


def test_successful_registration_inserts_queued_decision_and_allowlisted_event(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    session = _Session(
        _Result(None),
        _Result("active"),
        _Result(_revision_row()),
        _Result(DECISION_ID),
        _Result(),
    )
    result = register_human_decision(
        session,
        command=_command(),
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert result.processing_status == "queued"
    decision_sql = str(session.statements[3][0].compile(dialect=postgresql.dialect()))
    event_sql = str(session.statements[4][0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO rfp_human_decisions" in decision_sql
    assert "INSERT INTO rfp_workflow_events" in event_sql
    assert "ON CONFLICT (event_key) DO NOTHING" in event_sql


def test_claim_query_uses_skip_locked_lease_and_epoch() -> None:
    claimed = _claimed_row(lease_until=NOW + timedelta(minutes=1))
    session = _Session(_Result(claimed), _Result(4), _Result())
    result = claim_next_decision(
        session,
        execution_id=EXECUTION_ID,
        lease_seconds=60,
        claim_token=CLAIM_TOKEN,
    )
    assert result is not None
    assert result.claim_epoch == 2
    compiled = str(session.statements[0][0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "claim_epoch=(rfp_human_decisions.claim_epoch+" in compiled.replace(" ", "")
    assert "clock_timestamp()" in compiled


def test_claim_returns_none_when_queue_has_no_claimable_decision() -> None:
    result = claim_next_decision(
        _Session(_Result(None)), execution_id=EXECUTION_ID, claim_token=CLAIM_TOKEN
    )
    assert result is None


def test_claim_by_id_targets_only_the_requested_decision_with_same_fence() -> None:
    claimed = _claimed_row(lease_until=NOW + timedelta(minutes=1))
    session = _Session(_Result(claimed), _Result(4), _Result())

    result = claim_decision_by_id(
        session,
        decision_id=DECISION_ID,
        execution_id=EXECUTION_ID,
        lease_seconds=60,
        claim_token=CLAIM_TOKEN,
    )

    assert result is not None
    assert result.decision_id == DECISION_ID
    assert result.claim_token == CLAIM_TOKEN
    assert result.claim_epoch == 2
    compiled = str(session.statements[0][0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "rfp_human_decisions.decision_id =" in compiled
    assert "ORDER BY" not in compiled
    assert session.statements[0][1] == {"lease_seconds": 60}
    assert "claim_epoch=(rfp_human_decisions.claim_epoch+" in compiled.replace(" ", "")


def test_claim_by_id_does_not_fall_back_to_another_decision() -> None:
    result = claim_decision_by_id(
        _Session(_Result(None)),
        decision_id=DECISION_ID,
        execution_id=EXECUTION_ID,
        claim_token=CLAIM_TOKEN,
    )
    assert result is None


@pytest.mark.parametrize(
    "token,epoch,code",
    [
        (UUID(int=222), 2, "claim_token_mismatch"),
        (CLAIM_TOKEN, 1, "claim_epoch_mismatch"),
    ],
)
def test_application_rejects_invalid_claim_fence(token, epoch, code) -> None:
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=token,
            claim_epoch=epoch,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == code


def test_stale_revision_is_durably_rejected_without_approval() -> None:
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row(is_current=False)),
        _Result(_claimed_row()),
        _Result(4),
        _Result(True),
        _Result(rowcount=1),
        _Result(),
    )
    result = apply_claimed_decision(
        session,
        decision_id=DECISION_ID,
        claim_token=CLAIM_TOKEN,
        claim_epoch=2,
        expected_revision_state_version=4,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert result.processing_status == "rejected_stale"
    assert all(
        "UPDATE rfp_approval_department_revisions" not in str(item[0])
        for item in session.statements
    )


def test_application_rejects_state_version_not_bound_at_registration() -> None:
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
        _Result(4),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=5,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "revision_state_version_reference_mismatch"


@pytest.mark.parametrize("reason", ["authorization_revoked", "authorization_expired"])
def test_application_rechecks_revocation_and_expiry_after_locks(monkeypatch, reason) -> None:
    def denied(*args, **kwargs):
        raise AuthorizationDenied(reason)

    monkeypatch.setattr("services.rfp_approval.store._authorize_after_locks", denied)
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
        _Result(4),
        _Result(True),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == reason
    assert not any(
        "UPDATE rfp_approval_department_revisions" in str(item[0])
        for item in session.statements
    )


def test_successful_application_uses_revision_cas_and_decision_fence(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
        _Result(4),
        _Result(True),
        _Result(rowcount=1),
        _Result(rowcount=1),
        _Result(),
    )
    result = apply_claimed_decision(
        session,
        decision_id=DECISION_ID,
        claim_token=CLAIM_TOKEN,
        claim_epoch=2,
        expected_revision_state_version=4,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert result.revision_status == "approved"
    assert result.revision_state_version == 5
    revision_sql = str(session.statements[6][0].compile(dialect=postgresql.dialect()))
    decision_sql = str(session.statements[7][0].compile(dialect=postgresql.dialect()))
    assert "state_version" in revision_sql and "content_hash" in revision_sql
    assert "claim_token" in decision_sql and "claim_epoch" in decision_sql
    assert "lease_until > clock_timestamp()" in decision_sql


def test_application_retry_after_commit_is_idempotent(monkeypatch) -> None:
    decision = _claimed_row(processing_status="applied")
    revision = _revision_row(status="approved", applied_decision_id=DECISION_ID, state_version=5)
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(revision),
        _Result(decision),
    )
    _patch_authorization(monkeypatch)
    result = apply_claimed_decision(
        session,
        decision_id=DECISION_ID,
        claim_token=UUID(int=999),
        claim_epoch=1,
        expected_revision_state_version=4,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is True
    assert result.processing_status == "applied"


@pytest.mark.parametrize("run_status", ["blocked", "finalizing", "completed", "failed"])
def test_application_rejects_run_that_is_not_active(run_status) -> None:
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result(run_status),
        _Result(_revision_row()),
        _Result(_claimed_row()),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "approval_run_not_active"


def test_application_lock_modes_are_fk_compatible() -> None:
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
    )
    with pytest.raises(ApprovalStoreError):
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=UUID(int=222),
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    run_sql = str(session.statements[1][0].compile(dialect=postgresql.dialect()))
    revision_sql = str(session.statements[2][0].compile(dialect=postgresql.dialect()))
    decision_sql = str(session.statements[3][0].compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE" in run_sql
    assert "FOR NO KEY UPDATE" in revision_sql
    assert decision_sql.rstrip().endswith("FOR UPDATE")


def test_lease_is_rechecked_after_authorization_wait(monkeypatch) -> None:
    authorization_checked = []

    def authorized(*args, **kwargs):
        authorization_checked.append(True)
        return _authorization()

    monkeypatch.setattr(
        "services.rfp_approval.store._authorize_after_locks", authorized
    )
    session = _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("active"),
        _Result(_revision_row()),
        _Result(_claimed_row()),
        _Result(4),
        _Result(False),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert authorization_checked == [True]
    assert raised.value.code == "claim_lease_expired"


def test_reconstructed_envelope_with_same_idempotency_key_is_not_a_replay() -> None:
    original = _command()
    reconstructed_decision = original.decision.model_copy(
        update={"decision_id": UUID(int=987), "decided_at": NOW + timedelta(seconds=1)}
    )
    reconstructed = DecisionApplicationCommand(
        decision=reconstructed_decision,
        preparation_id=PREPARATION_ID,
        application_key=decision_application_key(
            reconstructed_decision, PREPARATION_ID
        ),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        register_human_decision(
            _Session(_Result(_queued_row(original))),
            command=reconstructed,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "decision_idempotency_key_reused"


def _event_row(**changes):
    row = {
        "event_key": "test-event:1",
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "thread_id": f"approval-run:{RUN_ID}",
        "execution_id": EXECUTION_ID,
        "sequence_no": 1,
        "node_name": "test_node",
        "agent_name": "rfp_approval_store",
        "event_type": "test_event",
        "safe_input_ref": {"decision_id": str(DECISION_ID)},
        "safe_output_ref": {"processing_status": "queued"},
        "result": "queued",
        "from_state": "pending_approval",
        "to_state": "pending_approval",
        "department_revision_id": REVISION_ID,
        "decision_id": DECISION_ID,
    }
    row.update(changes)
    return row


def _write_test_event(session) -> None:
    _insert_event(
        session,
        event_key="test-event:1",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        thread_id=f"approval-run:{RUN_ID}",
        execution_id=EXECUTION_ID,
        sequence_no=1,
        node_name="test_node",
        event_type="test_event",
        safe_input_ref={"decision_id": str(DECISION_ID)},
        safe_output_ref={"processing_status": "queued"},
        result="queued",
        from_state="pending_approval",
        to_state="pending_approval",
        revision_id=REVISION_ID,
        decision_id=DECISION_ID,
    )


def test_audit_event_conflict_requires_identical_persisted_event() -> None:
    _write_test_event(_Session(_Result(rowcount=0), _Result(_event_row())))
    with pytest.raises(ApprovalStoreError) as raised:
        _write_test_event(
            _Session(
                _Result(rowcount=0),
                _Result(_event_row(safe_output_ref={"processing_status": "applied"})),
            )
        )
    assert raised.value.code == "audit_event_collision"


def test_audit_event_conflict_without_existing_row_fails_closed() -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        _write_test_event(_Session(_Result(rowcount=0), _Result(None)))
    assert raised.value.code == "audit_event_insert_failed"


def _terminal_replay_session(*, status="applied") -> _Session:
    revision_status = "approved" if status == "applied" else "pending_approval"
    applied_decision = DECISION_ID if status == "applied" else None
    return _Session(
        _Result({"approval_run_id": RUN_ID, "revision_id": REVISION_ID}),
        _Result("completed"),
        _Result(
            _revision_row(
                status=revision_status,
                applied_decision_id=applied_decision,
                state_version=5,
            )
        ),
        _Result(_claimed_row(processing_status=status)),
    )


def test_terminal_replay_requires_same_current_actor_and_grant(monkeypatch) -> None:
    _patch_authorization(monkeypatch)
    session = _terminal_replay_session()
    result = apply_claimed_decision(
        session,
        decision_id=DECISION_ID,
        claim_token=UUID(int=999),
        claim_epoch=99,
        expected_revision_state_version=999,
        identity=_identity(),
        authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
        execution_id=EXECUTION_ID,
    )
    assert result.replayed is True
    assert not any(
        str(statement).lstrip().startswith(("UPDATE", "INSERT"))
        for statement, _ in session.statements
    )


@pytest.mark.parametrize(
    "identity",
    [
        _identity(principal_id=UUID(int=701)),
        _identity(binding_id=UUID(int=702)),
    ],
    ids=["other_principal", "other_binding"],
)
def test_terminal_replay_rejects_other_principal_or_binding(identity) -> None:
    session = _terminal_replay_session()
    with pytest.raises(ApprovalStoreError) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=identity,
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "decision_actor_mismatch"
    assert len(session.statements) == 4


@pytest.mark.parametrize(
    "denial_code",
    [
        "authorization_revoked",
        "authorization_expired",
        "principal_inactive",
        "binding_inactive",
        "no_effective_authorization",
    ],
)
def test_terminal_replay_denies_noncurrent_authorization_without_writes(
    monkeypatch, denial_code
) -> None:
    def denied(*args, **kwargs):
        raise AuthorizationDenied(denial_code)

    monkeypatch.setattr("services.rfp_approval.store._authorize_after_locks", denied)
    session = _terminal_replay_session()
    with pytest.raises(AuthorizationDenied) as raised:
        apply_claimed_decision(
            session,
            decision_id=DECISION_ID,
            claim_token=CLAIM_TOKEN,
            claim_epoch=2,
            expected_revision_state_version=4,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == denial_code
    assert not any(
        str(statement).lstrip().startswith(("UPDATE", "INSERT"))
        for statement, _ in session.statements
    )


def test_start_replay_rejects_wrong_persisted_initiator(monkeypatch) -> None:
    start = SimpleNamespace(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_hash=H64,
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        initial_revisions=[],
    )
    persisted = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_RUN_ID,
        "response_handoff_hash": H64,
        "start_request_hash": H64_B,
        "status": "active",
        "state_version": 0,
        "started_by_principal_id": PRINCIPAL_ID,
        "started_by_binding_id": BINDING_ID,
        "start_authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "production",
    }
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64_B
    )
    with pytest.raises(ApprovalStoreError) as raised:
        start_approval_run(
            _Session(_Result(persisted), _Result(persisted)),
            start=start,
            initial_revisions=[],
            identity=_identity(principal_id=UUID(int=801)),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64_B,
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "start_replay_actor_mismatch"


def test_registration_replay_rejects_revoked_actor(monkeypatch) -> None:
    def revoked(*args, **kwargs):
        raise AuthorizationDenied("authorization_revoked")

    monkeypatch.setattr("services.rfp_approval.store._authorize_after_locks", revoked)
    command = _command()
    persisted = _queued_row(command)
    session = _Session(
        _Result(persisted),
        _Result(RUN_ID),
        _Result(REVISION_ID),
        _Result(persisted),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        register_human_decision(
            session,
            command=command,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "authorization_revoked"
    assert len(session.statements) == 4


def test_registration_conflict_recovery_reauthorizes_before_return(monkeypatch) -> None:
    calls = []

    def authorize(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise AuthorizationDenied("authorization_revoked")
        return _authorization()

    monkeypatch.setattr(
        "services.rfp_approval.store._authorize_after_locks", authorize
    )
    command = _command()
    persisted = _queued_row(command)
    session = _Session(
        _Result(None),
        _Result("active"),
        _Result(_revision_row()),
        _Result(None),
        _Result(persisted),
        _Result(RUN_ID),
        _Result(REVISION_ID),
        _Result(persisted),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        register_human_decision(
            session,
            command=command,
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "authorization_revoked"
    assert calls == [True, True]
    assert not any(
        "INSERT INTO rfp_workflow_events" in str(statement)
        for statement, _ in session.statements
    )


def test_start_conflict_recovery_reauthorizes_before_return(monkeypatch) -> None:
    start = SimpleNamespace(
        contract_version="rfp-approval-start-v1",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        response_run_id=RESPONSE_RUN_ID,
        response_handoff_version="rfp-response-v1",
        response_handoff_hash=H64,
        active_departments=[],
        initial_revisions=[],
        max_review_revisions=3,
        started_by=SimpleNamespace(
            principal=_principal(),
            authorization_id=AUTHORIZATION_ID,
            permission="start_approval_run",
        ),
        started_at=NOW,
    )
    persisted = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_RUN_ID,
        "response_handoff_hash": H64,
        "start_request_hash": H64_B,
        "status": "active",
        "state_version": 0,
        "started_by_principal_id": PRINCIPAL_ID,
        "started_by_binding_id": BINDING_ID,
        "start_authorization_id": AUTHORIZATION_ID,
        "authorization_version": 3,
        "environment": "production",
    }
    handoff = SimpleNamespace(
        contract_version="rfp-response-v1",
        ticket_id=TICKET_ID,
        run_id=RESPONSE_RUN_ID,
        selected_departments=[],
        department_results=[],
        outcome="ready_for_part3",
    )
    monkeypatch.setattr(
        "services.rfp_approval.store.approval_start_request_hash", lambda *args: H64_B
    )
    monkeypatch.setattr(
        "services.rfp_approval.store.canonical_hash", lambda *args, **kwargs: H64
    )
    monkeypatch.setattr(
        "services.rfp_approval.store.RFPResponseHandoff.model_validate",
        lambda *args, **kwargs: handoff,
    )
    calls = []

    def authorize(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise AuthorizationDenied("authorization_revoked")
        return _authorization()

    monkeypatch.setattr(
        "services.rfp_approval.store._authorize_after_locks", authorize
    )
    handoff_row = {
        "payload": {},
        "payload_hash": H64,
        "contract_version": "rfp-response-v1",
    }
    session = _Session(
        _Result(None),
        _Result("en_evaluación"),
        _Result(None),
        _Result(handoff_row),
        _Result("ready_for_part3"),
        _Result(None),
        _Result(persisted),
        _Result(persisted),
    )
    with pytest.raises(AuthorizationDenied) as raised:
        start_approval_run(
            session,
            start=start,
            initial_revisions=[],
            identity=_identity(),
            authorization_reference=AuthorizationReference(AUTHORIZATION_ID, 3),
            idempotency_key=IDEMPOTENCY_ID,
            request_hash=H64_B,
            execution_id=EXECUTION_ID,
        )
    assert raised.value.code == "authorization_revoked"
    assert calls == [True, True]
    assert not any(
        "INSERT INTO rfp_workflow_events" in str(statement)
        for statement, _ in session.statements
    )
