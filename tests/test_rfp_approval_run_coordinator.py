from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import MissingCommercialInput, SafePreview
from services.rfp_approval.authorization import EffectiveAuthorization
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.run_coordinator import (
    _build_chain,
    initialize_approval_run_from_handoff,
)
from services.rfp_approval.store import ApprovalStoreError


NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)


def test_automation_cannot_be_delegated_by_start_only_grant() -> None:
    identity = _identity()
    authorization = EffectiveAuthorization(
        authorization_id=uuid4(),
        authorization_version=1,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="start_approval_run",
        department_scope=None,
        trigger_scope=None,
        target_policy="run",
        evaluated_at=NOW,
    )
    with pytest.raises(ApprovalStoreError) as raised:
        initialize_approval_run_from_handoff(
            SimpleNamespace(),
            ticket_id=uuid4(),
            response_run_id=None,
            identity=identity,
            authorization=authorization,
            idempotency_key=uuid4(),
            requested_at=NOW,
            automate_proposal_materialization=True,
        )
    assert raised.value.code == "proposal_automation_requires_run_management"


def _identity() -> ResolvedIdentity:
    return ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc:coordinator",
        subject="qa-coordinator",
        trackflow_user_id="qa-coordinator",
        general_role="manager",
        principal_id=uuid4(),
        binding_id=uuid4(),
        responsible_code=None,
        synthetic=True,
    )


def _authorization(identity: ResolvedIdentity) -> EffectiveAuthorization:
    return EffectiveAuthorization(
        authorization_id=uuid4(),
        authorization_version=1,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="manage_approval_run",
        department_scope=None,
        trigger_scope=None,
        target_policy="run",
        evaluated_at=NOW,
    )


def _source(preview_status="passed"):
    source_draft_id = uuid4()
    source_evaluation_id = uuid4()
    draft = SimpleNamespace(
        ticket_id=uuid4(),
        run_id=uuid4(),
        department=DepartmentId.warehouse,
        draft_id=source_draft_id,
        iteration_count=1,
        generated_content="Documented Warehouse response.",
        commercial_terms=[],
        pending_commercial_items=[],
        supported_claims=[],
        aspect_responses=[],
    )
    evaluation = SimpleNamespace(
        evaluation_id=source_evaluation_id,
        compliance=SimpleNamespace(passed=True),
        readability=SimpleNamespace(passed=True),
        relevance=SimpleNamespace(passed=True),
        blocking_rule_ids=[],
        blocks_commercial_completeness=False,
        overall_pass=True,
    )
    preview = SafePreview(
        preview_id=uuid4(),
        draft_id=source_draft_id,
        gate_status=preview_status,
        content=(
            "Documented Warehouse response."
            if preview_status == "passed"
            else None
        ),
        reason_codes=(
            [] if preview_status == "passed" else [f"preview_{preview_status}"]
        ),
    )
    return draft, evaluation, preview, "a" * 64, "b" * 64, "c" * 64, NOW


def _chain(approval_run_id, ticket_id):
    source = _source()
    source[0].ticket_id = ticket_id
    handoff = SimpleNamespace(ticket_id=ticket_id)
    department_result = SimpleNamespace(department=DepartmentId.warehouse)
    return _build_chain(
        approval_run_id=approval_run_id,
        handoff=handoff,
        department_result=department_result,
        source=source,
        execution_id=uuid4(),
    )


def test_clean_part2_source_builds_exact_reviewable_chain() -> None:
    approval_run_id, ticket_id = uuid4(), uuid4()
    chain = _chain(approval_run_id, ticket_id)
    assert chain.revision.status == "pending_assessment"
    assert chain.revision.content == "Documented Warehouse response."
    assert chain.revision.commercial_terms == []
    assert chain.revision.pending_commercial_items == []
    assert chain.revision.source_part2_draft_hash == "a" * 64
    assert chain.revision.source_part2_evaluation_hash == "b" * 64
    assert chain.bundle.eligible is True
    assert chain.preview.payload.gate_status == "passed"
    assert chain.preview.payload.rendered_content == (
        "Documented Warehouse response."
    )
    assert chain.preview.payload.rendered_content_hash is not None
    assert chain.preview.payload.reason_codes == []
    assert chain.revision.validation_bundle_id == chain.bundle.validation_bundle_id
    assert chain.draft.provenance.source_kind == "part2_import"
    assert chain.evaluation.provenance.source_kind == "part2_import"
    assert chain.preview.provenance.source_kind == "part2_import"
    assert chain.bundle.provenance.source_kind == "part2_import"
    assert chain.draft.provenance.source_artifact_ids == [
        chain.revision.source_part2_draft_id
    ]


def test_unsafe_preview_still_blocks_an_indicative_revision() -> None:
    approval_run_id, ticket_id = uuid4(), uuid4()
    source = list(_source())
    source[0].ticket_id = ticket_id
    source[0].pending_commercial_items = [
        MissingCommercialInput(
            code="MONTHLY_STORAGE_VOLUME_REQUIRED",
            description="Authorized monthly storage volume is required.",
            owner="Ana Whitfield",
            blocks_commercial_completeness=True,
        )
    ]
    source[1].blocks_commercial_completeness = True
    source[1].overall_pass = False
    source[2] = SafePreview(
        preview_id=uuid4(),
        draft_id=source[0].draft_id,
        gate_status="blocked",
        content=None,
        reason_codes=["missing_commercial_input"],
    )
    chain = _build_chain(
        approval_run_id=approval_run_id,
        handoff=SimpleNamespace(ticket_id=ticket_id),
        department_result=SimpleNamespace(department=DepartmentId.warehouse),
        source=tuple(source),
        execution_id=uuid4(),
    )
    assert chain.revision.status == "blocked_compliance"
    assert chain.bundle.eligible is False
    assert chain.preview.payload.gate_status == "blocked"
    assert chain.preview.payload.rendered_content is None
    assert chain.preview.payload.rendered_content_hash is None
    assert chain.preview.payload.reason_codes == ["missing_commercial_input"]
    assert chain.revision.commercial_terms == []
    assert chain.revision.pending_commercial_items[0].code == (
        "MONTHLY_STORAGE_VOLUME_REQUIRED"
    )


