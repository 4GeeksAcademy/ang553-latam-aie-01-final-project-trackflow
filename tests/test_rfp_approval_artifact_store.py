"""Offline unit tests for the Part 3 artifact store.

Scripted sessions validate decisions and generated SQL only.  They are not
evidence of PostgreSQL locking, FK enforcement, or concurrent execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    EvidenceReference,
    GlobalCurrency,
    GlobalSLA,
)
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    DepartmentCorrectedDraftPayload,
    ImmutableArtifactReference,
    ProposalCorrectedDraftPayload,
    SafePreviewArtifactPayload,
    department_revision_content_hash,
    proposal_revision_content_hash,
)
from services.rfp_approval.artifact_store import (
    ArtifactProducerContext,
    _audit,
    _lock_department_approval_state,
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.store import ApprovalStoreError


NOW = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
RUN_ID = UUID("30000000-0000-0000-0000-000000000001")
TICKET_ID = UUID("30000000-0000-0000-0000-000000000002")
DRAFT_ID = UUID("30000000-0000-0000-0000-000000000003")
EVALUATION_ID = UUID("30000000-0000-0000-0000-000000000004")
PREVIEW_ID = UUID("30000000-0000-0000-0000-000000000005")
BUNDLE_ID = UUID("30000000-0000-0000-0000-000000000006")
EXECUTION_ID = UUID("30000000-0000-0000-0000-000000000007")
WAREHOUSE_REVISION_ID = UUID("30000000-0000-0000-0000-000000000010")
LASTMILE_REVISION_ID = UUID("30000000-0000-0000-0000-000000000011")
REVERSE_REVISION_ID = UUID("30000000-0000-0000-0000-000000000012")
SUPERSEDED_WAREHOUSE_REVISION_ID = UUID("30000000-0000-0000-0000-000000000013")
H1 = "a" * 64
H2 = "b" * 64
CONFLICT_ID = UUID("30000000-0000-0000-0000-000000000020")


class _Result:
    def __init__(self, value=None, *, rowcount=1):
        self.value = value
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def all(self):
        return self.value


class _Session:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError(f"unexpected statement: {statement}")
        return self.results.pop(0)


def _error_code(error: pytest.ExceptionInfo[ApprovalStoreError]) -> str:
    return error.value.code


def _producer(
    environment="production",
    node="part3_evidence_producer",
    execution_id=EXECUTION_ID,
):
    return ArtifactProducerContext._from_verified_orchestrator(
        environment=environment,
        producer_node=node,
        execution_id=execution_id,
    )


def _provenance(kind, pairs=(), node="part3_evidence_producer"):
    data = {
        "producer_node": node,
        "execution_id": EXECUTION_ID,
        "source_kind": kind,
        "source_artifact_ids": [item[0] for item in pairs],
        "source_hashes": [item[1] for item in pairs],
        "produced_at": NOW,
    }
    return ApprovalArtifactProvenance(
        **data,
        provenance_hash=canonical_hash(data),
    )


def _draft():
    payload = DepartmentCorrectedDraftPayload(
        department=DepartmentId.warehouse,
        content="Corrected warehouse section",
        commercial_terms=[],
        pending_commercial_items=[],
        evidence=[],
    )
    content_hash = department_revision_content_hash(
        content=payload.content,
        commercial_terms=payload.commercial_terms,
        pending_commercial_items=payload.pending_commercial_items,
        evidence=payload.evidence,
    )
    return CorrectedDraftArtifact(
        draft_artifact_id=DRAFT_ID,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="warehouse",
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=_provenance("human_correction", [(UUID(int=40), H1)]),
        created_at=NOW,
    )


def _proposal_draft(source_revision_ids=None):
    source_revision_ids = source_revision_ids or [
        WAREHOUSE_REVISION_ID,
        LASTMILE_REVISION_ID,
        REVERSE_REVISION_ID,
    ]
    evidence = EvidenceReference(
        document_id="synthetic-authorized-global-terms",
        section_id="global-terms",
        evidence_hash=H1,
    )
    payload = ProposalCorrectedDraftPayload(
        currency=GlobalCurrency(
            value="USD", status="documented", evidence=[evidence]
        ),
        sla=GlobalSLA(
            applicability="applicable",
            percentage=99.0,
            measurement_period="monthly",
            scope=["warehouse", "lastmile", "reverse"],
            exclusions=[],
            evidence=[evidence],
        ),
        volume_discounts=[],
        source_department_revision_ids=source_revision_ids,
        authorized_input_ids=[],
    )
    content_hash = proposal_revision_content_hash(
        currency=payload.currency,
        sla=payload.sla,
        volume_discounts=payload.volume_discounts,
        source_department_revision_ids=payload.source_department_revision_ids,
        authorized_input_ids=payload.authorized_input_ids,
    )
    return CorrectedDraftArtifact(
        draft_artifact_id=DRAFT_ID,
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="proposal",
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=_provenance("human_correction", [(UUID(int=40), H1)]),
        created_at=NOW,
    )


def _global_artifacts(source_revision_ids=None):
    draft = _proposal_draft(source_revision_ids)
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        eligible=True,
    )
    evaluation = ApprovalValidationArtifact(
        artifact_id=EVALUATION_ID,
        artifact_type="global_evaluation",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="proposal",
        corrected_draft_id=DRAFT_ID,
        evaluated_content_hash=draft.content_hash,
        payload=evaluation_payload,
        payload_hash=canonical_hash(evaluation_payload),
        provenance=_provenance(
            "deterministic_evaluator", [(DRAFT_ID, draft.content_hash)]
        ),
        created_at=NOW,
    )
    rendered = "Safe proposal preview"
    preview_payload = SafePreviewArtifactPayload(
        gate_status="passed",
        rendered_content=rendered,
        rendered_content_hash=canonical_hash({"rendered_content": rendered}),
        reason_codes=[],
    )
    preview = ApprovalValidationArtifact(
        artifact_id=PREVIEW_ID,
        artifact_type="safe_preview",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="proposal",
        corrected_draft_id=DRAFT_ID,
        evaluated_content_hash=draft.content_hash,
        payload=preview_payload,
        payload_hash=canonical_hash(preview_payload),
        provenance=_provenance(
            "compliance_evaluator", [(DRAFT_ID, draft.content_hash)]
        ),
        created_at=NOW,
    )
    evaluation_reference = ImmutableArtifactReference(
        artifact_type="global_evaluation",
        artifact_id=EVALUATION_ID,
        contract_version=evaluation.contract_version,
        payload_hash=evaluation.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    preview_reference = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=PREVIEW_ID,
        contract_version=preview.contract_version,
        payload_hash=preview.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    validation_hash = canonical_hash(
        {
            "revision_content_hash": draft.content_hash,
            "global_evaluation": evaluation_reference,
            "safe_preview": preview_reference,
            "mandatory_compliance_passed": True,
            "unresolved_input_codes": [],
            "blocking_rule_ids": [],
            "open_conflict_ids": [],
            "source_departments_approved": True,
            "eligible_for_synthesis": True,
            "validated_at": NOW,
        }
    )
    provenance = _provenance(
        "compliance_evaluator",
        [
            (DRAFT_ID, draft.content_hash),
            (EVALUATION_ID, evaluation.payload_hash),
            (PREVIEW_ID, preview.payload_hash),
        ],
    )
    data = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "proposal",
        "corrected_draft_id": DRAFT_ID,
        "content_hash": draft.content_hash,
        "evaluation_artifact_id": EVALUATION_ID,
        "evaluation_artifact_type": "global_evaluation",
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": PREVIEW_ID,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": True,
        "eligible": True,
        "provenance": provenance,
        "validated_at": NOW,
    }
    bundle = ApprovalValidationBundle(
        validation_bundle_id=BUNDLE_ID,
        **data,
        bundle_hash=canonical_hash(data),
    )
    return draft, evaluation, preview, bundle


def _evaluation(draft=None, payload=None):
    draft = draft or _draft()
    payload = payload or ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        eligible=True,
    )
    return ApprovalValidationArtifact(
        artifact_id=EVALUATION_ID,
        artifact_type="evaluation",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="warehouse",
        corrected_draft_id=draft.draft_artifact_id,
        evaluated_content_hash=draft.content_hash,
        payload=payload,
        payload_hash=canonical_hash(payload),
        provenance=_provenance(
            "deterministic_evaluator", [(draft.draft_artifact_id, draft.content_hash)]
        ),
        created_at=NOW,
    )


def _preview(draft=None, gate_status="passed"):
    draft = draft or _draft()
    if gate_status == "passed":
        rendered = "Safe warehouse preview"
        payload = SafePreviewArtifactPayload(
            gate_status="passed",
            rendered_content=rendered,
            rendered_content_hash=canonical_hash({"rendered_content": rendered}),
            reason_codes=[],
        )
    else:
        payload = SafePreviewArtifactPayload(
            gate_status=gate_status,
            rendered_content=None,
            rendered_content_hash=None,
            reason_codes=["COMPLIANCE_BLOCKED"],
        )
    return ApprovalValidationArtifact(
        artifact_id=PREVIEW_ID,
        artifact_type="safe_preview",
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="warehouse",
        corrected_draft_id=draft.draft_artifact_id,
        evaluated_content_hash=draft.content_hash,
        payload=payload,
        payload_hash=canonical_hash(payload),
        provenance=_provenance(
            "compliance_evaluator", [(draft.draft_artifact_id, draft.content_hash)]
        ),
        created_at=NOW,
    )


def _bundle(evaluation=None, preview=None, **changes):
    draft = _draft()
    evaluation = evaluation or _evaluation(draft)
    preview = preview or _preview(draft)
    evaluation_reference = ImmutableArtifactReference(
        artifact_type=evaluation.artifact_type,
        artifact_id=evaluation.artifact_id,
        contract_version=evaluation.contract_version,
        payload_hash=evaluation.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    preview_reference = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=preview.artifact_id,
        contract_version=preview.contract_version,
        payload_hash=preview.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    validation_hash = canonical_hash(
        {
            "revision_content_hash": draft.content_hash,
            "evaluation": evaluation_reference,
            "safe_preview": preview_reference,
            "validated_at": NOW,
        }
    )
    provenance = _provenance(
        "compliance_evaluator",
        [
            (draft.draft_artifact_id, draft.content_hash),
            (evaluation.artifact_id, evaluation.payload_hash),
            (preview.artifact_id, preview.payload_hash),
        ],
    )
    data = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "warehouse",
        "corrected_draft_id": draft.draft_artifact_id,
        "content_hash": draft.content_hash,
        "evaluation_artifact_id": evaluation.artifact_id,
        "evaluation_artifact_type": "evaluation",
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": preview.artifact_id,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": True,
        "provenance": provenance,
        "validated_at": NOW,
    }
    data.update(changes)
    return ApprovalValidationBundle(
        validation_bundle_id=BUNDLE_ID,
        **data,
        bundle_hash=canonical_hash(data),
    )


def _bundle_with_persisted_json(
    evaluation_payload,
    preview_payload=None,
    **summary_changes,
):
    """Build a valid bundle whose referenced rows contain raw JSONB values."""

    draft = _draft()
    evaluation = _evaluation(draft)
    preview = _preview(draft)
    evaluation_payload = canonical_value(evaluation_payload)
    preview_payload = canonical_value(preview_payload or preview.payload)
    evaluation_hash = canonical_hash(evaluation_payload)
    preview_hash = canonical_hash(preview_payload)
    evaluation_reference = ImmutableArtifactReference(
        artifact_type="evaluation",
        artifact_id=EVALUATION_ID,
        contract_version=evaluation.contract_version,
        payload_hash=evaluation_hash,
        evaluated_content_hash=draft.content_hash,
    )
    preview_reference = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=PREVIEW_ID,
        contract_version=preview.contract_version,
        payload_hash=preview_hash,
        evaluated_content_hash=draft.content_hash,
    )
    validation_hash = canonical_hash(
        {
            "revision_content_hash": draft.content_hash,
            "evaluation": evaluation_reference,
            "safe_preview": preview_reference,
            "validated_at": NOW,
        }
    )
    provenance = _provenance(
        "compliance_evaluator",
        [
            (DRAFT_ID, draft.content_hash),
            (EVALUATION_ID, evaluation_hash),
            (PREVIEW_ID, preview_hash),
        ],
    )
    data = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "scope_key": "warehouse",
        "corrected_draft_id": DRAFT_ID,
        "content_hash": draft.content_hash,
        "evaluation_artifact_id": EVALUATION_ID,
        "evaluation_artifact_type": "evaluation",
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation_hash,
        "safe_preview_artifact_id": PREVIEW_ID,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview_hash,
        "revision_validation_hash": validation_hash,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
        "open_conflict_ids": [],
        "source_departments_approved": False,
        "eligible": True,
        "provenance": provenance,
        "validated_at": NOW,
    }
    data.update(summary_changes)
    bundle = ApprovalValidationBundle(
        validation_bundle_id=BUNDLE_ID,
        **data,
        bundle_hash=canonical_hash(data),
    )
    evaluation_row = _artifact_row(evaluation)
    evaluation_row.update(
        payload=evaluation_payload,
        payload_hash=evaluation_hash,
    )
    preview_row = _artifact_row(preview)
    preview_row.update(payload=preview_payload, payload_hash=preview_hash)
    return bundle, evaluation_row, preview_row


def _run_row(**changes):
    row = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "environment": "production",
        "status": "active",
        "active_departments": ["warehouse"],
    }
    row.update(changes)
    return row


def _draft_row(draft=None):
    draft = draft or _draft()
    return {
        "draft_artifact_id": draft.draft_artifact_id,
        "approval_run_id": draft.approval_run_id,
        "ticket_id": draft.ticket_id,
        "scope_key": draft.scope_key,
        "content_payload": canonical_value(draft.payload),
        "content_hash": draft.content_hash,
        "payload_hash": draft.payload_hash,
    }


def _department_row(
    revision_id,
    department,
    *,
    status="approved",
    approval_run_id=RUN_ID,
    ticket_id=TICKET_ID,
    is_current=True,
):
    return {
        "revision_id": revision_id,
        "approval_run_id": approval_run_id,
        "ticket_id": ticket_id,
        "department_id": department,
        "status": status,
        "is_current": is_current,
    }


def _current_department_rows():
    return [
        _department_row(WAREHOUSE_REVISION_ID, "warehouse"),
        _department_row(LASTMILE_REVISION_ID, "lastmile"),
        _department_row(REVERSE_REVISION_ID, "reverse"),
    ]


def _audit_existing_row(**changes):
    row = {
        "event_key": f"artifact:draft:{DRAFT_ID}",
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "thread_id": f"artifact:draft:{DRAFT_ID}",
        "execution_id": EXECUTION_ID,
        "sequence_no": 1,
        "node_name": "persist_corrected_draft",
        "agent_name": "rfp_approval_artifact_store",
        "event_type": "corrected_draft_persisted",
        "safe_input_ref": {
            "draft_artifact_id": str(DRAFT_ID),
            "content_hash": H1,
        },
        "safe_output_ref": {"draft_artifact_id": str(DRAFT_ID)},
        "result": "persisted",
        "from_state": None,
        "to_state": None,
        "department_revision_id": None,
        "proposal_revision_id": None,
        "decision_id": None,
        "conflict_id": None,
        "actor_principal_id": None,
        "actor_binding_id": None,
        "authorization_id": None,
        "authorization_version": None,
        "environment": None,
    }
    row.update(changes)
    return row


def _write_audit(
    session,
    *,
    producer=None,
    event_type="corrected_draft_persisted",
    safe_input=None,
):
    _audit(
        session,
        event_key=f"artifact:draft:{DRAFT_ID}",
        run_id=RUN_ID,
        ticket_id=TICKET_ID,
        thread_id=f"artifact:draft:{DRAFT_ID}",
        producer=producer or _producer(),
        node_name="persist_corrected_draft",
        event_type=event_type,
        safe_input=safe_input
        or {"draft_artifact_id": DRAFT_ID, "content_hash": H1},
        safe_output={"draft_artifact_id": DRAFT_ID},
    )


def _artifact_row(artifact):
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "approval_run_id": artifact.approval_run_id,
        "ticket_id": artifact.ticket_id,
        "scope_key": artifact.scope_key,
        "corrected_draft_id": artifact.corrected_draft_id,
        "evaluated_content_hash": artifact.evaluated_content_hash,
        "contract_version": artifact.contract_version,
        "payload": canonical_value(artifact.payload),
        "payload_hash": artifact.payload_hash,
    }


def _validation_values(artifact):
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "approval_run_id": artifact.approval_run_id,
        "ticket_id": artifact.ticket_id,
        "scope_key": artifact.scope_key,
        "corrected_draft_id": artifact.corrected_draft_id,
        "evaluated_content_hash": artifact.evaluated_content_hash,
        "contract_version": artifact.contract_version,
        "payload": canonical_value(artifact.payload),
        "payload_hash": artifact.payload_hash,
        "provenance": canonical_value(artifact.provenance),
        "provenance_hash": artifact.provenance.provenance_hash,
        "producer_node": artifact.provenance.producer_node,
        "execution_id": artifact.provenance.execution_id,
        "produced_at": artifact.provenance.produced_at,
        "created_at": artifact.created_at,
    }


def _bundle_values(bundle):
    return {
        "validation_bundle_id": bundle.validation_bundle_id,
        "contract_version": bundle.contract_version,
        "approval_run_id": bundle.approval_run_id,
        "ticket_id": bundle.ticket_id,
        "scope_key": bundle.scope_key,
        "corrected_draft_id": bundle.corrected_draft_id,
        "content_hash": bundle.content_hash,
        "evaluation_artifact_id": bundle.evaluation_artifact_id,
        "evaluation_artifact_type": bundle.evaluation_artifact_type,
        "evaluation_contract_version": bundle.evaluation_contract_version,
        "evaluation_payload_hash": bundle.evaluation_payload_hash,
        "safe_preview_artifact_id": bundle.safe_preview_artifact_id,
        "safe_preview_artifact_type": "safe_preview",
        "safe_preview_contract_version": bundle.safe_preview_contract_version,
        "safe_preview_payload_hash": bundle.safe_preview_payload_hash,
        "revision_validation_hash": bundle.revision_validation_hash,
        "mandatory_compliance_passed": bundle.mandatory_compliance_passed,
        "blocks_commercial_completeness": bundle.blocks_commercial_completeness,
        "blocking_rule_ids": canonical_value(bundle.blocking_rule_ids),
        "unresolved_input_codes": canonical_value(bundle.unresolved_input_codes),
        "open_conflict_ids": canonical_value(bundle.open_conflict_ids),
        "source_departments_approved": bundle.source_departments_approved,
        "eligible": bundle.eligible,
        "provenance": canonical_value(bundle.provenance),
        "provenance_hash": bundle.provenance.provenance_hash,
        "bundle_hash": bundle.bundle_hash,
        "validated_at": bundle.validated_at,
    }


def test_producer_context_cannot_be_freely_constructed() -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        ArtifactProducerContext(
            "production", "node", EXECUTION_ID, object()
        )
    assert _error_code(raised) == "unverified_artifact_producer"


def test_strict_audit_inserts_new_event() -> None:
    session = _Session(_Result({"event_id": UUID(int=500)}))
    _write_audit(session)
    compiled = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (event_key) DO NOTHING" in compiled
    assert "RETURNING rfp_workflow_events.event_id" in compiled


def test_strict_audit_accepts_exact_replay_after_conflict_wait() -> None:
    session = _Session(_Result(None), _Result(_audit_existing_row()))
    _write_audit(session)
    compiled = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE" in compiled
    assert len(session.statements) == 2


@pytest.mark.parametrize(
    ("existing_changes", "call_changes"),
    [
        ({"event_type": "different_event"}, {}),
        ({"approval_run_id": UUID(int=502)}, {}),
    ],
)
def test_strict_audit_rejects_incompatible_event_key(
    existing_changes, call_changes
) -> None:
    session = _Session(
        _Result(None),
        _Result(_audit_existing_row(**existing_changes)),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        _write_audit(session, **call_changes)
    assert _error_code(raised) == "artifact_audit_event_collision"


def test_strict_audit_rejects_changed_execution_id() -> None:
    session = _Session(_Result(None), _Result(_audit_existing_row()))
    with pytest.raises(ApprovalStoreError) as raised:
        _write_audit(session, producer=_producer(execution_id=UUID(int=503)))
    assert _error_code(raised) == "artifact_audit_event_collision"


def test_strict_audit_rejects_changed_safe_references() -> None:
    session = _Session(_Result(None), _Result(_audit_existing_row()))
    with pytest.raises(ApprovalStoreError) as raised:
        _write_audit(
            session,
            safe_input={"draft_artifact_id": DRAFT_ID, "content_hash": H2},
        )
    assert _error_code(raised) == "artifact_audit_event_collision"


def test_corrected_draft_persists_safe_audit_references(monkeypatch) -> None:
    draft = _draft()
    inserted = {
        "draft_artifact_id": draft.draft_artifact_id,
        "approval_run_id": RUN_ID,
        "content_hash": draft.content_hash,
    }
    session = _Session(_Result(_run_row()), _Result(inserted))
    captured = {}
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    record = persist_corrected_draft(session, draft=draft, producer=_producer())
    assert record.replayed is False
    compiled = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (draft_artifact_id) DO NOTHING" in compiled
    assert set(captured["safe_input"]) == {
        "draft_artifact_id", "scope_key", "contract_version", "content_hash",
        "payload_hash", "provenance_hash",
    }
    assert "Corrected warehouse section" not in str(captured)


def test_store_revalidates_mutated_contract_before_any_sql() -> None:
    invalid = _draft().model_copy(update={"payload_hash": H2})
    session = _Session()
    with pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(session, draft=invalid, producer=_producer())
    assert _error_code(raised) == "artifact_contract_invalid"
    assert session.statements == []


def test_producer_must_match_provenance_before_any_sql() -> None:
    draft = _draft()
    session = _Session()
    with pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(
            session,
            draft=draft,
            producer=_producer(node="different_verified_node"),
        )
    assert _error_code(raised) == "artifact_producer_provenance_mismatch"
    assert session.statements == []


def test_draft_replay_is_exact_and_collision_is_rejected(monkeypatch) -> None:
    draft = _draft()
    audit_calls = []
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit",
        lambda *args, **kwargs: audit_calls.append(kwargs),
    )
    expected = {
        "draft_artifact_id": draft.draft_artifact_id,
        "approval_run_id": draft.approval_run_id,
        "ticket_id": draft.ticket_id,
        "scope_key": draft.scope_key,
        "contract_version": draft.contract_version,
        "content_payload": canonical_value(draft.payload),
        "content_hash": draft.content_hash,
        "payload_hash": draft.payload_hash,
        "provenance": canonical_value(draft.provenance),
        "provenance_hash": draft.provenance.provenance_hash,
        "producer_node": draft.provenance.producer_node,
        "execution_id": draft.provenance.execution_id,
        "produced_at": draft.provenance.produced_at,
        "created_at": draft.created_at,
    }
    replay = _Session(_Result(_run_row()), _Result(None), _Result(expected))
    assert persist_corrected_draft(
        replay, draft=draft, producer=_producer()
    ).replayed is True
    inserted = _Session(_Result(_run_row()), _Result(expected))
    assert not persist_corrected_draft(
        inserted, draft=draft, producer=_producer()
    ).replayed
    assert audit_calls[0] == audit_calls[1]
    assert "replayed" not in audit_calls[0]["safe_output"]
    collision = dict(expected, payload_hash=H2)
    session = _Session(_Result(_run_row()), _Result(None), _Result(collision))
    with pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(session, draft=draft, producer=_producer())
    assert _error_code(raised) == "corrected_draft_id_collision"


@pytest.mark.parametrize(
    ("run", "environment", "code"),
    [
        (None, "production", "approval_run_not_found_or_environment_mismatch"),
        (_run_row(status="completed"), "production", "approval_run_not_active"),
        (_run_row(), "qa", "approval_run_not_found_or_environment_mismatch"),
    ],
)
def test_draft_requires_active_run_and_exact_environment(run, environment, code) -> None:
    session = _Session(_Result(run))
    with pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(
            session, draft=_draft(), producer=_producer(environment=environment)
        )
    assert _error_code(raised) == code


def test_validation_artifact_requires_exact_draft(monkeypatch) -> None:
    artifact = _evaluation()
    session = _Session(_Result(_run_row()), _Result(None))
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(
            session, artifact=artifact, producer=_producer()
        )
    assert _error_code(raised) == "validation_artifact_draft_mismatch"


def test_validation_artifact_rejects_incompatible_provenance() -> None:
    artifact = _evaluation().model_copy(
        update={
            "provenance": _provenance(
                "deterministic_evaluator", [(UUID(int=99), _draft().content_hash)]
            )
        }
    )
    session = _Session(_Result(_run_row()), _Result(_draft_row()))
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(session, artifact=artifact, producer=_producer())
    assert _error_code(raised) == "validation_artifact_provenance_draft_mismatch"


def test_validation_artifact_rejects_human_correction_as_validation_source() -> None:
    artifact = _evaluation().model_copy(
        update={
            "provenance": _provenance(
                "human_correction", [(DRAFT_ID, _draft().content_hash)]
            )
        }
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(
            _Session(), artifact=artifact, producer=_producer()
        )
    assert _error_code(raised) == "validation_artifact_provenance_kind_invalid"


def test_validation_artifact_rejects_wrong_draft_hash_in_provenance() -> None:
    artifact = _evaluation()
    artifact = artifact.model_copy(
        update={
            "provenance": _provenance(
                "deterministic_evaluator",
                [(artifact.corrected_draft_id, H2)],
            )
        }
    )
    session = _Session(_Result(_run_row()), _Result(_draft_row()))
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(session, artifact=artifact, producer=_producer())
    assert _error_code(raised) == "validation_artifact_provenance_draft_mismatch"


def test_artifact_provenance_rejects_misaligned_pairs_and_tampered_hash() -> None:
    values = {
        "producer_node": "part3_evidence_producer",
        "execution_id": EXECUTION_ID,
        "source_kind": "deterministic_evaluator",
        "source_artifact_ids": [DRAFT_ID, UUID(int=99)],
        "source_hashes": [_draft().content_hash],
        "produced_at": NOW,
    }
    with pytest.raises(ValueError, match="source artifact ids and hashes must align"):
        ApprovalArtifactProvenance(
            **values,
            provenance_hash=canonical_hash(values),
        )

    valid = _provenance(
        "deterministic_evaluator", [(DRAFT_ID, _draft().content_hash)]
    )
    with pytest.raises(ValueError, match="artifact provenance hash is invalid"):
        ApprovalArtifactProvenance.model_validate(
            valid.model_dump(mode="python") | {"provenance_hash": H2}
        )


def test_validation_artifact_persists_and_uses_natural_idempotency(monkeypatch) -> None:
    artifact = _evaluation()
    session = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(_artifact_row(artifact))
    )
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    record = persist_validation_artifact(
        session, artifact=artifact, producer=_producer()
    )
    assert record.artifact_type == "evaluation" and not record.replayed
    compiled = str(session.statements[2].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (artifact_id) DO NOTHING" in compiled


def test_validation_artifact_exact_replay_and_collision(monkeypatch) -> None:
    artifact = _evaluation()
    expected = _validation_values(artifact)
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    replay = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(None), _Result(expected)
    )
    assert persist_validation_artifact(
        replay, artifact=artifact, producer=_producer()
    ).replayed
    collision = dict(expected, payload_hash=H2)
    rejected = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(None), _Result(collision)
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(
            rejected, artifact=artifact, producer=_producer()
        )
    assert _error_code(raised) == "validation_artifact_id_collision"


def test_bundle_persists_only_with_matching_semantics(monkeypatch) -> None:
    evaluation, preview = _evaluation(), _preview()
    bundle = _bundle(evaluation, preview)
    rows = [_artifact_row(evaluation), _artifact_row(preview)]
    session = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(rows), _Result({
            "validation_bundle_id": bundle.validation_bundle_id,
            "approval_run_id": RUN_ID,
            "scope_key": "warehouse",
            "content_hash": bundle.content_hash,
        })
    )
    captured = {}
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    record = persist_validation_bundle(
        session, bundle=bundle, producer=_producer()
    )
    assert record.eligible and not record.replayed
    assert "content" not in captured["safe_input"]
    assert "payload" not in captured["safe_input"]


def test_bundle_accepts_jsonb_uuid_strings_in_open_conflicts(monkeypatch) -> None:
    evaluation_payload = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[CONFLICT_ID],
        eligible=False,
    )
    bundle, evaluation_row, preview_row = _bundle_with_persisted_json(
        evaluation_payload,
        open_conflict_ids=[CONFLICT_ID],
        eligible=False,
    )
    assert evaluation_row["payload"]["open_conflict_ids"] == [str(CONFLICT_ID)]
    session = _Session(
        _Result(_run_row()),
        _Result(_draft_row()),
        _Result([evaluation_row, preview_row]),
        _Result({"validation_bundle_id": BUNDLE_ID}),
    )
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    record = persist_validation_bundle(
        session, bundle=bundle, producer=_producer()
    )
    assert record.eligible is False


def test_bundle_rejects_jsonb_payload_with_stale_hash() -> None:
    payload = canonical_value(_evaluation().payload)
    bundle, evaluation_row, preview_row = _bundle_with_persisted_json(payload)
    evaluation_row["payload"] = {
        **payload,
        "mandatory_compliance_passed": False,
        "blocking_rule_ids": ["COMPLIANCE_FAILURE"],
        "eligible": False,
    }
    session = _Session(
        _Result(_run_row()),
        _Result(_draft_row()),
        _Result([evaluation_row, preview_row]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "persisted_validation_artifact_hash_mismatch"


@pytest.mark.parametrize(
    "payload_change",
    [
        {"open_conflict_ids": ["not-a-uuid"], "eligible": False},
        {"unexpected_field": "not allowed"},
        {"blocking_rule_ids": ["COMPLIANCE_FAILURE"], "eligible": True},
    ],
    ids=["malformed-uuid", "extra-field", "incoherent-eligibility"],
)
def test_bundle_rejects_invalid_jsonb_evaluation(payload_change) -> None:
    payload = {**canonical_value(_evaluation().payload), **payload_change}
    bundle, evaluation_row, preview_row = _bundle_with_persisted_json(payload)
    session = _Session(
        _Result(_run_row()),
        _Result(_draft_row()),
        _Result([evaluation_row, preview_row]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "persisted_validation_artifact_invalid"


def test_bundle_rejects_invalid_jsonb_safe_preview() -> None:
    preview_payload = {
        "gate_status": "passed",
        "rendered_content": None,
        "rendered_content_hash": None,
        "reason_codes": [],
    }
    bundle, evaluation_row, preview_row = _bundle_with_persisted_json(
        _evaluation().payload,
        preview_payload,
    )
    session = _Session(
        _Result(_run_row()),
        _Result(_draft_row()),
        _Result([evaluation_row, preview_row]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "persisted_validation_artifact_invalid"


def test_bundle_exact_replay_and_collision(monkeypatch) -> None:
    evaluation, preview = _evaluation(), _preview()
    bundle = _bundle(evaluation, preview)
    rows = [_artifact_row(evaluation), _artifact_row(preview)]
    expected = _bundle_values(bundle)
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    replay = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(rows),
        _Result(None), _Result(expected),
    )
    assert persist_validation_bundle(
        replay, bundle=bundle, producer=_producer()
    ).replayed
    collision = dict(expected, bundle_hash=H2)
    rejected = _Session(
        _Result(_run_row()), _Result(_draft_row()), _Result(rows),
        _Result(None), _Result(collision),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(
            rejected, bundle=bundle, producer=_producer()
        )
    assert _error_code(raised) == "validation_bundle_id_collision"


def test_global_department_state_is_resolved_under_stable_row_lock() -> None:
    rows = _current_department_rows()
    rows[2]["status"] = "request_changes"
    session = _Session(_Result(rows))
    run = _run_row(active_departments=["warehouse", "lastmile", "reverse"])
    assert not _lock_department_approval_state(
        session,
        run,
        [WAREHOUSE_REVISION_ID, LASTMILE_REVISION_ID, REVERSE_REVISION_ID],
    )
    compiled = str(session.statements[0].compile(dialect=postgresql.dialect()))
    # SQLAlchemy's key_share=True without read=True deliberately renders the
    # stronger FOR NO KEY UPDATE mode used by the existing Part 3 stores.
    assert "FOR NO KEY UPDATE" in compiled
    assert "is_current" in compiled


def test_bundle_rejects_preview_from_other_draft_with_same_hash() -> None:
    evaluation, preview = _evaluation(), _preview()
    bundle = _bundle(evaluation, preview)
    preview_row = _artifact_row(preview)
    preview_row["corrected_draft_id"] = UUID(int=88)
    session = _Session(
        _Result(_run_row()),
        _Result(_draft_row()),
        _Result([_artifact_row(evaluation), preview_row]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "validation_bundle_artifact_mismatch"


def test_bundle_rejects_evaluation_summary_contradiction() -> None:
    evaluation, preview = _evaluation(), _preview()
    bundle = _bundle(evaluation, preview)
    row = _artifact_row(evaluation)
    contradictory = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=False,
        blocking_rule_ids=["COMPLIANCE_FAILURE"],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        eligible=False,
    )
    row["payload"] = canonical_value(contradictory)
    row["payload_hash"] = bundle.evaluation_payload_hash
    session = _Session(
        _Result(_run_row()), _Result(_draft_row()),
        _Result([row, _artifact_row(preview)]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) in {
        "persisted_validation_artifact_hash_mismatch",
        "validation_bundle_evaluation_summary_mismatch",
    }


@pytest.mark.parametrize("gate_status", ["blocked", "indeterminate", "technical_error"])
def test_eligible_bundle_rejects_non_passed_preview(gate_status) -> None:
    evaluation = _evaluation()
    preview = _preview(gate_status=gate_status)
    bundle = _bundle(evaluation, preview)
    session = _Session(
        _Result(_run_row()), _Result(_draft_row()),
        _Result([_artifact_row(evaluation), _artifact_row(preview)]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "eligible_bundle_preview_not_passed"


def test_contract_cannot_represent_passed_evaluation_as_ineligible_preview() -> None:
    evaluation = _evaluation()
    preview = _preview(gate_status="blocked")
    with pytest.raises(ValueError, match="bundle eligibility disagrees with blockers"):
        _bundle(evaluation, preview, eligible=False)


@pytest.mark.parametrize(
    "pending_codes",
    [
        ["RETURN_PROCESSING_RATE_REQUIRED"],
        ["MONTHLY_STORAGE_VOLUME_REQUIRED"],
        [
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "RETURN_PROCESSING_RATE_REQUIRED",
            "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
        ],
    ],
)
def test_indicative_bundle_is_eligible_for_human_review(pending_codes) -> None:
    bundle = _bundle(
        blocks_commercial_completeness=True,
        unresolved_input_codes=pending_codes,
    )
    assert bundle.eligible is True
    assert bundle.blocks_commercial_completeness is True
    assert bundle.unresolved_input_codes == pending_codes


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "blocks_commercial_completeness": True,
                "unresolved_input_codes": ["UNKNOWN_COMMERCIAL_INPUT"],
            },
            "bundle eligibility disagrees with blockers",
        ),
        (
            {"mandatory_compliance_passed": False},
            "bundle eligibility disagrees with blockers",
        ),
        (
            {"blocking_rule_ids": ["TF-CONFIDENTIAL-001"]},
            "bundle eligibility disagrees with blockers",
        ),
        (
            {
                "blocks_commercial_completeness": False,
                "unresolved_input_codes": ["RETURN_PROCESSING_RATE_REQUIRED"],
            },
            "commercial completeness flag disagrees with unresolved inputs",
        ),
        (
            {"blocks_commercial_completeness": True},
            "commercial completeness flag disagrees with unresolved inputs",
        ),
        (
            {
                "blocks_commercial_completeness": True,
                "unresolved_input_codes": [
                    "RETURN_PROCESSING_RATE_REQUIRED",
                    "RETURN_PROCESSING_RATE_REQUIRED",
                ],
            },
            "unresolved_input_codes must not contain duplicates",
        ),
    ],
)
def test_indicative_bundle_rejects_unsafe_or_incoherent_states(
    changes, message
) -> None:
    with pytest.raises(ValueError, match=message):
        _bundle(**changes)


def test_indicative_bundle_rejects_non_string_pending_payload() -> None:
    with pytest.raises(ValueError):
        _bundle(
            blocks_commercial_completeness=True,
            unresolved_input_codes=[1],
        )


def test_global_bundle_accepts_exact_current_approved_sources(monkeypatch) -> None:
    draft, evaluation, preview, bundle = _global_artifacts()
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
        _Result(_current_department_rows()),
        _Result([_artifact_row(evaluation), _artifact_row(preview)]),
        _Result({"validation_bundle_id": BUNDLE_ID}),
    )
    monkeypatch.setattr(
        "services.rfp_approval.artifact_store._audit", lambda *args, **kwargs: None
    )
    record = persist_validation_bundle(
        session, bundle=bundle, producer=_producer()
    )
    assert record.eligible and not record.replayed


def test_global_bundle_rejects_superseded_source_revision() -> None:
    source_ids = [
        SUPERSEDED_WAREHOUSE_REVISION_ID,
        LASTMILE_REVISION_ID,
        REVERSE_REVISION_ID,
    ]
    draft, _evaluation_item, _preview_item, bundle = _global_artifacts(source_ids)
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
        _Result(_current_department_rows()),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "global_draft_source_revision_mismatch"


def test_global_bundle_rejects_missing_active_department_source() -> None:
    draft, _evaluation_item, _preview_item, bundle = _global_artifacts(
        [WAREHOUSE_REVISION_ID, LASTMILE_REVISION_ID]
    )
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "global_draft_source_revision_scope_mismatch"


@pytest.mark.parametrize("foreign_key", ["approval_run_id", "ticket_id"])
def test_global_bundle_rejects_source_from_other_run_or_ticket(foreign_key) -> None:
    draft, _evaluation_item, _preview_item, bundle = _global_artifacts()
    rows = _current_department_rows()
    rows[0][foreign_key] = UUID(int=999)
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
        _Result(rows),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "global_draft_source_revision_mismatch"


def test_global_bundle_rejects_two_sources_for_same_department() -> None:
    draft, _evaluation_item, _preview_item, bundle = _global_artifacts(
        [
            WAREHOUSE_REVISION_ID,
            SUPERSEDED_WAREHOUSE_REVISION_ID,
            LASTMILE_REVISION_ID,
        ]
    )
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
        _Result(_current_department_rows()),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "global_draft_source_revision_mismatch"


def test_global_bundle_rejects_nonapproved_current_source() -> None:
    draft, evaluation, preview, bundle = _global_artifacts()
    rows = _current_department_rows()
    rows[2]["status"] = "request_changes"
    session = _Session(
        _Result(_run_row(active_departments=["warehouse", "lastmile", "reverse"])),
        _Result(_draft_row(draft)),
        _Result(rows),
        _Result([_artifact_row(evaluation), _artifact_row(preview)]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert _error_code(raised) == "validation_bundle_department_state_mismatch"


def test_bundle_provenance_must_bind_all_three_artifacts() -> None:
    bundle = _bundle()
    bad = _bundle(
        provenance=_provenance(
            "compliance_evaluator",
            [(DRAFT_ID, bundle.content_hash), (EVALUATION_ID, bundle.evaluation_payload_hash)],
        )
    )
    evaluation, preview = _evaluation(), _preview()
    session = _Session(
        _Result(_run_row()), _Result(_draft_row()),
        _Result([_artifact_row(evaluation), _artifact_row(preview)]),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bad, producer=_producer())
    assert _error_code(raised) == "validation_bundle_provenance_preview_mismatch"
