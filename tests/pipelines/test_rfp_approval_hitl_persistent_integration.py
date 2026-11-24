"""Persistent HITL validation with isolated PostgreSQL and three processes.

The integration test is fail-closed unless its own HITL guardrails are set. It
never reuses guardrails from prior gates and retains the SQLite checkpoint and
manifest outside the repository for review.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    DepartmentHandoffResult,
    RFPResponseHandoff,
)
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalRevisionValidation,
    ApprovalRunStart,
    AuthorizedBusinessInput,
    AuthorizedHumanActor,
    AuthenticatedPrincipal,
    BusinessInputProvenance,
    ConflictAssessment,
    CurrencyMismatchFacts,
    DepartmentRevision,
    ImmutableArtifactReference,
    InitialRevisionReference,
    QuantityFact,
    QuantityScope,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    RevisionValidationReferences,
    RunInitiator,
    VolumeCapacityFacts,
    WarehouseCapacityInput,
    authorized_business_input_hash,
    conflict_assessment_hash,
)
from pipelines.rfp_approval.hitl import DepartmentApprovalTarget
from services.rfp_approval.artifact_store import (
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.arbitration import persist_conflict_assessment
from services.rfp_approval.commercial_store import record_authorized_business_input
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_authorizations,
    rfp_identity_bindings,
    rfp_principals,
)
from services.rfp_approval.store import (
    approval_start_request_hash,
    link_initial_validation_bundle,
    start_approval_run,
)
from services.rfp_intake.models import rfp_tickets
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_runs,
)
from tests.test_rfp_approval_artifact_store_postgres_integration import (
    EXTENSION_MIGRATION,
    HISTORICAL_MIGRATIONS_AND_SEEDS,
    _artifact,
    _bundle,
    _draft,
    _producer,
)


URL = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_TEST_DATABASE_URL")
AUTHORIZED = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_DDL_DML_AUTHORIZED") == "YES"
EXPECTED_DATABASE = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_EXPECTED_DATABASE")
EXPECTED_USER = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_EXPECTED_USER")
EXPECTED_PORT = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_EXPECTED_PORT")
EXPECTED_ENVIRONMENT = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_EXPECTED_ENVIRONMENT")
VERIFIED_CONTAINER_ID = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_HITL_VERIFIED_CONTAINER_ID"
)
CONTAINER_VERIFIED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_CONTAINER_VERIFIED") == "YES"
)
SCHEMA_ISOLATION_AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_SCHEMA_ISOLATION_AUTHORIZED") == "YES"
)
CHECKPOINT_PATH = os.getenv("TRACKFLOW_RFP_APPROVAL_HITL_CHECKPOINT_PATH")
SCHEMA_PREFIX = "gate_hitl_persistent_"
SKIP_REASON = "requires a newly authorized persistent HITL PostgreSQL execution"


def _checkpoint_guardrail_reason(checkpoint_path: str | None) -> str | None:
    checkpoint = Path(checkpoint_path or "")
    if not checkpoint.is_absolute():
        return "persistent HITL checkpoint path is not absolute"
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        checkpoint_parent = checkpoint.parent.resolve(strict=False)
    except OSError:
        return "persistent HITL checkpoint directory is invalid"
    if checkpoint_parent.parent != temporary_root:
        return "persistent HITL checkpoint directory is not isolated"
    if not re.fullmatch(
        r"trackflow-hitl-runtime-[a-f0-9]{32}", checkpoint_parent.name
    ):
        return "persistent HITL checkpoint directory is not unique"
    if checkpoint.parent.exists() and (
        not checkpoint.parent.is_dir() or checkpoint.parent.is_symlink()
    ):
        return "persistent HITL checkpoint directory is unsafe"
    if not re.fullmatch(
        r"rfp-approval-checkpoints-[a-z0-9-]+\.sqlite3", checkpoint.name
    ):
        return "persistent HITL checkpoint name is not unique"
    if checkpoint.exists():
        return "persistent HITL checkpoint already exists"
    return None


def _guardrail_reason(
    database_url: str | None,
    authorized: bool,
    expected_database: str | None,
    expected_user: str | None,
    expected_port: str | None,
    expected_environment: str | None,
    verified_container_id: str | None,
    container_verified: bool,
    schema_isolation_authorized: bool,
    checkpoint_path: str | None,
) -> str | None:
    if not all(
        (
            database_url,
            authorized,
            expected_database,
            expected_user,
            expected_port,
            expected_environment,
            verified_container_id,
            container_verified,
            schema_isolation_authorized,
            checkpoint_path,
        )
    ):
        return SKIP_REASON
    parsed = make_url(database_url or "")
    if not parsed.drivername.startswith("postgresql"):
        return "persistent HITL URL is not PostgreSQL"
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        return "persistent HITL PostgreSQL is not loopback-isolated"
    try:
        port = int(expected_port or "")
    except ValueError:
        return "persistent HITL expected port is invalid"
    if (parsed.database, parsed.username, parsed.port) != (
        expected_database,
        expected_user,
        port,
    ):
        return "persistent HITL database identity does not match"
    if expected_database != "trackflow_rfp_approval_hitl_persistent_test":
        return "persistent HITL database name is not authorized"
    if expected_user != "trackflow_hitl_persistent":
        return "persistent HITL database user is not authorized"
    if expected_environment != "qa":
        return "persistent HITL environment is not qa"
    if not re.fullmatch(r"[a-f0-9]{64}", verified_container_id or ""):
        return "persistent HITL container identity is invalid"
    checkpoint_reason = _checkpoint_guardrail_reason(checkpoint_path)
    if checkpoint_reason is not None:
        return checkpoint_reason
    if os.getenv("LANGGRAPH_STRICT_MSGPACK") != "true":
        return "LANGGRAPH_STRICT_MSGPACK=true is required"
    return None


def _authorization_reason() -> str | None:
    return _guardrail_reason(
        URL,
        AUTHORIZED,
        EXPECTED_DATABASE,
        EXPECTED_USER,
        EXPECTED_PORT,
        EXPECTED_ENVIRONMENT,
        VERIFIED_CONTAINER_ID,
        CONTAINER_VERIFIED,
        SCHEMA_ISOLATION_AUTHORIZED,
        CHECKPOINT_PATH,
    )


def _quoted_schema(schema: str) -> str:
    assert re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema)
    return f'"{schema}"'


def _schema_exists(engine: Engine, schema: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace "
                    "WHERE nspname=:schema)"
                ),
                {"schema": schema},
            ).scalar_one()
        )


def _run_migration(engine: Engine, schema: str, path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 0
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT", no_parameters=True
    ) as connection:
        connection.exec_driver_sql(f"SET search_path TO {_quoted_schema(schema)}")
        assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
        connection.exec_driver_sql(path.read_text(encoding="utf-8"))


def _run_seed(engine: Engine, schema: str, seed) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
        seed(connection.connection.driver_connection)


@contextmanager
def _isolated_schema(admin: Engine, scenario: Engine, schema: str) -> Iterator[None]:
    confirmed = False
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {_quoted_schema(schema)}")
        if not _schema_exists(admin, schema):
            raise RuntimeError("persistent HITL schema creation was not confirmed")
        confirmed = True
        for migration, seed in HISTORICAL_MIGRATIONS_AND_SEEDS:
            _run_migration(admin, schema, migration)
            _run_seed(scenario, schema, seed)
        _run_migration(admin, schema, EXTENSION_MIGRATION)
        yield
    finally:
        if confirmed:
            with admin.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
                connection.exec_driver_sql(
                    f"DROP SCHEMA {_quoted_schema(schema)} CASCADE"
                )
            if _schema_exists(admin, schema):
                raise RuntimeError("persistent HITL schema cleanup failed")


@dataclass(frozen=True)
class PersistentScenario:
    warehouse: DepartmentApprovalTarget
    lastmile: DepartmentApprovalTarget


def _validation(bundle) -> ApprovalRevisionValidation:
    evaluation = ImmutableArtifactReference(
        artifact_type="evaluation",
        artifact_id=bundle.evaluation_artifact_id,
        contract_version=bundle.evaluation_contract_version,
        payload_hash=bundle.evaluation_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    preview = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=bundle.safe_preview_artifact_id,
        contract_version=bundle.safe_preview_contract_version,
        payload_hash=bundle.safe_preview_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    return ApprovalRevisionValidation(
        references=RevisionValidationReferences(
            revision_content_hash=bundle.content_hash,
            evaluation=evaluation,
            safe_preview=preview,
            validation_hash=bundle.revision_validation_hash,
            validated_at=bundle.validated_at,
        ),
        mandatory_compliance_passed=True,
        blocks_commercial_completeness=False,
        blocking_rule_ids=[],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        global_revalidation_required=False,
        eligible_for_approval=True,
    )


def _revision(department, draft, bundle, source) -> DepartmentRevision:
    return DepartmentRevision(
        revision_id=uuid4(),
        draft_id=draft.draft_artifact_id,
        ticket_id=draft.ticket_id,
        approval_run_id=draft.approval_run_id,
        department=department,
        review_revision_no=1,
        source_part2_draft_id=source["draft_id"],
        source_part2_draft_hash=source["draft_hash"],
        source_part2_evaluation_id=source["evaluation_id"],
        source_part2_evaluation_hash=source["evaluation_hash"],
        source_part2_iteration=1,
        content=draft.payload.content,
        content_hash=draft.content_hash,
        commercial_terms=draft.payload.commercial_terms,
        pending_commercial_items=draft.payload.pending_commercial_items,
        evidence=draft.payload.evidence,
        validation=_validation(bundle),
        validation_bundle_id=bundle.validation_bundle_id,
        status="pending_assessment",
        creation_reason="part2_import",
        created_at=bundle.validated_at,
    )


def _no_conflict_assessment(facts, assessed_at: datetime) -> ConflictAssessment:
    return ConflictAssessment(
        assessment_id=uuid4(),
        trigger=facts.trigger,
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
        assessment_hash=conflict_assessment_hash(
            trigger=facts.trigger,
            facts=facts,
            outcome="no_conflict",
            missing_input_codes=[],
        ),
        assessed_at=assessed_at,
    )


def _seed_scenario(engine: Engine) -> PersistentScenario:
    now = datetime.now(timezone.utc)
    ticket_id, response_run_id, approval_run_id = uuid4(), uuid4(), uuid4()
    global_draft_id = uuid4()
    ana_principal_id, ana_binding_id = uuid4(), uuid4()
    carlos_principal_id, carlos_binding_id = uuid4(), uuid4()
    start_auth_id, warehouse_auth_id, lastmile_auth_id, input_auth_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    sources = {}
    chains = {}
    revisions = {}
    for department, content in (
        (DepartmentId.warehouse, "Persistent Warehouse approval content."),
        (DepartmentId.lastmile, "Persistent Last Mile approval content."),
    ):
        source = {
            "draft_id": uuid4(),
            "draft_hash": canonical_hash({"source": department.value, "kind": "draft"}),
            "evaluation_id": uuid4(),
            "evaluation_hash": canonical_hash(
                {"source": department.value, "kind": "evaluation"}
            ),
        }
        draft = _draft(
            content=content,
            scope=department.value,
            run_id=approval_run_id,
            ticket_id=ticket_id,
        )
        evaluation = _artifact(draft)
        preview = _artifact(draft, artifact_type="safe_preview")
        bundle = _bundle(draft, evaluation, preview)
        sources[department] = source
        chains[department] = (draft, evaluation, preview, bundle)
        revisions[department] = _revision(department, draft, bundle, source)

    ana_principal = AuthenticatedPrincipal(
        issuer="urn:trackflow:qa:oidc:hitl-persistent",
        subject="qa-ana-hitl-persistent",
        trackflow_user_id="qa-ana-hitl-persistent",
        auth_source="oidc",
        general_role="manager",
        synthetic=True,
    )
    ana_identity = ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer=ana_principal.issuer,
        subject=ana_principal.subject,
        trackflow_user_id=ana_principal.trackflow_user_id,
        general_role=ana_principal.general_role,
        principal_id=ana_principal_id,
        binding_id=ana_binding_id,
        responsible_code="ana_whitfield",
        synthetic=True,
    )
    results = [
        DepartmentHandoffResult(
            department=department,
            terminal_status="passed",
            iteration_count=1,
            final_draft_id=sources[department]["draft_id"],
            evaluation_result_id=sources[department]["evaluation_id"],
            safe_preview_id=chains[department][2].artifact_id,
            blocks_commercial_completeness=False,
            requires_human_input=False,
            blocking_rule_ids=[],
        )
        for department in (DepartmentId.warehouse, DepartmentId.lastmile)
    ]
    handoff = RFPResponseHandoff(
        ticket_id=ticket_id,
        run_id=response_run_id,
        source_handoff_hash=canonical_hash({"source": str(ticket_id)}),
        outcome="ready_for_part3",
        all_sections_passed=True,
        global_compliance_passed=True,
        selected_departments=[DepartmentId.warehouse, DepartmentId.lastmile],
        department_results=results,
        global_draft_id=global_draft_id,
        global_evaluation_result_id=uuid4(),
        unresolved_items=[],
        created_at=now,
    )
    handoff_payload = canonical_value(handoff)
    handoff_hash = canonical_hash(handoff_payload)
    start = ApprovalRunStart(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        response_handoff_hash=handoff_hash,
        active_departments=[DepartmentId.warehouse, DepartmentId.lastmile],
        initial_revisions=[
            InitialRevisionReference(
                department=department,
                source_draft_id=sources[department]["draft_id"],
                source_draft_hash=sources[department]["draft_hash"],
                source_evaluation_id=sources[department]["evaluation_id"],
                source_evaluation_hash=sources[department]["evaluation_hash"],
                source_part2_iteration=1,
                terminal_status="passed",
            )
            for department in (DepartmentId.warehouse, DepartmentId.lastmile)
        ],
        started_by=RunInitiator(
            principal=ana_principal,
            authorization_id=start_auth_id,
            permission="start_approval_run",
        ),
        started_at=now,
    )
    with Session(engine) as session, session.begin():
        session.execute(
            insert(rfp_tickets).values(
                ticket_id=ticket_id,
                status="en_evaluación",
                raw_pdf_path="hitl-persistent/input.pdf",
                uploaded_by_subject="qa-hitl-persistent",
            )
        )
        session.execute(
            insert(rfp_response_runs).values(
                run_id=response_run_id,
                ticket_id=ticket_id,
                source_handoff_version="rfp-intake-v1",
                source_handoff_hash=handoff.source_handoff_hash,
                status="ready_for_part3",
                global_iteration_count=1,
            )
        )
        for department in (DepartmentId.warehouse, DepartmentId.lastmile):
            source = sources[department]
            session.execute(
                insert(rfp_response_drafts).values(
                    draft_id=source["draft_id"],
                    run_id=response_run_id,
                    scope_key=department.value,
                    iteration_count=1,
                    contract_version="rfp-department-draft-v1",
                    payload={"department": department.value},
                    payload_hash=source["draft_hash"],
                )
            )
            session.execute(
                insert(rfp_response_evaluations).values(
                    evaluation_id=source["evaluation_id"],
                    run_id=response_run_id,
                    draft_id=source["draft_id"],
                    scope_key=department.value,
                    contract_version="section-evaluation-v1",
                    overall_pass=True,
                    terminal_status="passed",
                    payload={"department": department.value},
                    payload_hash=source["evaluation_hash"],
                )
            )
        session.execute(
            insert(rfp_response_handoffs).values(
                run_id=response_run_id,
                ticket_id=ticket_id,
                contract_version="rfp-response-v1",
                outcome="ready_for_part3",
                payload=handoff_payload,
                payload_hash=handoff_hash,
            )
        )
        for principal_id, user_id, responsible in (
            (ana_principal_id, "qa-ana-hitl-persistent", "ana_whitfield"),
            (carlos_principal_id, "qa-carlos-hitl-persistent", "carlos_vega"),
        ):
            session.execute(
                insert(rfp_principals).values(
                    principal_id=principal_id,
                    environment="qa",
                    trackflow_user_id=user_id,
                    responsible_code=responsible,
                    synthetic=True,
                )
            )
        for binding_id, principal_id, subject in (
            (ana_binding_id, ana_principal_id, "qa-ana-hitl-persistent"),
            (
                carlos_binding_id,
                carlos_principal_id,
                "qa-carlos-hitl-persistent",
            ),
        ):
            session.execute(
                insert(rfp_identity_bindings).values(
                    binding_id=binding_id,
                    principal_id=principal_id,
                    environment="qa",
                    auth_source="oidc",
                    issuer="urn:trackflow:qa:oidc:hitl-persistent",
                    subject=subject,
                )
            )
        grants = (
            (start_auth_id, ana_principal_id, "start_approval_run", None, "run"),
            (
                warehouse_auth_id,
                ana_principal_id,
                "approve_department",
                "warehouse",
                "department_exact",
            ),
            (
                lastmile_auth_id,
                carlos_principal_id,
                "approve_department",
                "lastmile",
                "department_exact",
            ),
            (
                input_auth_id,
                ana_principal_id,
                "provide_business_input",
                "warehouse",
                "department_exact",
            ),
        )
        for authorization_id, principal_id, permission, department, policy in grants:
            session.execute(
                insert(rfp_authorizations).values(
                    authorization_id=authorization_id,
                    principal_id=principal_id,
                    environment="qa",
                    permission=permission,
                    department_scope=department,
                    target_policy=policy,
                    scope_fingerprint=canonical_hash(
                        {"permission": permission, "department": department}
                    ),
                    authorization_version=1,
                    active_from=now - timedelta(minutes=1),
                )
            )
    idempotency_key = uuid4()
    request_hash = approval_start_request_hash(
        start, list(revisions.values()), idempotency_key
    )
    with Session(engine) as session, session.begin():
        start_approval_run(
            session,
            start=start,
            initial_revisions=list(revisions.values()),
            identity=ana_identity,
            authorization_reference=AuthorizationReference(start_auth_id, 1),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            execution_id=uuid4(),
        )
    for department in (DepartmentId.warehouse, DepartmentId.lastmile):
        draft, evaluation, preview, bundle = chains[department]
        with Session(engine) as session, session.begin():
            persist_corrected_draft(session, draft=draft, producer=_producer())
            persist_validation_artifact(
                session, artifact=evaluation, producer=_producer()
            )
            persist_validation_artifact(
                session, artifact=preview, producer=_producer()
            )
            persist_validation_bundle(session, bundle=bundle, producer=_producer())

    capacity_payload = WarehouseCapacityInput(
        site_code="WH-QA-HITL-01",
        capacity_value=Decimal("150"),
        capacity_unit="m3",
        period="month",
        available_from=date.today(),
    )
    capacity_actor = AuthorizedHumanActor(
        principal=ana_principal,
        authorization_id=input_auth_id,
        official_responsible="Ana Whitfield",
        permission="provide_business_input",
        department_scope=DepartmentId.warehouse,
    )
    capacity_provenance = BusinessInputProvenance(
        evidence_type="responsible_attestation",
        evidence_id=uuid4(),
        source_name="QA persistent HITL warehouse capacity attestation",
        issued_at=now,
        source_payload_hash=canonical_hash(
            {
                "site_code": capacity_payload.site_code,
                "capacity_value": str(capacity_payload.capacity_value),
                "capacity_unit": capacity_payload.capacity_unit,
                "period": capacity_payload.period,
            }
        ),
    )
    capacity_input = AuthorizedBusinessInput(
        input_id=uuid4(),
        ticket_id=ticket_id,
        approval_run_id=approval_run_id,
        input_kind="warehouse_capacity",
        payload=capacity_payload,
        provenance=capacity_provenance,
        actor=capacity_actor,
        payload_hash=authorized_business_input_hash(
            input_kind="warehouse_capacity",
            payload=capacity_payload,
            provenance=capacity_provenance,
            actor=capacity_actor,
        ),
        recorded_at=now,
    )
    quantity_scope = QuantityScope(
        location_code=capacity_payload.site_code,
        service_code="warehouse-storage",
    )
    required_volume = QuantityFact(
        value=Decimal("100"),
        dimension="volume",
        unit="m3",
        period="month",
        operational_scope=quantity_scope,
    )
    available_capacity = QuantityFact(
        value=capacity_payload.capacity_value,
        dimension="volume",
        unit=capacity_payload.capacity_unit,
        period=capacity_payload.period,
        operational_scope=quantity_scope,
    )
    assessed_hashes = {
        department.value: revision.content_hash
        for department, revision in revisions.items()
    }
    assessments = (
        _no_conflict_assessment(
            VolumeCapacityFacts(
                required_volume=required_volume,
                available_capacity=available_capacity,
                capacity_input_id=capacity_input.input_id,
                comparison_status="comparable",
                exceeds_capacity=False,
            ),
            now,
        ),
        _no_conflict_assessment(
            ReturnsSLAFacts(
                observed=[
                    ReturnSLAViolation(
                        department_revision_id=revision.revision_id,
                        sla_hours=Decimal("48"),
                    )
                    for revision in revisions.values()
                ],
                violating_revision_ids=[],
                conflict_demonstrated=False,
            ),
            now,
        ),
        _no_conflict_assessment(
            CurrencyMismatchFacts(
                client_country="US",
                expected_currency="USD",
                proposal_revision_id=global_draft_id,
                proposal_currency="USD",
                monetary_terms=[],
                mismatched_revision_ids=[],
                conflict_demonstrated=False,
            ),
            now,
        ),
    )
    with Session(engine) as session, session.begin():
        record_authorized_business_input(
            session,
            business_input=capacity_input,
            identity=ana_identity,
            authorization_reference=AuthorizationReference(input_auth_id, 1),
            idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
        for assessment in assessments:
            persist_conflict_assessment(
                session,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                assessment=assessment,
                assessed_content_hashes=assessed_hashes,
                conflict=None,
                execution_id=uuid4(),
            )

    for department in (DepartmentId.warehouse, DepartmentId.lastmile):
        bundle = chains[department][3]
        with Session(engine) as session, session.begin():
            link_initial_validation_bundle(
                session,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                revision_id=revisions[department].revision_id,
                validation_bundle_id=bundle.validation_bundle_id,
                identity=ana_identity,
                authorization_reference=AuthorizationReference(start_auth_id, 1),
                permission="start_approval_run",
                execution_id=uuid4(),
            )
    return PersistentScenario(
        warehouse=DepartmentApprovalTarget(
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            revision_id=revisions[DepartmentId.warehouse].revision_id,
            department=DepartmentId.warehouse,
        ),
        lastmile=DepartmentApprovalTarget(
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            revision_id=revisions[DepartmentId.lastmile].revision_id,
            department=DepartmentId.lastmile,
        ),
    )


def _target_manifest(target: DepartmentApprovalTarget) -> dict[str, str]:
    return {
        "approval_run_id": str(target.approval_run_id),
        "ticket_id": str(target.ticket_id),
        "revision_id": str(target.revision_id),
        "department": target.department.value,
    }


def test_persistent_hitl_guardrails_fail_closed_offline(tmp_path) -> None:
    assert _guardrail_reason(
        None, False, None, None, None, None, None, False, False, None
    ) == SKIP_REASON
    assert (
        _guardrail_reason(
            "postgresql+psycopg2://u:p@db.example/test",
            True,
            "trackflow_rfp_approval_hitl_persistent_test",
            "trackflow_hitl_persistent",
            "65476",
            "qa",
            "a" * 64,
            True,
            True,
            str(tmp_path / "rfp-approval-checkpoints-test.sqlite3"),
        )
        == "persistent HITL PostgreSQL is not loopback-isolated"
    )


def test_persistent_hitl_checkpoint_guardrail_accepts_unique_system_temp_path(
    monkeypatch,
) -> None:
    runtime = (
        Path(tempfile.gettempdir()) / f"trackflow-hitl-runtime-{uuid4().hex}"
    )
    checkpoint = runtime / "rfp-approval-checkpoints-offline.sqlite3"

    assert _checkpoint_guardrail_reason(str(checkpoint)) is None
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    assert _guardrail_reason(
        "postgresql+psycopg2://trackflow_hitl_persistent:test-only@127.0.0.1:65476/"
        "trackflow_rfp_approval_hitl_persistent_test",
        True,
        "trackflow_rfp_approval_hitl_persistent_test",
        "trackflow_hitl_persistent",
        "65476",
        "qa",
        "a" * 64,
        True,
        True,
        str(checkpoint),
    ) is None


def test_persistent_hitl_checkpoint_guardrail_rejects_nonisolated_path(
    tmp_path,
) -> None:
    checkpoint = tmp_path / "rfp-approval-checkpoints-offline.sqlite3"

    assert _checkpoint_guardrail_reason(str(checkpoint)) in {
        "persistent HITL checkpoint directory is not isolated",
        "persistent HITL checkpoint directory is not unique",
    }


@pytest.mark.parametrize(
    "department,content",
    [
        (DepartmentId.warehouse, "Offline Warehouse contract."),
        (DepartmentId.lastmile, "Offline Last Mile contract."),
    ],
)
def test_persistent_department_contracts_build_offline(
    department, content
) -> None:
    run_id, ticket_id = uuid4(), uuid4()
    source = {
        "draft_id": uuid4(),
        "draft_hash": canonical_hash({"offline": "draft", "scope": department.value}),
        "evaluation_id": uuid4(),
        "evaluation_hash": canonical_hash(
            {"offline": "evaluation", "scope": department.value}
        ),
    }
    draft = _draft(
        content=content,
        scope=department.value,
        run_id=run_id,
        ticket_id=ticket_id,
    )
    evaluation = _artifact(draft)
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview)
    revision = _revision(department, draft, bundle, source)

    assert revision.department == department
    assert revision.validation_bundle_id == bundle.validation_bundle_id
    assert revision.validation.eligible_for_approval is True
    assert revision.validation.references.validation_hash == bundle.revision_validation_hash


def test_real_three_process_persistent_hitl() -> None:
    reason = _authorization_reason()
    if reason is not None:
        pytest.skip(reason)
    assert URL is not None and CHECKPOINT_PATH is not None
    admin = create_engine(URL, pool_pre_ping=True)
    schema = f"{SCHEMA_PREFIX}{uuid4().hex}"
    scenario_engine = create_engine(
        URL,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    checkpoint = Path(CHECKPOINT_PATH)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = checkpoint.with_suffix(".manifest.json")
    assert not checkpoint.exists() and not manifest_path.exists()
    try:
        with _isolated_schema(admin, scenario_engine, schema):
            scenario = _seed_scenario(scenario_engine)
            manifest = {
                "schema": schema,
                "checkpoint_path": str(checkpoint),
                "warehouse": _target_manifest(scenario.warehouse),
                "lastmile": _target_manifest(scenario.lastmile),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            child_env = os.environ.copy()
            child_env["LANGGRAPH_STRICT_MSGPACK"] = "true"
            phases = []
            for phase in ("a", "b", "c"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "tests.rfp_approval_hitl_persistent_runner",
                        phase,
                        str(manifest_path),
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    env=child_env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                assert completed.returncode == 0, completed.stderr
                phases.append(json.loads(completed.stdout.strip().splitlines()[-1]))
            assert [item["phase"] for item in phases] == ["a", "b", "c"]
            assert len({item["pid"] for item in phases}) == 3
            assert phases[1]["lastmile_status"] == "approved"
            assert phases[2]["warehouse_status"] == "pending_approval"
            assert phases[2]["decision_count"] == 1
            raw_checkpoint = checkpoint.read_bytes()
            assert b"qa-carlos-hitl-persistent" not in raw_checkpoint
            assert b"qa-ana-hitl-persistent" not in raw_checkpoint
    finally:
        scenario_engine.dispose()
        admin.dispose()
