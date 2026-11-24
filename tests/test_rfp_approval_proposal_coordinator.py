from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    EvidenceReference,
    GlobalCommercialTermsDraft,
    GlobalComplianceEvaluation,
    GlobalCurrency,
    GlobalSLA,
    ProposalEvaluationResult,
    RequestedAspect,
    RFPResponseStart,
    VolumeDiscountRow,
)
from data.pipelines.rfp_response.hashing import canonical_hash
from data.pipelines.rfp_response.evaluators import evaluate_global
from data.pipelines.rfp_response.generators import generate_global_terms
from pipelines.rfp_approval.contracts import (
    BusinessInputProvenance,
    MonthlyStorageVolumeInput,
    QuantityScope,
)
from pipelines.rfp_approval.fan_in import (
    ApprovedDepartmentRevision,
    FanInReadiness,
)
from services.rfp_approval.authorization import EffectiveAuthorization
from services.rfp_approval.commercial_store import ProposalRevisionRecord
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.proposal_coordinator import (
    ProposalMaterializationResult,
    _assert_no_authorized_volume_contradiction,
    _assert_part2_global_source_eligibility,
    _effective_global_source_from_authorized_volume,
    materialize_delegated_initial_proposal,
    materialize_initial_proposal,
)
from services.rfp_approval.store import ApprovalStoreError


class _Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _Session:
    def __init__(self, values):
        self.values = iter(values)

    def execute(self, statement):
        return _Result(next(self.values))


def _identity(now, *, responsible_code=None):
    return ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc:coordinator",
        subject="qa-coordinator",
        trackflow_user_id="qa-coordinator",
        general_role="manager",
        principal_id=uuid4(),
        binding_id=uuid4(),
        responsible_code=responsible_code,
        synthetic=True,
    )


def test_delegated_materializer_uses_only_persisted_manager_reference(
    monkeypatch,
) -> None:
    run_id, ticket_id = uuid4(), uuid4()
    principal_id, binding_id, authorization_id = uuid4(), uuid4(), uuid4()
    row = {
        "started_by_principal_id": principal_id,
        "started_by_binding_id": binding_id,
        "start_authorization_id": authorization_id,
        "authorization_version": 7,
        "started_by_general_role": "manager",
        "automate_proposal_materialization": True,
        "environment": "qa",
        "trackflow_user_id": "qa-run-manager",
        "responsible_code": None,
        "synthetic": True,
        "auth_source": "local",
        "issuer": "urn:trackflow:qa:local",
        "subject": "qa-run-manager-subject",
    }
    captured = {}

    def materialize(session, **kwargs):
        captured.update(kwargs)
        return ProposalMaterializationResult(uuid4(), run_id, True, False)

    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator._materialize_initial_proposal",
        materialize,
    )
    result = materialize_delegated_initial_proposal(
        _Session([row]), approval_run_id=run_id, ticket_id=ticket_id
    )

    assert result.approval_run_id == run_id
    assert captured["identity"].principal_id == principal_id
    assert captured["identity"].binding_id == binding_id
    assert captured["identity"].general_role == "manager"
    assert captured["authorization_reference"].authorization_id == authorization_id
    assert captured["authorization_reference"].authorization_version == 7


def test_delegated_materializer_rejects_run_without_opt_in() -> None:
    row = {
        "automate_proposal_materialization": False,
        "started_by_general_role": None,
    }
    with pytest.raises(ApprovalStoreError) as raised:
        materialize_delegated_initial_proposal(
            _Session([row]), approval_run_id=uuid4(), ticket_id=uuid4()
        )
    assert raised.value.code == "proposal_automation_not_delegated"


