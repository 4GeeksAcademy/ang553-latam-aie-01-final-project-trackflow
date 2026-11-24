"""Real isolated PostgreSQL proof of the durable arbitration cycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationResolution,
    ArbitrationTarget,
    AuthorizedHumanActor,
    ConflictAssessment,
    CorrectedRevisionEvidence,
    HumanArbitrationExecution,
    ImmutableArtifactReference,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    arbitration_conflict_fingerprint,
    arbitration_resolution_hash,
    conflict_assessment_hash,
)
from pipelines.rfp_approval.fan_in import determine_fan_in_readiness
from services.rfp_approval.arbitration import (
    persist_conflict_assessment,
    record_arbitration_resolution,
)
from services.rfp_approval.authorization import AuthorizationDenied
from services.rfp_approval.commercial_store import create_corrected_department_revision
from services.rfp_approval.identity import VerifiedAuthenticationContext
from services.rfp_approval.models import (
    rfp_arbitration_conflicts,
    rfp_arbitration_resolutions,
    rfp_authorizations,
    rfp_workflow_events,
)
from tests.rfp_approval_postgres_fixture import (
    AUTHORIZED_ENV,
    LIFECYCLE_MIGRATIONS,
    URL_ENV,
    guardrail_reason,
    isolated_engine,
)
from tests.test_rfp_approval_commercial_store_postgres_integration import (
    _approve,
    _chain,
    _miguel_context,
    _record_input,
    _revision,
)
from tests.test_rfp_approval_store_postgres_integration import _scenario


def _authentication(principal) -> VerifiedAuthenticationContext:
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment="qa",
        auth_source=principal.auth_source,
        issuer=principal.issuer,
        subject=principal.subject,
        trackflow_user_id=principal.trackflow_user_id,
        general_role=principal.general_role,
    )


def _assessment(facts: ReturnsSLAFacts) -> ConflictAssessment:
    outcome = "demonstrated" if facts.conflict_demonstrated else "no_conflict"
    digest = conflict_assessment_hash(
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=[],
    )
    return ConflictAssessment(
        assessment_id=uuid4(),
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=[],
        assessment_hash=digest,
        assessed_at=datetime.now(timezone.utc),
    )


def _request_resolution(conflict, target, actor) -> ArbitrationResolution:
    reason = "Correct the unsupported return SLA before approval."
    digest = arbitration_resolution_hash(
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
    )
    return ArbitrationResolution(
        resolution_id=uuid4(),
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        trigger=conflict.trigger,
        action="request_changes",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        execution=HumanArbitrationExecution(
            execution_id=uuid4(),
            conflict_id=conflict.conflict_id,
            actor=actor,
            decided_at=datetime.now(timezone.utc),
        ),
        resolution_hash=digest,
    )


def test_guardrails_are_shared_and_fail_closed() -> None:
    assert guardrail_reason({})
    assert URL_ENV.startswith("TRACKFLOW_RFP_APPROVAL_ARBITRATION_FINALIZATION_")
    assert AUTHORIZED_ENV.startswith(
        "TRACKFLOW_RFP_APPROVAL_ARBITRATION_FINALIZATION_"
    )
    assert LIFECYCLE_MIGRATIONS == (
        "rfp_intake.sql",
        "rfp_response_generation.sql",
        "rfp_approval.sql",
        "rfp_approval_extension.sql",
        "rfp_approval_monthly_storage_volume.sql",
        "rfp_approval_indicative_validation_bundle.sql",
    )


def test_postgres_durable_arbitration_cycle(isolated_engine: Engine) -> None:
    scenario = _scenario(isolated_engine)
    miguel_identity, _input_reference, miguel_input_actor = _miguel_context(
        isolated_engine
    )
    now = datetime.now(timezone.utc)
    miguel_grant = uuid4()
    miguel_incompatible_grant = uuid4()
    with Session(isolated_engine) as session, session.begin():
        session.execute(
            insert(rfp_authorizations).values(
                authorization_id=miguel_incompatible_grant,
                principal_id=miguel_identity.principal_id,
                environment="qa",
                permission="arbitrate",
                department_scope=None,
                trigger_scope="currency-mismatch",
                target_policy="cross_department",
                scope_fingerprint=canonical_hash(
                    {"arbitration": str(miguel_incompatible_grant)}
                ),
                authorization_version=1,
                active_from=now - timedelta(minutes=1),
            )
        )
    incompatible_actor = AuthorizedHumanActor(
        principal=miguel_input_actor.principal,
        authorization_id=miguel_incompatible_grant,
        official_responsible="Miguel Torres",
        permission="arbitrate",
        trigger_scope="returns-sla-breach",
    )
    target = ArbitrationTarget(
        department=DepartmentId.warehouse,
        department_revision_id=scenario.revision_id,
    )
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
        detected_at=now,
    )
    with Session(isolated_engine) as session, session.begin():
        first = persist_conflict_assessment(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            assessment=assessment,
            assessed_content_hashes={"warehouse": scenario.content_hash},
            conflict=conflict,
            execution_id=uuid4(),
        )
    with Session(isolated_engine) as session, session.begin():
        replay = persist_conflict_assessment(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            assessment=assessment,
            assessed_content_hashes={"warehouse": scenario.content_hash},
            conflict=conflict,
            execution_id=uuid4(),
        )
    assert first.conflict_status == "open" and not first.replayed
    assert replay.replayed
    with Session(isolated_engine) as session:
        assert "open_arbitration_conflict" in determine_fan_in_readiness(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
        ).blockers

    with Session(isolated_engine) as session:
        counts_before = (
            session.execute(
                select(func.count()).select_from(rfp_arbitration_resolutions).where(
                    rfp_arbitration_resolutions.c.approval_run_id
                    == scenario.approval_run_id
                )
            ).scalar_one(),
            session.execute(
                select(func.count()).select_from(rfp_workflow_events).where(
                    rfp_workflow_events.c.approval_run_id == scenario.approval_run_id
                )
            ).scalar_one(),
        )
    wrong = _request_resolution(conflict, target, incompatible_actor)
    with Session(isolated_engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied) as denied:
            record_arbitration_resolution(
                session,
                approval_run_id=scenario.approval_run_id,
                ticket_id=scenario.ticket_id,
                resolution=wrong,
                idempotency_key=uuid4(),
                authentication=_authentication(incompatible_actor.principal),
                execution_id=uuid4(),
            )
    assert denied.value.code == "no_effective_authorization"
    with Session(isolated_engine) as session:
        counts_after = (
            session.execute(
                select(func.count()).select_from(rfp_arbitration_resolutions).where(
                    rfp_arbitration_resolutions.c.approval_run_id
                    == scenario.approval_run_id
                )
            ).scalar_one(),
            session.execute(
                select(func.count()).select_from(rfp_workflow_events).where(
                    rfp_workflow_events.c.approval_run_id == scenario.approval_run_id
                )
            ).scalar_one(),
        )
    assert counts_after == counts_before

    with Session(isolated_engine) as session, session.begin():
        session.execute(
            insert(rfp_authorizations).values(
                authorization_id=miguel_grant,
                principal_id=miguel_identity.principal_id,
                environment="qa",
                permission="arbitrate",
                department_scope=None,
                trigger_scope="returns-sla-breach",
                target_policy="non_reverse_or_cross",
                scope_fingerprint=canonical_hash(
                    {"arbitration": str(miguel_grant)}
                ),
                authorization_version=1,
                active_from=now - timedelta(minutes=1),
            )
        )
    miguel_actor = AuthorizedHumanActor(
        principal=miguel_input_actor.principal,
        authorization_id=miguel_grant,
        official_responsible="Miguel Torres",
        permission="arbitrate",
        trigger_scope="returns-sla-breach",
    )

    request = _request_resolution(conflict, target, miguel_actor)
    request_key = uuid4()
    with Session(isolated_engine) as session, session.begin():
        requested = record_arbitration_resolution(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            resolution=request,
            idempotency_key=request_key,
            authentication=_authentication(miguel_actor.principal),
            execution_id=uuid4(),
        )
    with Session(isolated_engine) as session, session.begin():
        requested_replay = record_arbitration_resolution(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            resolution=request,
            idempotency_key=request_key,
            authentication=_authentication(miguel_actor.principal),
            execution_id=uuid4(),
        )
    assert requested.conflict_status == "changes_requested"
    assert requested_replay.replayed

    _approve(isolated_engine, scenario.revision_id)
    business_input, _identity, input_reference, _key, _input_record = _record_input(
        isolated_engine, scenario, "operational_scope"
    )
    corrected_draft, corrected_bundle = _chain(
        isolated_engine,
        scenario,
        business_input,
        content="Warehouse returns handling uses a documented 48-hour SLA.",
    )
    corrected_revision = _revision(
        scenario,
        scenario.revision,
        corrected_draft,
        corrected_bundle,
        input_reference,
        2,
    )
    with Session(isolated_engine) as session, session.begin():
        corrected_record = create_corrected_department_revision(
            session,
            revision=corrected_revision,
            expected_predecessor_status="approved",
            expected_predecessor_content_hash=scenario.content_hash,
            expected_predecessor_state_version=0,
            identity=scenario.identity,
            authorization_reference=input_reference,
            idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert corrected_record.review_revision_no == 2
    assert "48-hour SLA" in corrected_revision.content
    corrected_facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(
                department_revision_id=corrected_revision.revision_id,
                sla_hours=Decimal("48"),
            )
        ],
        violating_revision_ids=[],
        conflict_demonstrated=False,
    )
    corrected_assessment = _assessment(corrected_facts)
    with Session(isolated_engine) as session, session.begin():
        persist_conflict_assessment(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            assessment=corrected_assessment,
            assessed_content_hashes={"warehouse": corrected_revision.content_hash},
            conflict=None,
            execution_id=uuid4(),
        )
    evidence = CorrectedRevisionEvidence(
        original_target=target,
        corrected_revision_id=corrected_revision.revision_id,
        corrected_content_hash=corrected_revision.content_hash,
        revision_validation=ImmutableArtifactReference(
            artifact_type="revision_validation",
            artifact_id=corrected_revision.validation_bundle_id,
            contract_version="rfp-approval-validation-bundle-v1",
            payload_hash=corrected_revision.validation.references.validation_hash,
            evaluated_content_hash=corrected_revision.content_hash,
        ),
        conflict_recheck=ImmutableArtifactReference(
            artifact_type="conflict_recheck",
            artifact_id=corrected_assessment.assessment_id,
            contract_version=corrected_assessment.contract_version,
            payload_hash=corrected_assessment.assessment_hash,
            evaluated_content_hash=corrected_revision.content_hash,
        ),
    )
    reason = "The corrected revision now documents the supported 48-hour SLA."
    accept_hash = arbitration_resolution_hash(
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        action="accept_resolution",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        corrected_evidence=[evidence],
        post_correction_assessment=corrected_assessment,
    )
    accepted_contract = ArbitrationResolution(
        resolution_id=uuid4(),
        conflict_id=conflict.conflict_id,
        conflict_fingerprint=conflict.fingerprint,
        trigger=conflict.trigger,
        action="accept_resolution",
        targets=[target],
        authorized_input_ids=[],
        reason=reason,
        execution=HumanArbitrationExecution(
            execution_id=uuid4(), conflict_id=conflict.conflict_id,
            actor=miguel_actor, decided_at=datetime.now(timezone.utc),
        ),
        corrected_evidence=[evidence],
        post_correction_assessment=corrected_assessment,
        resolution_hash=accept_hash,
    )
    accept_key = uuid4()
    with Session(isolated_engine) as session, session.begin():
        accepted = record_arbitration_resolution(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            resolution=accepted_contract,
            idempotency_key=accept_key,
            authentication=_authentication(miguel_actor.principal),
            execution_id=uuid4(),
        )
    with Session(isolated_engine) as session, session.begin():
        accepted_replay = record_arbitration_resolution(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
            resolution=accepted_contract,
            idempotency_key=accept_key,
            authentication=_authentication(miguel_actor.principal),
            execution_id=uuid4(),
        )
    assert accepted.conflict_status == "resolved"
    assert accepted_replay.replayed
    with Session(isolated_engine) as session:
        assert session.execute(
            select(rfp_arbitration_conflicts.c.status).where(
                rfp_arbitration_conflicts.c.conflict_id == conflict.conflict_id
            )
        ).scalar_one() == "resolved"
        assert session.execute(
            select(func.count()).select_from(rfp_arbitration_resolutions).where(
                rfp_arbitration_resolutions.c.conflict_id == conflict.conflict_id
            )
        ).scalar_one() == 2
        assert session.execute(
            select(func.count()).select_from(rfp_workflow_events).where(
                rfp_workflow_events.c.conflict_id == conflict.conflict_id
            )
        ).scalar_one() == 6
    with Session(isolated_engine) as session:
        assert "open_arbitration_conflict" not in determine_fan_in_readiness(
            session,
            approval_run_id=scenario.approval_run_id,
            ticket_id=scenario.ticket_id,
        ).blockers