def test_declared_commercial_pending_item_reaches_assessment_without_invention() -> None:
    approval_run_id, ticket_id = uuid4(), uuid4()
    source = list(_source())
    source[0].ticket_id = ticket_id
    source[0].pending_commercial_items = [
        MissingCommercialInput(
            code="MONTHLY_STORAGE_VOLUME_REQUIRED",
            description="Average volume remains an open commercial question.",
            owner="Miguel Torres",
            blocks_commercial_completeness=True,
        )
    ]
    source[1].blocks_commercial_completeness = True
    source[1].overall_pass = False

    chain = _build_chain(
        approval_run_id=approval_run_id,
        handoff=SimpleNamespace(ticket_id=ticket_id),
        department_result=SimpleNamespace(department=DepartmentId.warehouse),
        source=tuple(source),
        execution_id=uuid4(),
    )

    assert chain.revision.status == "pending_assessment"
    assert chain.revision.validation.eligible_for_approval is True
    assert chain.revision.validation.blocks_commercial_completeness is True
    assert chain.revision.validation.unresolved_input_codes == [
        "MONTHLY_STORAGE_VOLUME_REQUIRED"
    ]


@pytest.mark.parametrize(
    "gate_status",
    ["blocked", "indeterminate", "technical_error"],
)
def test_nonpassed_part2_preview_preserves_fail_closed_state(gate_status) -> None:
    approval_run_id, ticket_id = uuid4(), uuid4()
    source = list(_source(gate_status))
    source[0].ticket_id = ticket_id
    source[1].compliance.passed = False
    source[1].blocking_rule_ids = [f"preview_{gate_status}"]
    source[1].overall_pass = False
    chain = _build_chain(
        approval_run_id=approval_run_id,
        handoff=SimpleNamespace(ticket_id=ticket_id),
        department_result=SimpleNamespace(department=DepartmentId.warehouse),
        source=tuple(source),
        execution_id=uuid4(),
    )
    payload = chain.preview.payload
    assert payload.gate_status == gate_status
    assert payload.rendered_content is None
    assert payload.rendered_content_hash is None
    assert payload.reason_codes == [f"preview_{gate_status}"]
    assert chain.revision.status == "blocked_compliance"
    assert chain.bundle.eligible is False


@pytest.mark.parametrize(
    ("gate_status", "content", "reason_codes"),
    [
        ("passed", None, []),
        ("passed", "Documented Warehouse response.", ["unexpected_reason"]),
        ("blocked", "unsafe content", ["blocked_reason"]),
        ("indeterminate", None, []),
        ("technical_error", None, []),
    ],
)
def test_inconsistent_part2_preview_is_rejected(
    gate_status, content, reason_codes
) -> None:
    approval_run_id, ticket_id = uuid4(), uuid4()
    source = list(_source())
    source[0].ticket_id = ticket_id
    source[2] = SimpleNamespace(
        preview_id=uuid4(),
        gate_status=gate_status,
        content=content,
        reason_codes=reason_codes,
    )
    with pytest.raises(ValidationError):
        _build_chain(
            approval_run_id=approval_run_id,
            handoff=SimpleNamespace(ticket_id=ticket_id),
            department_result=SimpleNamespace(department=DepartmentId.warehouse),
            source=tuple(source),
            execution_id=uuid4(),
        )


def test_initialization_orders_all_persistence_before_public_hitl_link(
    monkeypatch,
) -> None:
    ticket_id, response_run_id = uuid4(), uuid4()
    identity = _identity()
    authorization = _authorization(identity)
    result = SimpleNamespace(
        department=DepartmentId.warehouse,
        terminal_status="passed",
    )
    handoff = SimpleNamespace(
        ticket_id=ticket_id,
        run_id=response_run_id,
        selected_departments=[DepartmentId.warehouse],
        department_results=[result],
    )
    approval_run_id_holder = {}
    calls = []
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator._load_handoff",
        lambda *args, **kwargs: (handoff, "c" * 64),
    )
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator._load_department_source",
        lambda *args, **kwargs: _source(),
    )
    session = SimpleNamespace(
        execute=lambda statement: SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: None)
        )
    )

    def start(session, **kwargs):
        calls.append("start")
        approval_run_id_holder["value"] = kwargs["start"].approval_run_id
        return SimpleNamespace(replayed=False)

    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator.start_approval_run", start
    )
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator.approval_start_request_hash",
        lambda *args: "d" * 64,
    )
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator.persist_corrected_draft",
        lambda *args, **kwargs: calls.append("draft"),
    )
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator.persist_validation_artifact",
        lambda *args, artifact, **kwargs: calls.append(artifact.artifact_type),
    )
    monkeypatch.setattr(
        "services.rfp_approval.run_coordinator.persist_validation_bundle",
        lambda *args, **kwargs: calls.append("bundle"),
    )
    initialized = initialize_approval_run_from_handoff(
        session,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        identity=identity,
        authorization=authorization,
        idempotency_key=uuid4(),
        requested_at=NOW,
    )
    assert calls == ["start", "draft", "evaluation", "safe_preview", "bundle"]
    assert initialized.approval_run_id == approval_run_id_holder["value"]
    assert initialized.revisions[0].ready_for_hitl is False
    assert initialized.replayed is False