def test_corrected_department_and_declared_human_review_can_replace_rollup_but_not_global_safety() -> None:
    draft = SimpleNamespace(pending_commercial_items=[])
    preview = {"gate_status": "passed", "safe_content": "safe"}
    corrected_rollup = SimpleNamespace(
        overall_pass=False,
        outcome="needs_human_review",
        blocking_rule_ids=[],
        global_compliance=SimpleNamespace(passed=True),
    )

    _assert_part2_global_source_eligibility(
        draft,
        corrected_rollup,
        preview,
        require_department_rollup_pass=False,
    )
    _assert_part2_global_source_eligibility(
        draft,
        corrected_rollup,
        preview,
        require_department_rollup_pass=True,
    )

    unsafe_global = SimpleNamespace(
        overall_pass=False,
        outcome="needs_human_review",
        global_compliance=SimpleNamespace(passed=False),
    )
    with pytest.raises(ApprovalStoreError) as unsafe:
        _assert_part2_global_source_eligibility(
            draft,
            unsafe_global,
            preview,
            require_department_rollup_pass=False,
        )
    assert unsafe.value.code == "part2_global_source_not_eligible_for_import"


def test_authorized_monthly_volume_rebuilds_global_safety_without_rewriting_part2(
    monkeypatch,
) -> None:
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    ticket_id, run_id, approval_run_id = uuid4(), uuid4(), uuid4()
    request = RFPResponseStart(
        ticket_id=ticket_id,
        run_id=run_id,
        source_handoff_hash="a" * 64,
        summary_for_sales="Prepare approved warehouse services.",
        key_aspects=[
            RequestedAspect(
                aspect_id="warehouse:01:1-2",
                department=DepartmentId.warehouse,
                fact="Warehouse service is requested.",
                requested_action="Provide warehouse service.",
                line_start=1,
                line_end=2,
            )
        ],
        selected_departments=[DepartmentId.warehouse],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=None,
        customer_managed_carrier=True,
    )
    source = generate_global_terms(
        request,
        department_draft_ids=[uuid4()],
        iteration_count=1,
        generated_at=now,
    )
    compliance, _feedback = evaluate_global(
        request,
        source,
        section_currencies=frozenset({"USD"}),
        consistency_issues=[],
        evaluated_at=now,
    )
    blocking = [item for item in compliance.findings if item.blocking]
    evaluation = ProposalEvaluationResult(
        evaluation_id=uuid4(),
        ticket_id=ticket_id,
        run_id=run_id,
        section_evaluation_ids=[uuid4()],
        global_draft_id=source.draft_id,
        global_iteration_count=1,
        global_compliance=compliance,
        all_departments_passed=True,
        blocks_commercial_completeness=True,
        requires_human_input=True,
        blocking_rule_ids=[item.rule_id for item in blocking],
        regenerable=False,
        overall_pass=False,
        outcome="needs_human_review",
        terminal_status="needs_human_input",
        evaluated_at=now,
    )
    volume = MonthlyStorageVolumeInput(
        value=Decimal("20.1250"),
        unit="m3",
        period="month",
        operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
        effective_from=date(2026, 11, 1),
    )
    provenance = BusinessInputProvenance(
        evidence_type="responsible_attestation",
        evidence_id=uuid4(),
        source_name="Authorized QA storage forecast",
        issued_at=now,
        source_payload_hash="b" * 64,
    )
    input_id = uuid4()
    row = {
        "input_id": input_id,
        "payload": volume.model_dump(mode="json"),
        "payload_hash": "c" * 64,
        "provenance": provenance.model_dump(mode="json"),
        "provenance_hash": canonical_hash(provenance),
        "evidence_hash": provenance.source_payload_hash,
    }
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.load_response_start",
        lambda session, *, run_id: request,
    )
    corrected, corrected_evaluation, preview = (
        _effective_global_source_from_authorized_volume(
            _Session([date(2026, 11, 23), [row]]),
            run={
                "approval_run_id": approval_run_id,
                "ticket_id": ticket_id,
                "response_run_id": run_id,
            },
            source=source,
            evaluation=evaluation,
            preview_row={
                "gate_status": "blocked",
                "safe_content": None,
                "reason_codes": ["commercial_terms_pending"],
            },
        )
    )
    assert corrected.draft_id == source.draft_id
    assert corrected.pending_commercial_items == []
    assert corrected_evaluation.global_compliance.passed is True
    assert corrected_evaluation.overall_pass is True
    assert preview["gate_status"] == "passed"
    assert preview["authorized_input_ids"] == [input_id]


