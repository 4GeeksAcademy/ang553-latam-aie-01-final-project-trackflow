"""Real isolated PostgreSQL proofs for final-document transactionality."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    EvidenceReference,
    GlobalCurrency,
    GlobalSLA,
    VolumeDiscountRow,
)
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationTarget,
    CorrectedDraftArtifact,
    ImmutableArtifactReference,
    InitialProposalRevisionCommand,
    ProposalCorrectedDraftPayload,
    ProposalRevision,
    ProposalRevisionValidation,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    RunInitiator,
    arbitration_conflict_fingerprint,
    proposal_revision_content_hash,
)
from services.rfp_approval.arbitration import persist_conflict_assessment
from services.rfp_approval.artifact_store import (
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.commercial_store import (
    create_corrected_department_revision,
    create_initial_proposal_revision,
)
from services.rfp_approval.finalization import (
    FinalizationCommand,
    FinalizationError,
    finalize_approved_proposal,
)
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_final_documents,
    rfp_human_decisions,
    rfp_workflow_events,
)
from services.rfp_approval.store import (
    apply_claimed_decision,
    claim_decision_by_id,
    claim_next_decision,
    register_human_decision,
)
from services.rfp_intake.models import rfp_metadata, rfp_tickets
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
)
from tests.rfp_approval_postgres_fixture import isolated_engine
from tests.test_rfp_approval_arbitration_postgres_integration import _assessment
from tests.test_rfp_approval_artifact_store_postgres_integration import (
    _artifact,
    _bundle,
    _producer,
    _provenance,
)
from tests.test_rfp_approval_commercial_store_postgres_integration import (
    _chain,
    _manage_grant,
    _record_input,
    _revision,
)
from tests.test_rfp_approval_store_postgres_integration import (
    _command,
    _register,
    _scenario,
)


def _apply_approval(engine: Engine, scenario) -> None:
    command, _record = _register(engine, scenario, _command(scenario))
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None and claim.decision_id == command.decision.decision_id
    with Session(engine) as session, session.begin():
        result = apply_claimed_decision(
            session,
            decision_id=claim.decision_id,
            claim_token=claim.claim_token,
            claim_epoch=claim.claim_epoch,
            expected_revision_state_version=claim.expected_revision_state_version,
            identity=scenario.identity,
            authorization_reference=scenario.approval_reference,
            execution_id=uuid4(),
        )
    assert result.processing_status == "applied"


def _create_eligible_proposal(engine: Engine, scenario) -> ProposalRevision:
    evidence = [
        EvidenceReference(
            document_id="qa-finalization-source",
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
            condition="Documented monthly shipment volume",
            status="documented",
            evidence=evidence,
        )
    ]
    with Session(engine) as session:
        handoff = session.execute(
            select(rfp_response_handoffs.c.payload).where(
                rfp_response_handoffs.c.run_id == scenario.start.response_run_id
            )
        ).scalar_one()
    source_draft_id = UUID(handoff["global_draft_id"])
    source_evaluation_id = UUID(handoff["global_evaluation_result_id"])
    source_payload = {
        "currency": canonical_value(currency),
        "sla": canonical_value(sla),
        "volume_discounts": canonical_value(discounts),
    }
    source_draft_hash = canonical_hash(source_payload)
    source_evaluation_payload = {"overall_pass": True, "scope": "proposal"}
    source_evaluation_hash = canonical_hash(source_evaluation_payload)
    with Session(engine) as session, session.begin():
        session.execute(
            insert(rfp_response_drafts).values(
                draft_id=source_draft_id,
                run_id=scenario.start.response_run_id,
                scope_key="proposal",
                iteration_count=1,
                contract_version="rfp-global-commercial-terms-v1",
                payload=source_payload,
                payload_hash=source_draft_hash,
            )
        )
        session.execute(
            insert(rfp_response_evaluations).values(
                evaluation_id=source_evaluation_id,
                run_id=scenario.start.response_run_id,
                draft_id=source_draft_id,
                scope_key="proposal",
                contract_version="proposal-evaluation-v1",
                overall_pass=True,
                terminal_status="passed",
                payload=source_evaluation_payload,
                payload_hash=source_evaluation_hash,
            )
        )
    payload = ProposalCorrectedDraftPayload(
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=[],
    )
    content_hash = proposal_revision_content_hash(
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=[],
    )
    provenance = _provenance("part2_import", [(source_draft_id, source_draft_hash)])
    draft = CorrectedDraftArtifact(
        draft_artifact_id=uuid4(),
        approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id,
        scope_key="proposal",
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=provenance,
        created_at=provenance.produced_at,
    )
    evaluation = _artifact(draft, artifact_type="global_evaluation")
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview, source_departments_approved=True)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    validation = ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        revision_content_hash=content_hash,
        global_evaluation=ImmutableArtifactReference(
            artifact_type="global_evaluation",
            artifact_id=bundle.evaluation_artifact_id,
            contract_version=bundle.evaluation_contract_version,
            payload_hash=bundle.evaluation_payload_hash,
            evaluated_content_hash=content_hash,
        ),
        safe_preview=ImmutableArtifactReference(
            artifact_type="safe_preview",
            artifact_id=bundle.safe_preview_artifact_id,
            contract_version=bundle.safe_preview_contract_version,
            payload_hash=bundle.safe_preview_payload_hash,
            evaluated_content_hash=content_hash,
        ),
        mandatory_compliance_passed=True,
        unresolved_input_codes=[],
        blocking_rule_ids=[],
        open_conflict_ids=[],
        source_departments_approved=True,
        eligible_for_synthesis=True,
        validation_hash=bundle.revision_validation_hash,
        validated_at=bundle.validated_at,
    )
    proposal = ProposalRevision(
        revision_id=uuid4(),
        approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id,
        revision_no=1,
        source_global_draft_id=source_draft_id,
        source_global_draft_hash=source_draft_hash,
        source_global_evaluation_id=source_evaluation_id,
        source_global_evaluation_hash=source_evaluation_hash,
        currency=currency,
        sla=sla,
        volume_discounts=discounts,
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=[],
        content_hash=content_hash,
        validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        created_at=datetime.now(timezone.utc),
    )
    manage = _manage_grant(engine, scenario)
    command = InitialProposalRevisionCommand(
        creation_mode="part2_import",
        response_run_id=scenario.start.response_run_id,
        response_handoff_hash=scenario.start.response_handoff_hash,
        proposal=proposal,
        validation_bundle_id=bundle.validation_bundle_id,
        imported_by=RunInitiator(
            principal=scenario.start.started_by.principal,
            authorization_id=manage.authorization_id,
            permission="manage_approval_run",
        ),
        created_by=None,
        idempotency_key=uuid4(),
        requested_at=datetime.now(timezone.utc),
    )
    with Session(engine) as session, session.begin():
        create_initial_proposal_revision(
            session,
            command=command,
            identity=scenario.identity,
            authorization_reference=manage,
            execution_id=uuid4(),
        )
    return proposal


def _insert_rfp_metadata(engine: Engine, scenario, *, document_us: bool) -> None:
    departments = [department.value for department in scenario.start.active_departments]
    values = {
        "ticket_id": scenario.ticket_id,
        "client_country_code": "US" if document_us else None,
        "client_country_original": "United States" if document_us else None,
        "operation_country_codes": ["US"] if document_us else [],
        "currency": "USD",
        "services_requested": departments,
        "departments_needed": departments,
        "readability_metrics": {},
        "conversion_metrics": {},
    }
    with Session(engine) as session, session.begin():
        inserted = session.execute(insert(rfp_metadata).values(**values))
        assert inserted.rowcount == 1
    with Session(engine) as session:
        persisted = session.execute(
            select(rfp_metadata).where(
                rfp_metadata.c.ticket_id == scenario.ticket_id
            )
        ).mappings().one()
    assert all(persisted[key] == value for key, value in values.items())


def _document_us_client(engine: Engine, scenario) -> None:
    _insert_rfp_metadata(engine, scenario, document_us=True)


def _ready_scenario(engine: Engine):
    scenario = _scenario(engine)
    _document_us_client(engine, scenario)
    _apply_approval(engine, scenario)
    proposal = _create_eligible_proposal(engine, scenario)
    return scenario, proposal


def _finalize(engine: Engine, scenario):
    with Session(engine) as session, session.begin():
        return finalize_approved_proposal(
            session,
            command=FinalizationCommand(
                approval_run_id=scenario.approval_run_id,
                ticket_id=scenario.ticket_id,
                final_document_id=uuid4(),
                execution_id=uuid4(),
                generated_at=datetime.now(timezone.utc),
            ),
        )


def test_postgres_missing_client_country_blocks_finalization(
    isolated_engine: Engine,
) -> None:
    scenario = _scenario(isolated_engine)
    _insert_rfp_metadata(isolated_engine, scenario, document_us=False)
    _apply_approval(isolated_engine, scenario)
    _create_eligible_proposal(isolated_engine, scenario)

    with pytest.raises(FinalizationError) as missing_country:
        _finalize(isolated_engine, scenario)

    assert missing_country.value.code == "client_country_not_documented"
    with Session(isolated_engine) as session:
        assert session.execute(
            select(rfp_metadata.c.client_country_code).where(
                rfp_metadata.c.ticket_id == scenario.ticket_id
            )
        ).scalar_one() is None
        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 0
        assert session.execute(
            select(rfp_approval_runs.c.status).where(
                rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == "active"


def test_postgres_finalization_blockers_concurrency_and_replay(
    isolated_engine: Engine,
) -> None:
    scenario, proposal = _ready_scenario(isolated_engine)
    with Session(isolated_engine) as session, session.begin():
        revision = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            )
        ).mappings().one()
        facts = ReturnsSLAFacts(
            observed=[
                ReturnSLAViolation(
                    department_revision_id=scenario.revision_id,
                    sla_hours=Decimal("24"),
                )
            ],
            violating_revision_ids=[scenario.revision_id],
            conflict_demonstrated=True,
        )
        assessment = _assessment(facts)
        target = ArbitrationTarget(
            department=DepartmentId.warehouse,
            department_revision_id=scenario.revision_id,
        )
        conflict = ArbitrationConflict(
            conflict_id=uuid4(),
            approval_run_id=scenario.approval_run_id,
            trigger=facts.trigger,
            facts=facts,
            affected_targets=[target],
            status="demonstrated",
            missing_input_codes=[],
            fingerprint=arbitration_conflict_fingerprint(
                trigger=facts.trigger, facts=facts, affected_targets=[target]
            ),
            detected_at=datetime.now(timezone.utc),
        )
        savepoint = session.begin_nested()
        persist_conflict_assessment(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            assessment=assessment,
            assessed_content_hashes={"warehouse": revision["content_hash"]},
            conflict=conflict,
            execution_id=uuid4(),
        )
        with pytest.raises(FinalizationError) as blocked:
            finalize_approved_proposal(
                session,
                command=FinalizationCommand(
                    approval_run_id=scenario.approval_run_id,
                    ticket_id=scenario.ticket_id,
                    final_document_id=uuid4(),
                    execution_id=uuid4(),
                    generated_at=datetime.now(timezone.utc),
                ),
            )
        assert "open_arbitration_conflict" in blocked.value.code
        savepoint.rollback()

        savepoint = session.begin_nested()
        session.execute(
            update(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            ).values(status="pending_approval", applied_decision_id=None)
        )
        with pytest.raises(FinalizationError) as pending:
            finalize_approved_proposal(
                session,
                command=FinalizationCommand(
                    approval_run_id=scenario.approval_run_id,
                    ticket_id=scenario.ticket_id,
                    final_document_id=uuid4(),
                    execution_id=uuid4(),
                    generated_at=datetime.now(timezone.utc),
                ),
            )
        assert pending.value.code == "department_decision_not_applied"
        savepoint.rollback()

        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=20) for future in (
            pool.submit(_finalize, isolated_engine, scenario),
            pool.submit(_finalize, isolated_engine, scenario),
        )]
    assert sorted(item.replayed for item in results) == [False, True]
    assert len({item.document.final_document_id for item in results}) == 1
    replay = _finalize(isolated_engine, scenario)
    assert replay.replayed and replay.document == results[0].document
    document = replay.document
    assert [section.department for section in document.payload.sections] == [
        DepartmentId.warehouse
    ]
    assert document.payload.global_terms.currency.value == "USD"
    assert document.payload.manifest.proposal_revision_id == proposal.revision_id
    with Session(isolated_engine) as session:
        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 1
        assert session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == scenario.ticket_id
            )
        ).scalar_one() == "terminado"
        assert session.execute(
            select(rfp_approval_runs.c.status).where(
                rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == "completed"
        events = session.execute(
            select(rfp_workflow_events).where(
                rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
                rfp_workflow_events.c.thread_id.like("%:finalization:%"),
            )
        ).mappings().all()
        assert len(events) == 3
        safe_projection = json.dumps(
            [
                {"input": row["safe_input_ref"], "output": row["safe_output_ref"]}
                for row in events
            ]
        ).lower()
        assert all(
            forbidden not in safe_projection
            for forbidden in ("token", "password", "authorization", "prompt")
        )


def test_postgres_superseded_proposal_source_blocks_finalization(
    isolated_engine: Engine,
) -> None:
    scenario, proposal = _ready_scenario(isolated_engine)
    business_input, _identity, reference, _key, _record = _record_input(
        isolated_engine, scenario, "operational_scope"
    )
    draft, bundle = _chain(
        isolated_engine,
        scenario,
        business_input,
        content="Current Warehouse revision B after proposal A was prepared.",
    )
    successor = _revision(
        scenario, scenario.revision, draft, bundle, reference, 2
    )
    with Session(isolated_engine) as session:
        predecessor_state = session.execute(
            select(
                rfp_approval_department_revisions.c.status,
                rfp_approval_department_revisions.c.state_version,
                rfp_approval_department_revisions.c.content_hash,
            ).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            )
        ).one()
    with Session(isolated_engine) as session, session.begin():
        create_corrected_department_revision(
            session,
            revision=successor,
            expected_predecessor_status=predecessor_state.status,
            expected_predecessor_content_hash=predecessor_state.content_hash,
            expected_predecessor_state_version=predecessor_state.state_version,
            identity=scenario.identity,
            authorization_reference=reference,
            idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert successor.validation.eligible_for_approval is True
    assert proposal.source_department_revision_ids == [scenario.revision_id]
    assert successor.revision_id not in proposal.source_department_revision_ids
    with Session(isolated_engine) as session:
        predecessor_before_approval = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            )
        ).mappings().one()
        successor_before_approval = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == successor.revision_id
            )
        ).mappings().one()
    assert predecessor_before_approval["status"] == "superseded"
    assert predecessor_before_approval["is_current"] is False
    assert successor_before_approval["status"] == "pending_approval"
    assert successor_before_approval["is_current"] is True
    assert successor_before_approval["applied_decision_id"] is None
    assert (
        successor_before_approval["validation_bundle_id"]
        == successor.validation_bundle_id
    )
    successor_scenario = replace(
        scenario,
        revision_id=successor.revision_id,
        draft_id=successor.draft_id,
        content_hash=successor.content_hash,
        revision=successor,
    )
    command = _command(successor_scenario)
    with Session(isolated_engine) as session, session.begin():
        registered = register_human_decision(
            session,
            command=command,
            identity=scenario.identity,
            authorization_reference=scenario.approval_reference,
            execution_id=uuid4(),
        )
    assert registered.decision_id == command.decision.decision_id
    assert registered.revision_id == successor.revision_id
    assert registered.processing_status == "queued"
    with Session(isolated_engine) as session, session.begin():
        claim = claim_decision_by_id(
            session,
            decision_id=command.decision.decision_id,
            execution_id=uuid4(),
        )
    assert claim is not None
    assert claim.decision_id == command.decision.decision_id
    assert claim.revision_id == successor.revision_id
    with Session(isolated_engine) as session, session.begin():
        applied = apply_claimed_decision(
            session,
            decision_id=claim.decision_id,
            claim_token=claim.claim_token,
            claim_epoch=claim.claim_epoch,
            expected_revision_state_version=claim.expected_revision_state_version,
            identity=scenario.identity,
            authorization_reference=scenario.approval_reference,
            execution_id=uuid4(),
        )
    assert applied.processing_status == "applied"
    assert applied.revision_status == "approved"
    assert applied.revision_id == successor.revision_id

    with Session(isolated_engine) as session:
        predecessor = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            )
        ).mappings().one()
        current = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == successor.revision_id
            )
        ).mappings().one()
        persisted_decision = session.execute(
            select(rfp_human_decisions).where(
                rfp_human_decisions.c.decision_id == command.decision.decision_id
            )
        ).mappings().one()
        persisted_proposal = session.execute(
            select(rfp_approval_proposal_revisions).where(
                rfp_approval_proposal_revisions.c.proposal_revision_id
                == proposal.revision_id
            )
        ).mappings().one()
        assert predecessor["status"] == "superseded"
        assert predecessor["is_current"] is False
        assert current["status"] == "approved"
        assert current["is_current"] is True
        assert current["validation_bundle_id"] == successor.validation_bundle_id
        assert current["applied_decision_id"] == command.decision.decision_id
        assert persisted_decision["processing_status"] == "applied"
        assert persisted_decision["revision_id"] == successor.revision_id
        assert proposal.source_department_revision_ids == [scenario.revision_id]
        assert successor.revision_id not in proposal.source_department_revision_ids
        assert persisted_proposal["warehouse_revision_id"] == scenario.revision_id
        assert session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == scenario.ticket_id
            )
        ).scalar_one() == "esperando_aprobación"
        assert session.execute(
            select(rfp_approval_runs.c.status).where(
                rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == "active"
        assert session.execute(
            select(func.count()).select_from(rfp_workflow_events).where(
                rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
                rfp_workflow_events.c.thread_id.like("%:finalization:%"),
            )
        ).scalar_one() == 0

    with pytest.raises(FinalizationError) as obsolete:
        _finalize(isolated_engine, scenario)
    blocker_prefix = "fan_in_blocked:"
    assert obsolete.value.code.startswith(blocker_prefix)
    blocker_codes = obsolete.value.code[len(blocker_prefix) :].split(",")
    expected_blocker_codes = {
        "proposal_content_sources_mismatch",
        "proposal_department_sources_mismatch",
    }
    assert set(blocker_codes) == expected_blocker_codes
    assert len(blocker_codes) == len(expected_blocker_codes)
    with Session(isolated_engine) as session:
        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 0
        assert session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == scenario.ticket_id
            )
        ).scalar_one() == "esperando_aprobación"
        assert session.execute(
            select(rfp_approval_runs.c.status).where(
                rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == "active"
        predecessor = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == scenario.revision_id
            )
        ).mappings().one()
        current = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == successor.revision_id
            )
        ).mappings().one()
        persisted_proposal = session.execute(
            select(rfp_approval_proposal_revisions).where(
                rfp_approval_proposal_revisions.c.proposal_revision_id
                == proposal.revision_id
            )
        ).mappings().one()
        assert predecessor["status"] == "superseded"
        assert predecessor["is_current"] is False
        assert current["status"] == "approved"
        assert current["is_current"] is True
        assert current["applied_decision_id"] == command.decision.decision_id
        assert session.execute(
            select(rfp_human_decisions.c.processing_status).where(
                rfp_human_decisions.c.decision_id == command.decision.decision_id
            )
        ).scalar_one() == "applied"
        assert persisted_proposal["warehouse_revision_id"] == scenario.revision_id
        assert persisted_proposal["warehouse_revision_id"] != successor.revision_id
        assert session.execute(
            select(func.count()).select_from(rfp_workflow_events).where(
                rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
                rfp_workflow_events.c.thread_id.like("%:finalization:%"),
            )
        ).scalar_one() == 0


def test_postgres_finalization_insert_failure_rolls_back_everything(
    isolated_engine: Engine,
) -> None:
    scenario, _proposal = _ready_scenario(isolated_engine)
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE FUNCTION reject_final_document() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
            "'synthetic finalization insert failure' USING ERRCODE='P0001'; END $$"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_final_document_before_insert "
            "BEFORE INSERT ON rfp_final_documents FOR EACH ROW "
            "EXECUTE FUNCTION reject_final_document()"
        )
    with pytest.raises(DBAPIError):
        _finalize(isolated_engine, scenario)
    with Session(isolated_engine) as session:
        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 0
        assert session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == scenario.ticket_id
            )
        ).scalar_one() == "esperando_aprobación"
        assert session.execute(
            select(rfp_approval_runs.c.status).where(
                rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == "active"
        assert session.execute(
            select(func.count()).select_from(rfp_workflow_events).where(
                rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
                rfp_workflow_events.c.thread_id.like("%:finalization:%"),
            )
        ).scalar_one() == 0