def test_documented_pdf_volume_rejects_a_later_contradictory_authorized_fact(
    monkeypatch,
) -> None:
    today = date(2026, 11, 23)
    request = SimpleNamespace(average_monthly_volume_m3=Decimal("20.0000"))
    volume = MonthlyStorageVolumeInput(
        value=Decimal("21.0000"),
        unit="m3",
        period="month",
        operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
        effective_from=today,
    )
    provenance = BusinessInputProvenance(
        evidence_type="responsible_attestation",
        evidence_id=uuid4(),
        source_name="Authorized QA storage forecast",
        issued_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
        source_payload_hash="d" * 64,
    )
    row = {
        "payload": volume.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        "provenance_hash": canonical_hash(provenance),
        "evidence_hash": provenance.source_payload_hash,
    }
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.load_response_start",
        lambda session, *, run_id: request,
    )
    with pytest.raises(ApprovalStoreError) as raised:
        _assert_no_authorized_volume_contradiction(
            _Session([[row], today]),
            run={
                "approval_run_id": uuid4(),
                "ticket_id": uuid4(),
                "response_run_id": uuid4(),
            },
        )
    assert raised.value.code == "monthly_storage_volume_source_contradiction"


@pytest.mark.parametrize("authorized_volume", [False, True])
def test_materializer_revalidates_global_sources_after_corrected_approval(
    monkeypatch, authorized_volume
) -> None:
    now = datetime.now(timezone.utc)
    approval_run_id, ticket_id, response_run_id = uuid4(), uuid4(), uuid4()
    revision_id, decision_id = uuid4(), uuid4()
    source_draft_id, source_evaluation_id, source_preview_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    evidence = [
        EvidenceReference(
            document_id="rfp-source",
            section_id="commercial",
            evidence_hash="a" * 64,
        )
    ]
    currency = GlobalCurrency(value="USD", status="documented", evidence=evidence)
    sla = GlobalSLA(
        applicability="not_applicable",
        percentage=None,
        measurement_period=None,
        scope=[],
        exclusions=[],
        evidence=[],
    )
    discounts = [
        VolumeDiscountRow(
            volume_from=1000,
            volume_to=None,
            discount_percentage=5,
            condition="Documented monthly volume",
            status="documented",
            evidence=evidence,
        )
    ]
    source = GlobalCommercialTermsDraft(
        draft_id=source_draft_id,
        ticket_id=ticket_id,
        run_id=response_run_id,
        global_iteration_count=1,
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_section_ids=[uuid4()],
        pending_commercial_items=[],
        generated_at=now - timedelta(minutes=2),
    )
    evaluation = ProposalEvaluationResult(
        evaluation_id=source_evaluation_id,
        ticket_id=ticket_id,
        run_id=response_run_id,
        section_evaluation_ids=[uuid4()],
        global_draft_id=source_draft_id,
        global_iteration_count=1,
        global_compliance=GlobalComplianceEvaluation(**{"pass": True, "findings": []}),
        all_departments_passed=False,
        blocks_commercial_completeness=False,
        requires_human_input=False,
        blocking_rule_ids=[],
        regenerable=False,
        overall_pass=False,
        outcome="needs_human_review",
        terminal_status=None,
        evaluated_at=now - timedelta(minutes=1),
    )
    source_draft_hash = canonical_hash(source)
    source_evaluation_hash = canonical_hash(evaluation)
    run = {
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "response_run_id": response_run_id,
        "response_handoff_hash": "b" * 64,
        "environment": "qa",
        "status": "active",
    }
    department_source = SimpleNamespace(
        department=DepartmentId.warehouse,
        final_draft_id=uuid4(),
        evaluation_result_id=uuid4(),
        iteration_count=1,
    )
    department_row = {
        "department_id": "warehouse",
        "content_hash": "c" * 64,
        "review_revision_no": 2,
        "content_payload": {"creation_reason": "authorized_input"},
        "source_part2_draft_id": department_source.final_draft_id,
        "source_part2_evaluation_id": department_source.evaluation_result_id,
        "source_part2_iteration": 1,
    }
    authorized_input_id = uuid4()
    authorized_input_hash = "d" * 64
    identity = _identity(
        now,
        responsible_code="miguel_torres" if authorized_volume else None,
    )
    manager_authorization = EffectiveAuthorization(
        authorization_id=uuid4(),
        authorization_version=1,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="manage_approval_run",
        department_scope=None,
        trigger_scope=None,
        target_policy="run",
        evaluated_at=now,
    )
    input_authorization = EffectiveAuthorization(
        authorization_id=uuid4(),
        authorization_version=1,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="provide_business_input",
        department_scope=None,
        trigger_scope=None,
        target_policy="proposal_wide",
        evaluated_at=now,
    )
    session_values = [run, None, [department_row]]
    if authorized_volume:
        session_values.append(
            [
                {
                    "input_id": authorized_input_id,
                    "actor_principal_id": identity.principal_id,
                    "actor_binding_id": identity.binding_id,
                    "authorization_id": input_authorization.authorization_id,
                    "authorization_version": input_authorization.authorization_version,
                    "payload_hash": authorized_input_hash,
                }
            ]
        )
    session_values.append([now])
    session = _Session(session_values)
    readiness = FanInReadiness(
        ready_for_synthesis=False,
        blockers=("current_proposal_count",),
        approved_revisions=(
            ApprovedDepartmentRevision(
                department=DepartmentId.warehouse,
                revision_id=revision_id,
                decision_id=decision_id,
            ),
        ),
        proposal_revision_id=None,
    )
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.determine_fan_in_readiness",
        lambda *args, **kwargs: readiness,
    )
    loaded = {}

    def load_sources(*args, **kwargs):
        loaded.update(kwargs)
        return (
            SimpleNamespace(
                run_id=response_run_id,
                department_results=[department_source],
            ),
            source,
            evaluation,
            {"payload_hash": source_draft_hash},
            {"payload_hash": source_evaluation_hash},
            {
                "preview_id": source_preview_id,
                "draft_id": source_draft_id,
                "gate_status": "passed",
                "safe_content": "{\"currency\":\"USD\"}",
                "reason_codes": [],
                "created_at": now - timedelta(seconds=30),
                "authorized_input_ids": (
                    [authorized_input_id] if authorized_volume else []
                ),
            },
        )

    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator._load_part2_sources",
        load_sources,
    )
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.resolve_effective_authorization",
        lambda session, *, identity, target, reference, lock_rows: (
            input_authorization
            if target.permission == "provide_business_input"
            else manager_authorization
        ),
    )
    persisted = {}
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.persist_corrected_draft",
        lambda session, *, draft, producer: persisted.setdefault("draft", draft),
    )
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.persist_validation_artifact",
        lambda session, *, artifact, producer: persisted.setdefault(
            artifact.artifact_type, artifact
        ),
    )
    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.persist_validation_bundle",
        lambda session, *, bundle, producer: persisted.setdefault("bundle", bundle),
    )

    def create(session, *, command, identity, authorization_reference, execution_id):
        persisted["command"] = command
        persisted["authorization_reference"] = authorization_reference
        return ProposalRevisionRecord(
            command.proposal.revision_id,
            command.proposal.approval_run_id,
            1,
            True,
            0,
            False,
        )

    monkeypatch.setattr(
        "services.rfp_approval.proposal_coordinator.create_initial_proposal_revision",
        create,
    )
    result = materialize_initial_proposal(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        identity=identity,
        authorization=manager_authorization,
    )
    proposal = persisted["command"].proposal
    assert result.eligible_for_synthesis is True
    assert loaded["require_department_rollup_pass"] is False
    assert persisted["bundle"].source_departments_approved is True
    assert proposal.source_department_revision_ids == [revision_id]
    expected_input_ids = [authorized_input_id] if authorized_volume else []
    assert proposal.authorized_input_ids == expected_input_ids
    assert proposal.currency == currency
    assert proposal.sla == sla
    assert proposal.volume_discounts == discounts
    assert persisted["command"].creation_mode == (
        "authorized_commercial_change" if authorized_volume else "part2_import"
    )
    assert (
        persisted["global_evaluation"].provenance.source_kind
        == "deterministic_evaluator"
    )
    assert persisted["safe_preview"].provenance.source_kind == (
        "compliance_evaluator" if authorized_volume else "part2_import"
    )
    assert persisted["bundle"].provenance.source_kind == "deterministic_evaluator"
    draft = persisted["draft"]
    global_evaluation = persisted["global_evaluation"]
    safe_preview = persisted["safe_preview"]
    assert draft.provenance.source_kind == (
        "human_correction" if authorized_volume else "part2_import"
    )
    draft_pairs = list(
        zip(
            draft.provenance.source_artifact_ids,
            draft.provenance.source_hashes,
        )
    )
    assert draft_pairs == [
        (source_draft_id, source_draft_hash),
        *(
            [(authorized_input_id, authorized_input_hash)]
            if authorized_volume
            else []
        ),
    ]
    if authorized_volume:
        assert persisted["command"].imported_by is None
        assert persisted["command"].created_by is not None
        assert (
            persisted["command"].created_by.authorization_id
            == input_authorization.authorization_id
        )
        assert (
            persisted["authorization_reference"].authorization_id
            == input_authorization.authorization_id
        )
    else:
        assert persisted["command"].created_by is None
        assert persisted["command"].imported_by is not None
        assert (
            persisted["authorization_reference"].authorization_id
            == manager_authorization.authorization_id
        )
    assert global_evaluation.corrected_draft_id == draft.draft_artifact_id
    assert global_evaluation.evaluated_content_hash == draft.content_hash
    assert safe_preview.corrected_draft_id == draft.draft_artifact_id
    assert safe_preview.evaluated_content_hash == draft.content_hash

    evaluation_pairs = list(
        zip(
            global_evaluation.provenance.source_artifact_ids,
            global_evaluation.provenance.source_hashes,
        )
    )
    assert evaluation_pairs == [
        (draft.draft_artifact_id, draft.content_hash),
        (source_draft_id, source_draft_hash),
        (source_evaluation_id, source_evaluation_hash),
        (revision_id, department_row["content_hash"]),
    ]
    preview_pairs = list(
        zip(
            safe_preview.provenance.source_artifact_ids,
            safe_preview.provenance.source_hashes,
        )
    )
    assert preview_pairs == [
        (draft.draft_artifact_id, draft.content_hash),
        (source_draft_id, source_draft_hash),
        (
            source_preview_id,
            canonical_hash(
                {
                    "preview_id": source_preview_id,
                    "draft_id": source_draft_id,
                    "gate_status": "passed",
                    "safe_content": "{\"currency\":\"USD\"}",
                    "reason_codes": [],
                }
            ),
        ),
    ]
    assert len(dict(evaluation_pairs)) == len(evaluation_pairs)
    assert len(dict(preview_pairs)) == len(preview_pairs)
    for artifact in (global_evaluation, safe_preview):
        provenance = artifact.provenance
        assert provenance.provenance_hash == canonical_hash(
            {
                "producer_node": provenance.producer_node,
                "execution_id": provenance.execution_id,
                "source_kind": provenance.source_kind,
                "source_artifact_ids": provenance.source_artifact_ids,
                "source_hashes": provenance.source_hashes,
                "produced_at": provenance.produced_at,
            }
        )

    bundle_pairs = list(
        zip(
            persisted["bundle"].provenance.source_artifact_ids,
            persisted["bundle"].provenance.source_hashes,
        )
    )
    assert bundle_pairs == [
        (draft.draft_artifact_id, draft.content_hash),
        (global_evaluation.artifact_id, global_evaluation.payload_hash),
        (safe_preview.artifact_id, safe_preview.payload_hash),
    ]
