"""Real PostgreSQL integration tests for the S2B commercial store.

Each scenario owns a confirmed UUID schema. No connection is opened unless all
nine S2B guardrails identify one explicitly authorized disposable database.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, insert, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects import postgresql
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import EvidenceReference, GlobalCurrency, GlobalSLA
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalRevisionValidation,
    ApprovalValidationArtifact,
    ApprovalValidationBundle,
    AuthenticatedPrincipal,
    AuthorizedBusinessInput,
    AuthorizedHumanActor,
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    ContentEvaluationArtifactPayload,
    CorrectedDraftArtifact,
    CurrencyConversionInput,
    CurrencyQuoteInput,
    DepartmentCorrectedDraftPayload,
    DepartmentRevision,
    GlobalCommercialTermInput,
    ImmutableArtifactReference,
    InitialProposalRevisionCommand,
    OperationalScopeInput,
    ProposalCorrectedDraftPayload,
    ProposalRevision,
    ProposalRevisionValidation,
    RevisionValidationReferences,
    RunInitiator,
    VolumeDiscountInput,
    VolumeDiscountTierInput,
    WarehouseCapacityInput,
    authorized_business_input_hash,
    department_revision_content_hash,
    proposal_revision_content_hash,
)
from services.rfp_approval.artifact_store import (
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import (
    AuthorizationDenied,
    AuthorizationReference,
)
from services.rfp_approval.commercial_store import (
    create_corrected_department_revision,
    create_corrected_proposal_revision,
    create_initial_proposal_revision,
    record_authorized_business_input,
)
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_department_revisions,
    rfp_final_documents,
    rfp_approval_proposal_revisions,
    rfp_authorizations,
    rfp_identity_bindings,
    rfp_principals,
    rfp_authorized_business_inputs,
    rfp_workflow_events,
)
from services.rfp_approval.store import ApprovalStoreError
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
)
from tests.test_rfp_approval_artifact_store_postgres_integration import (
    EXTENSION_MIGRATION,
    HISTORICAL_MIGRATIONS_AND_SEEDS,
    _artifact,
    _bundle,
    _producer,
    _provenance,
    _wait_for_lock,
)
from tests.test_rfp_approval_store_postgres_integration import Scenario, _scenario
from tests.test_rfp_approval_postgres_migration import IDS


URL = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_TEST_DATABASE_URL")
AUTHORIZED = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_DDL_DML_AUTHORIZED") == "YES"
EXPECTED_DATABASE = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_EXPECTED_DATABASE")
EXPECTED_USER = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_EXPECTED_USER")
EXPECTED_PORT = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_EXPECTED_PORT")
EXPECTED_ENVIRONMENT = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_EXPECTED_ENVIRONMENT")
VERIFIED_CONTAINER_ID = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_VERIFIED_CONTAINER_ID")
CONTAINER_VERIFIED = os.getenv("TRACKFLOW_RFP_APPROVAL_COMMERCIAL_CONTAINER_VERIFIED") == "YES"
SCHEMA_ISOLATION_AUTHORIZED = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_COMMERCIAL_SCHEMA_ISOLATION_AUTHORIZED"
) == "YES"
SCHEMA_PREFIX = "gate2b3b1_s2b_test_"
SKIP_REASON = "requires an explicitly authorized isolated S2B PostgreSQL database"


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
) -> str | None:
    if not all((database_url, authorized, expected_database, expected_user,
                expected_port, expected_environment, verified_container_id,
                container_verified, schema_isolation_authorized)):
        return SKIP_REASON
    parsed = make_url(database_url or "")
    if not parsed.drivername.startswith("postgresql"):
        return "S2B URL is not PostgreSQL"
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        return "S2B PostgreSQL host is not loopback-isolated"
    try:
        port = int(expected_port or "")
    except ValueError:
        return "S2B PostgreSQL port is invalid"
    if (parsed.database, parsed.username, parsed.port) != (
        expected_database, expected_user, port
    ):
        return "S2B PostgreSQL target does not match its expected identity"
    if not (expected_database or "").startswith("trackflow_rfp_approval_commercial_test_s2b"):
        return "S2B database does not use the disposable prefix"
    if not (expected_user or "").startswith("trackflow_gate2b3b1_s2b"):
        return "S2B user does not use the gate prefix"
    if expected_environment != "qa":
        return "S2B environment is not qa"
    if not re.fullmatch(r"[a-f0-9]{64}", verified_container_id or ""):
        return "S2B container identity is not verified"
    return None


def _authorization_reason() -> str | None:
    return _guardrail_reason(
        URL, AUTHORIZED, EXPECTED_DATABASE, EXPECTED_USER, EXPECTED_PORT,
        EXPECTED_ENVIRONMENT, VERIFIED_CONTAINER_ID, CONTAINER_VERIFIED,
        SCHEMA_ISOLATION_AUTHORIZED,
    )


def _quoted_schema(schema: str) -> str:
    assert re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema)
    return f'"{schema}"'


def _schema_exists(admin: Engine, schema: str) -> bool:
    with admin.connect() as connection:
        return bool(connection.execute(text(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"
        ), {"schema": schema}).scalar_one())


def _create_schema(admin: Engine, schema: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {_quoted_schema(schema)}")


def _run_migration(admin: Engine, schema: str, path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 0
    with admin.connect().execution_options(
        isolation_level="AUTOCOMMIT", no_parameters=True,
    ) as connection:
        connection.exec_driver_sql(f"SET search_path TO {_quoted_schema(schema)}")
        assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema
        connection.exec_driver_sql(path.read_text(encoding="utf-8"))
        assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == schema


def _seed_schema(engine: Engine, schema: str, seed) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
        assert connection.connection.driver_connection.autocommit is True
        seed(connection.connection.driver_connection)
        assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
    with engine.connect() as connection:
        assert connection.connection.driver_connection.autocommit is False


def _drop_schema(admin: Engine, schema: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql(f"DROP SCHEMA {_quoted_schema(schema)} CASCADE")
    if _schema_exists(admin, schema):
        raise RuntimeError(f"S2B schema cleanup failed: {schema}")


@contextmanager
def _isolated_schema(admin: Engine, scenario: Engine, schema: str) -> Iterator[None]:
    confirmed = False
    try:
        _create_schema(admin, schema)
        if not _schema_exists(admin, schema):
            raise RuntimeError(f"S2B schema creation was not confirmed: {schema}")
        confirmed = True
        for migration, seed in HISTORICAL_MIGRATIONS_AND_SEEDS:
            _run_migration(admin, schema, migration)
            _seed_schema(scenario, schema, seed)
        _run_migration(admin, schema, EXTENSION_MIGRATION)
        yield
    except Exception as primary:
        if confirmed:
            try:
                _drop_schema(admin, schema)
            except Exception as cleanup:
                raise ExceptionGroup("S2B scenario and cleanup failed", [primary, cleanup]) from None
        raise
    else:
        if confirmed:
            _drop_schema(admin, schema)


@pytest.fixture
def engine() -> Iterator[Engine]:
    reason = _authorization_reason()
    if reason is not None:
        pytest.skip(reason)
    assert URL is not None
    admin = create_engine(URL, pool_pre_ping=True)
    with admin.connect() as connection:
        database, user, tables, schemas = connection.execute(text(
            "SELECT current_database(),current_user,"
            "(SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
            "('pg_catalog','information_schema')),(SELECT count(*) FROM "
            f"pg_namespace WHERE nspname LIKE '{SCHEMA_PREFIX}%')"
        )).one()
        assert (database, user, tables, schemas) == (EXPECTED_DATABASE, EXPECTED_USER, 0, 0)
    schema = f"{SCHEMA_PREFIX}{uuid4().hex}"
    scenario = create_engine(
        URL, pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with _isolated_schema(admin, scenario, schema):
            try:
                with scenario.connect() as connection:
                    assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
                yield scenario
            finally:
                scenario.dispose()
    finally:
        admin.dispose()


def test_guardrails_fail_closed_without_authorization() -> None:
    assert _guardrail_reason(None, False, None, None, None, None, None, False, False)
    assert _guardrail_reason(
        "postgresql+psycopg2://u:p@db.example/test", True, "test", "u", "5432",
        "qa", "a" * 64, True, True,
    ) == "S2B PostgreSQL host is not loopback-isolated"


def test_offline_sql_compilation_preserves_lock_and_search_path_isolation() -> None:
    statement = select(rfp_approval_department_revisions).where(
        rfp_approval_department_revisions.c.revision_id == uuid4()
    ).with_for_update()
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "rfp_approval_department_revisions" in compiled
    assert "public." not in compiled
    assert "search_path" not in compiled


def _grant(
    engine: Engine,
    scenario: Scenario,
    *,
    expires_at: datetime | None = None,
) -> AuthorizationReference:
    authorization_id = uuid4()
    with Session(engine) as session, session.begin():
        session.execute(insert(rfp_authorizations).values(
            authorization_id=authorization_id,
            principal_id=scenario.identity.principal_id,
            environment="qa",
            permission="provide_business_input",
            department_scope="warehouse",
            target_policy="department_exact",
            scope_fingerprint=canonical_hash({"grant": str(authorization_id)}),
            authorization_version=1,
            active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
            expires_at=expires_at,
            granted_by_principal_id=scenario.identity.principal_id,
        ))
    return AuthorizationReference(authorization_id, 1)


def _actor(scenario: Scenario, reference: AuthorizationReference) -> AuthorizedHumanActor:
    return AuthorizedHumanActor(
        principal=scenario.start.started_by.principal,
        authorization_id=reference.authorization_id,
        official_responsible="Ana Whitfield",
        permission="provide_business_input",
        department_scope=DepartmentId.warehouse,
    )


def _payload(kind: str):
    today = date.today()
    values = {
        "client_price_quote": ClientPriceQuoteInput(
            department=DepartmentId.warehouse, service_code="storage",
            amount=Decimal("10"), currency="USD", unit="m3/month",
            effective_from=today,
        ),
        "warehouse_capacity": WarehouseCapacityInput(
            site_code="WH-QA-01", capacity_value=Decimal("150"),
            capacity_unit="m3", period="month", available_from=today,
        ),
        "operational_scope": OperationalScopeInput(
            department=DepartmentId.warehouse, scope_code="ambient-storage",
            description="QA ambient storage", included=True, effective_from=today,
        ),
        "currency_quote": CurrencyQuoteInput(
            term_code="global-base", amount=Decimal("20"), currency="USD",
            effective_from=today,
        ),
        "currency_conversion": CurrencyConversionInput(
            source_currency="EUR", target_currency="USD",
            conversion_rate=Decimal("1.08"), effective_at=datetime.now(timezone.utc),
            source_term_codes=["global-base"],
        ),
        "global_commercial_term": GlobalCommercialTermInput(
            term_code="fuel-policy", value="QA documented policy",
            effective_from=today,
        ),
        "volume_discount": VolumeDiscountInput(
            volume_unit="shipment", period="month",
            tiers=[VolumeDiscountTierInput(
                minimum_volume=Decimal("0"), maximum_volume=None,
                discount_percentage=Decimal("2"),
            )], effective_from=today,
        ),
    }
    evidence = {
        "client_price_quote": "approved_quote",
        "warehouse_capacity": "responsible_attestation",
        "operational_scope": "responsible_attestation",
        "currency_quote": "approved_quote",
        "currency_conversion": "authorized_conversion",
        "global_commercial_term": "approved_policy",
        "volume_discount": "approved_policy",
    }
    return values[kind], evidence[kind]


def _business_input(
    scenario: Scenario,
    reference: AuthorizationReference,
    kind: str = "warehouse_capacity",
    *,
    actor: AuthorizedHumanActor | None = None,
) -> AuthorizedBusinessInput:
    payload, evidence_type = _payload(kind)
    provenance = BusinessInputProvenance(
        evidence_type=evidence_type,
        evidence_id=uuid4(),
        source_name="S2B authorized QA source",
        issued_at=datetime.now(timezone.utc),
        source_payload_hash=canonical_hash({"kind": kind, "source": "s2b"}),
    )
    actor = actor or _actor(scenario, reference)
    data = {
        "input_id": uuid4(), "ticket_id": scenario.ticket_id,
        "approval_run_id": scenario.approval_run_id, "input_kind": kind,
        "payload": payload, "provenance": provenance, "actor": actor,
        "recorded_at": datetime.now(timezone.utc),
    }
    return AuthorizedBusinessInput(
        **data,
        payload_hash=authorized_business_input_hash(
            input_kind=kind, payload=payload, provenance=provenance, actor=actor,
        ),
    )


def _miguel_context(engine: Engine):
    principal_id = UUID(IDS["principal"])
    binding_id = UUID(IDS["binding"])
    authorization_id = uuid4()
    principal = scenario_principal = AuthenticatedPrincipal(
        issuer="urn:trackflow:qa:local",
        subject="qa-miguel",
        trackflow_user_id="qa-miguel",
        auth_source="local", general_role="user", synthetic=True,
    )
    identity = ResolvedIdentity._from_persisted_binding(
        environment="qa", auth_source="local", issuer=principal.issuer,
        subject=principal.subject, trackflow_user_id=principal.trackflow_user_id,
        general_role="user", principal_id=principal_id, binding_id=binding_id,
        responsible_code="miguel_torres", synthetic=True,
    )
    with Session(engine) as session, session.begin():
        persisted = session.execute(
            select(
                rfp_principals.c.principal_id,
                rfp_principals.c.environment,
                rfp_principals.c.trackflow_user_id,
                rfp_principals.c.responsible_code,
                rfp_identity_bindings.c.binding_id,
                rfp_identity_bindings.c.auth_source,
                rfp_identity_bindings.c.issuer,
                rfp_identity_bindings.c.subject,
            ).join(
                rfp_identity_bindings,
                rfp_identity_bindings.c.principal_id
                == rfp_principals.c.principal_id,
            ).where(
                rfp_principals.c.principal_id == principal_id,
                rfp_identity_bindings.c.binding_id == binding_id,
            )
        ).mappings().one()
        assert dict(persisted) == {
            "principal_id": principal_id,
            "environment": "qa",
            "trackflow_user_id": "qa-miguel",
            "responsible_code": "miguel_torres",
            "binding_id": binding_id,
            "auth_source": "local",
            "issuer": "urn:trackflow:qa:local",
            "subject": "qa-miguel",
        }
        session.execute(
            update(rfp_principals)
            .where(rfp_principals.c.principal_id == principal_id)
            .values(synthetic=True)
        )
        session.execute(insert(rfp_authorizations).values(
            authorization_id=authorization_id, principal_id=principal_id,
            environment="qa", permission="provide_business_input",
            target_policy="proposal_wide",
            scope_fingerprint=canonical_hash({"global": str(authorization_id)}),
            authorization_version=1,
            active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
            granted_by_principal_id=principal_id,
        ))
    reference = AuthorizationReference(authorization_id, 1)
    actor = AuthorizedHumanActor(
        principal=scenario_principal, authorization_id=authorization_id,
        official_responsible="Miguel Torres", permission="provide_business_input",
    )
    return identity, reference, actor


def _record_input(
    engine: Engine, scenario: Scenario, kind: str = "warehouse_capacity"
):
    global_scope = kind in {
        "currency_quote", "currency_conversion", "global_commercial_term",
        "volume_discount",
    }
    if global_scope:
        identity, reference, actor = _miguel_context(engine)
    else:
        identity, reference, actor = scenario.identity, _grant(engine, scenario), None
    value = _business_input(scenario, reference, kind, actor=actor)
    key = uuid4()
    with Session(engine) as session, session.begin():
        result = record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    return value, identity, reference, key, result


def _chain(
    engine: Engine,
    scenario: Scenario,
    input_value: AuthorizedBusinessInput,
    *,
    content: str,
    conflict: bool = False,
) -> tuple[CorrectedDraftArtifact, ApprovalValidationBundle]:
    evidence = [EvidenceReference(
        document_id="authorized-business-input-v1",
        section_id=str(input_value.input_id),
        evidence_hash=input_value.payload_hash,
    )]
    payload = DepartmentCorrectedDraftPayload(
        department=DepartmentId.warehouse, content=content,
        commercial_terms=[], pending_commercial_items=[], evidence=evidence,
    )
    content_hash = department_revision_content_hash(
        content=content, commercial_terms=[], pending_commercial_items=[], evidence=evidence,
    )
    provenance = _provenance(
        "human_correction", [(input_value.input_id, input_value.payload_hash)]
    )
    draft = CorrectedDraftArtifact(
        draft_artifact_id=uuid4(), approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id, scope_key="warehouse", payload=payload,
        content_hash=content_hash, payload_hash=canonical_hash(payload),
        provenance=provenance, created_at=provenance.produced_at,
    )
    if conflict:
        summary = ContentEvaluationArtifactPayload(
            mandatory_compliance_passed=True,
            blocks_commercial_completeness=False, blocking_rule_ids=[],
            unresolved_input_codes=[], open_conflict_ids=[uuid4()], eligible=False,
        )
        evaluation_provenance = _provenance(
            "deterministic_evaluator", [(draft.draft_artifact_id, draft.content_hash)]
        )
        evaluation = ApprovalValidationArtifact(
            artifact_id=uuid4(), artifact_type="evaluation",
            approval_run_id=draft.approval_run_id, ticket_id=draft.ticket_id,
            scope_key="warehouse", corrected_draft_id=draft.draft_artifact_id,
            evaluated_content_hash=draft.content_hash, payload=summary,
            payload_hash=canonical_hash(summary), provenance=evaluation_provenance,
            created_at=evaluation_provenance.produced_at,
        )
    else:
        evaluation = _artifact(draft)
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    return draft, bundle


def _revision(
    scenario: Scenario,
    predecessor: DepartmentRevision,
    draft: CorrectedDraftArtifact,
    bundle: ApprovalValidationBundle,
    reference: AuthorizationReference,
    number: int,
) -> DepartmentRevision:
    evaluation = ImmutableArtifactReference(
        artifact_type="evaluation", artifact_id=bundle.evaluation_artifact_id,
        contract_version=bundle.evaluation_contract_version,
        payload_hash=bundle.evaluation_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    preview = ImmutableArtifactReference(
        artifact_type="safe_preview", artifact_id=bundle.safe_preview_artifact_id,
        contract_version=bundle.safe_preview_contract_version,
        payload_hash=bundle.safe_preview_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    validation = ApprovalRevisionValidation(
        references=RevisionValidationReferences(
            revision_content_hash=bundle.content_hash, evaluation=evaluation,
            safe_preview=preview, validation_hash=bundle.revision_validation_hash,
            validated_at=bundle.validated_at,
        ),
        mandatory_compliance_passed=bundle.mandatory_compliance_passed,
        blocks_commercial_completeness=bundle.blocks_commercial_completeness,
        blocking_rule_ids=bundle.blocking_rule_ids,
        unresolved_input_codes=bundle.unresolved_input_codes,
        open_conflict_ids=bundle.open_conflict_ids,
        global_revalidation_required=True,
        eligible_for_approval=bundle.eligible,
    )
    return DepartmentRevision(
        revision_id=uuid4(), draft_id=draft.draft_artifact_id,
        ticket_id=scenario.ticket_id, approval_run_id=scenario.approval_run_id,
        department=DepartmentId.warehouse, review_revision_no=number,
        source_part2_draft_id=predecessor.source_part2_draft_id,
        source_part2_draft_hash=predecessor.source_part2_draft_hash,
        source_part2_evaluation_id=predecessor.source_part2_evaluation_id,
        source_part2_evaluation_hash=predecessor.source_part2_evaluation_hash,
        source_part2_iteration=predecessor.source_part2_iteration,
        supersedes_revision_id=predecessor.revision_id,
        content=draft.payload.content, content_hash=draft.content_hash,
        commercial_terms=draft.payload.commercial_terms,
        pending_commercial_items=draft.payload.pending_commercial_items,
        evidence=draft.payload.evidence, validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        status="pending_approval" if bundle.eligible else "blocked_conflict",
        created_by=_actor(scenario, reference), creation_reason="authorized_input",
        created_at=datetime.now(timezone.utc),
    )


def _approve(engine: Engine, revision_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == revision_id
        ).values(status="approved"))


def _correct(
    engine: Engine, scenario: Scenario, *, conflict: bool = False
):
    _approve(engine, scenario.revision_id)
    value, _identity, reference, _key, _result = _record_input(engine, scenario)
    draft, bundle = _chain(
        engine, scenario, value, content="S2B corrected warehouse", conflict=conflict,
    )
    revision = _revision(scenario, scenario.revision, draft, bundle, reference, 2)
    with Session(engine) as session, session.begin():
        record = create_corrected_department_revision(
            session, revision=revision, expected_predecessor_status="approved",
            expected_predecessor_content_hash=scenario.content_hash,
            expected_predecessor_state_version=0, identity=scenario.identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    return revision, reference, record


def _manage_grant(engine: Engine, scenario: Scenario) -> AuthorizationReference:
    authorization_id = uuid4()
    with Session(engine) as session, session.begin():
        session.execute(insert(rfp_authorizations).values(
            authorization_id=authorization_id,
            principal_id=scenario.identity.principal_id,
            environment="qa", permission="manage_approval_run",
            target_policy="run",
            scope_fingerprint=canonical_hash({"manage": str(authorization_id)}),
            authorization_version=1,
            active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
            granted_by_principal_id=scenario.identity.principal_id,
        ))
    return AuthorizationReference(authorization_id, 1)


def _global_terms():
    evidence = [EvidenceReference(
        document_id="s2b-global-source", section_id="currency",
        evidence_hash="a" * 64,
    )]
    return (
        GlobalCurrency(value="USD", status="documented", evidence=evidence),
        GlobalSLA(
            applicability="not_applicable", percentage=None,
            measurement_period=None, scope=[], exclusions=[], evidence=[],
        ),
    )


def _prepare_initial_proposal(
    engine: Engine,
    scenario: Scenario,
    *,
    mode: str,
    changed_terms: bool = False,
):
    _approve(engine, scenario.revision_id)
    currency, sla = _global_terms()
    source_currency = currency
    if changed_terms:
        source_currency = GlobalCurrency(
            value="EUR", status="documented", evidence=currency.evidence
        )
    source_payload = {
        "currency": canonical_value(source_currency),
        "sla": canonical_value(sla),
        "volume_discounts": [],
    }
    with Session(engine) as session:
        handoff_payload = session.execute(
            select(rfp_response_handoffs.c.payload).where(
                rfp_response_handoffs.c.run_id == scenario.start.response_run_id
            )
        ).scalar_one()
    source_draft_id = UUID(handoff_payload["global_draft_id"])
    source_evaluation_id = UUID(
        handoff_payload["global_evaluation_result_id"]
    )
    source_draft_hash = canonical_hash(source_payload)
    source_evaluation_payload = {"overall_pass": True, "scope": "proposal"}
    source_evaluation_hash = canonical_hash(source_evaluation_payload)
    with Session(engine) as session, session.begin():
        session.execute(insert(rfp_response_drafts).values(
            draft_id=source_draft_id, run_id=scenario.start.response_run_id,
            scope_key="proposal", iteration_count=1,
            contract_version="rfp-global-commercial-terms-v1",
            payload=source_payload, payload_hash=source_draft_hash,
        ))
        session.execute(insert(rfp_response_evaluations).values(
            evaluation_id=source_evaluation_id, run_id=scenario.start.response_run_id,
            draft_id=source_draft_id, scope_key="proposal",
            contract_version="proposal-evaluation-v1", overall_pass=True,
            terminal_status="passed", payload=source_evaluation_payload,
            payload_hash=source_evaluation_hash,
        ))
    authorized_ids: list[UUID] = []
    if mode == "part2_import":
        identity = scenario.identity
        reference = _manage_grant(engine, scenario)
        imported_by = RunInitiator(
            principal=scenario.start.started_by.principal,
            authorization_id=reference.authorization_id,
            permission="manage_approval_run",
        )
        created_by = None
        source_kind = "part2_import"
        provenance_pairs = [(source_draft_id, source_draft_hash)]
    else:
        value, identity, reference, _key, _result = _record_input(
            engine, scenario, "global_commercial_term"
        )
        authorized_ids = [value.input_id]
        imported_by = None
        created_by = value.actor
        source_kind = "human_correction"
        provenance_pairs = [(value.input_id, value.payload_hash)]
    payload = ProposalCorrectedDraftPayload(
        currency=currency, sla=sla, volume_discounts=[],
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=authorized_ids,
    )
    content_hash = proposal_revision_content_hash(
        currency=currency, sla=sla, volume_discounts=[],
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=authorized_ids,
    )
    provenance = _provenance(source_kind, provenance_pairs)
    draft = CorrectedDraftArtifact(
        draft_artifact_id=uuid4(), approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id, scope_key="proposal", payload=payload,
        content_hash=content_hash, payload_hash=canonical_hash(payload),
        provenance=provenance, created_at=provenance.produced_at,
    )
    evaluation = _artifact(draft, artifact_type="global_evaluation")
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview, source_departments_approved=True)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="global_evaluation", artifact_id=bundle.evaluation_artifact_id,
        contract_version=bundle.evaluation_contract_version,
        payload_hash=bundle.evaluation_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview", artifact_id=bundle.safe_preview_artifact_id,
        contract_version=bundle.safe_preview_contract_version,
        payload_hash=bundle.safe_preview_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    validation = ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        revision_content_hash=bundle.content_hash,
        global_evaluation=evaluation_ref, safe_preview=preview_ref,
        mandatory_compliance_passed=True, unresolved_input_codes=[],
        blocking_rule_ids=[], open_conflict_ids=[],
        source_departments_approved=True, eligible_for_synthesis=True,
        validation_hash=bundle.revision_validation_hash,
        validated_at=bundle.validated_at,
    )
    proposal = ProposalRevision(
        revision_id=uuid4(), approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id, revision_no=1,
        source_global_draft_id=source_draft_id,
        source_global_draft_hash=source_draft_hash,
        source_global_evaluation_id=source_evaluation_id,
        source_global_evaluation_hash=source_evaluation_hash,
        currency=currency, sla=sla, volume_discounts=[],
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=authorized_ids, content_hash=content_hash,
        validation=validation, validation_bundle_id=bundle.validation_bundle_id,
        created_at=datetime.now(timezone.utc),
    )
    command = InitialProposalRevisionCommand(
        creation_mode=mode, response_run_id=scenario.start.response_run_id,
        response_handoff_hash=scenario.start.response_handoff_hash,
        proposal=proposal, validation_bundle_id=bundle.validation_bundle_id,
        imported_by=imported_by, created_by=created_by,
        idempotency_key=uuid4(), requested_at=datetime.now(timezone.utc),
    )
    return command, identity, reference


def _create_initial(engine: Engine, scenario: Scenario, *, mode: str, changed_terms=False):
    command, identity, reference = _prepare_initial_proposal(
        engine, scenario, mode=mode, changed_terms=changed_terms
    )
    with Session(engine) as session, session.begin():
        record = create_initial_proposal_revision(
            session, command=command, identity=identity,
            authorization_reference=reference, execution_id=uuid4(),
        )
    return command, identity, reference, record


def _prepare_corrected_global(
    engine: Engine,
    scenario: Scenario,
    initial: InitialProposalRevisionCommand,
    *,
    blocked: bool = False,
):
    value, identity, reference, _key, _record = _record_input(
        engine, scenario, "global_commercial_term"
    )
    payload = ProposalCorrectedDraftPayload(
        currency=initial.proposal.currency, sla=initial.proposal.sla,
        volume_discounts=[],
        source_department_revision_ids=[scenario.revision_id],
        authorized_input_ids=[value.input_id],
    )
    content_hash = proposal_revision_content_hash(
        currency=payload.currency, sla=payload.sla, volume_discounts=[],
        source_department_revision_ids=payload.source_department_revision_ids,
        authorized_input_ids=payload.authorized_input_ids,
    )
    provenance = _provenance(
        "human_correction", [(value.input_id, value.payload_hash)]
    )
    draft = CorrectedDraftArtifact(
        draft_artifact_id=uuid4(), approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id, scope_key="proposal", payload=payload,
        content_hash=content_hash, payload_hash=canonical_hash(payload),
        provenance=provenance, created_at=provenance.produced_at,
    )
    if blocked:
        summary = ContentEvaluationArtifactPayload(
            mandatory_compliance_passed=False,
            blocks_commercial_completeness=False,
            blocking_rule_ids=["TF-GLOBAL-COMPLIANCE"],
            unresolved_input_codes=[], open_conflict_ids=[], eligible=False,
        )
        evaluation_provenance = _provenance(
            "deterministic_evaluator", [(draft.draft_artifact_id, draft.content_hash)]
        )
        evaluation = ApprovalValidationArtifact(
            artifact_id=uuid4(), artifact_type="global_evaluation",
            approval_run_id=draft.approval_run_id, ticket_id=draft.ticket_id,
            scope_key="proposal", corrected_draft_id=draft.draft_artifact_id,
            evaluated_content_hash=draft.content_hash, payload=summary,
            payload_hash=canonical_hash(summary), provenance=evaluation_provenance,
            created_at=evaluation_provenance.produced_at,
        )
    else:
        evaluation = _artifact(draft, artifact_type="global_evaluation")
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview, source_departments_approved=True)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="global_evaluation", artifact_id=bundle.evaluation_artifact_id,
        contract_version=bundle.evaluation_contract_version,
        payload_hash=bundle.evaluation_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview", artifact_id=bundle.safe_preview_artifact_id,
        contract_version=bundle.safe_preview_contract_version,
        payload_hash=bundle.safe_preview_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    validation = ProposalRevisionValidation(
        contract_version="rfp-proposal-revision-validation-v2",
        revision_content_hash=bundle.content_hash,
        global_evaluation=evaluation_ref, safe_preview=preview_ref,
        mandatory_compliance_passed=bundle.mandatory_compliance_passed,
        unresolved_input_codes=bundle.unresolved_input_codes,
        blocking_rule_ids=bundle.blocking_rule_ids,
        open_conflict_ids=bundle.open_conflict_ids,
        source_departments_approved=True,
        eligible_for_synthesis=bundle.eligible,
        validation_hash=bundle.revision_validation_hash,
        validated_at=bundle.validated_at,
    )
    revision = ProposalRevision(
        revision_id=uuid4(), approval_run_id=scenario.approval_run_id,
        ticket_id=scenario.ticket_id, revision_no=2,
        source_global_draft_id=initial.proposal.source_global_draft_id,
        source_global_draft_hash=initial.proposal.source_global_draft_hash,
        source_global_evaluation_id=initial.proposal.source_global_evaluation_id,
        source_global_evaluation_hash=initial.proposal.source_global_evaluation_hash,
        supersedes_revision_id=initial.proposal.revision_id,
        currency=payload.currency, sla=payload.sla, volume_discounts=[],
        source_department_revision_ids=payload.source_department_revision_ids,
        authorized_input_ids=payload.authorized_input_ids,
        content_hash=content_hash, validation=validation,
        validation_bundle_id=bundle.validation_bundle_id,
        created_at=datetime.now(timezone.utc),
    )
    return revision, identity, reference


@pytest.mark.parametrize("input_kind", [
    "client_price_quote", "warehouse_capacity", "operational_scope",
    "currency_quote", "currency_conversion", "global_commercial_term",
    "volume_discount",
])
def test_postgres_persists_each_authorized_input_immutably(
    engine: Engine, input_kind: str
) -> None:
    scenario = _scenario(engine)
    value, identity, reference, key, first = _record_input(engine, scenario, input_kind)
    with Session(engine) as session, session.begin():
        replay = record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert not first.replayed and replay.replayed
    divergent = value.model_copy(update={"input_id": uuid4()})
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        record_authorized_business_input(
            session, business_input=divergent, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert raised.value.code == "business_input_idempotency_key_reused"
    with Session(engine) as session, pytest.raises(DBAPIError):
        session.execute(update(rfp_authorized_business_inputs).where(
            rfp_authorized_business_inputs.c.input_id == value.input_id
        ).values(payload_hash="f" * 64))


def test_postgres_rejects_cross_department_and_global_scope_grants(engine: Engine) -> None:
    scenario = _scenario(engine)
    ana_reference = _grant(engine, scenario)
    _miguel_identity, miguel_reference, miguel_actor = _miguel_context(engine)
    value = _business_input(
        scenario, miguel_reference, "currency_quote", actor=miguel_actor
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError):
        record_authorized_business_input(
            session, business_input=value, identity=scenario.identity,
            authorization_reference=ana_reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_authorized_business_inputs
        ).where(rfp_authorized_business_inputs.c.approval_run_id == scenario.approval_run_id)).scalar_one() == 0


def test_postgres_department_a_to_b_preserves_heritage_bundle_and_audit(engine: Engine) -> None:
    scenario = _scenario(engine)
    revision, _reference, record = _correct(engine, scenario)
    assert record.status == "pending_approval"
    with Session(engine) as session:
        rows = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id.in_(
                [scenario.revision_id, revision.revision_id]
            )
        )).mappings().all()
        by_id = {row["revision_id"]: row for row in rows}
        assert not by_id[scenario.revision_id]["is_current"]
        assert by_id[revision.revision_id]["is_current"]
        assert by_id[revision.revision_id]["validation_bundle_id"] == revision.validation_bundle_id
        assert by_id[revision.revision_id]["source_part2_iteration"] == scenario.revision.source_part2_iteration
        assert session.execute(select(func.count()).select_from(rfp_workflow_events).where(
            rfp_workflow_events.c.department_revision_id == revision.revision_id
        )).scalar_one() == 1


def test_postgres_blocked_conflict_is_persisted_and_not_eligible(engine: Engine) -> None:
    scenario = _scenario(engine)
    revision, _reference, record = _correct(engine, scenario, conflict=True)
    assert record.status == "blocked_conflict"
    with Session(engine) as session:
        row = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == revision.revision_id
        )).mappings().one()
        assert row["status"] == "blocked_conflict"
        assert row["validation_payload"]["eligible_for_approval"] is False


def test_postgres_review_four_is_rejected_before_any_database_mutation(engine: Engine) -> None:
    scenario = _scenario(engine)
    revision_b, _reference, _record = _correct(engine, scenario)
    _approve(engine, revision_b.revision_id)
    value, _identity, reference, _key, _input_record = _record_input(engine, scenario)
    draft, bundle = _chain(
        engine, scenario, value, content="S2B corrected warehouse revision C"
    )
    revision_c = _revision(
        scenario, revision_b, draft, bundle, reference, 3
    )
    with Session(engine) as session, session.begin():
        result = create_corrected_department_revision(
            session, revision=revision_c, expected_predecessor_status="approved",
            expected_predecessor_content_hash=revision_b.content_hash,
            expected_predecessor_state_version=0, identity=scenario.identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert result.review_revision_no == 3
    payload = canonical_value(revision_c)
    payload["review_revision_no"] = 4
    with pytest.raises(ValueError):
        DepartmentRevision.model_validate(payload)
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_approval_department_revisions
        ).where(rfp_approval_department_revisions.c.approval_run_id == scenario.approval_run_id)).scalar_one() == 3


def test_postgres_stale_bundle_rejection_is_atomic(engine: Engine) -> None:
    scenario = _scenario(engine)
    _approve(engine, scenario.revision_id)
    value, _identity, reference, _key, _result = _record_input(engine, scenario)
    draft, bundle = _chain(engine, scenario, value, content="S2B stale bundle")
    revision = _revision(scenario, scenario.revision, draft, bundle, reference, 2)
    revision = revision.model_copy(update={"validation_bundle_id": uuid4()})
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        create_corrected_department_revision(
            session, revision=revision, expected_predecessor_status="approved",
            expected_predecessor_content_hash=scenario.content_hash,
            expected_predecessor_state_version=0, identity=scenario.identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert raised.value.code == "validation_bundle_not_found_or_mismatch"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_approval_department_revisions
        ).where(rfp_approval_department_revisions.c.revision_id == revision.revision_id)).scalar_one() == 0


def test_postgres_two_successors_race_and_only_one_cas_wins(engine: Engine) -> None:
    scenario = _scenario(engine)
    _approve(engine, scenario.revision_id)
    value, _identity, reference, _key, _result = _record_input(engine, scenario)
    candidates = []
    for label in ("one", "two"):
        draft, bundle = _chain(engine, scenario, value, content=f"S2B contender {label}")
        candidates.append(_revision(scenario, scenario.revision, draft, bundle, reference, 2))
    ready, release = Event(), Event()
    loser_name = f"s2b-loser-{uuid4()}"

    def attempt(candidate: DepartmentRevision, hold: bool, name: str | None = None):
        try:
            with Session(engine) as session, session.begin():
                if name:
                    session.execute(text("SET LOCAL application_name=:name"), {"name": name})
                result = create_corrected_department_revision(
                    session, revision=candidate, expected_predecessor_status="approved",
                    expected_predecessor_content_hash=scenario.content_hash,
                    expected_predecessor_state_version=0, identity=scenario.identity,
                    authorization_reference=reference, idempotency_key=uuid4(),
                    execution_id=uuid4(),
                )
                if hold:
                    ready.set()
                    assert release.wait(10)
                return result.revision_id
        except ApprovalStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(attempt, candidates[0], True)
        assert ready.wait(10)
        loser = pool.submit(attempt, candidates[1], False, loser_name)
        _wait_for_lock(engine, loser_name)
        release.set()
        assert winner.result(timeout=10) == candidates[0].revision_id
        assert loser.result(timeout=10) == "predecessor_revision_stale"


def test_postgres_replay_rechecks_revocation_and_writes_nothing(engine: Engine) -> None:
    scenario = _scenario(engine)
    value, identity, reference, key, _result = _record_input(engine, scenario)
    with Session(engine) as session, session.begin():
        assert record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        ).replayed
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_authorizations).where(
            rfp_authorizations.c.authorization_id == reference.authorization_id
        ).values(revoked_at=func.clock_timestamp()))
    with Session(engine) as session, pytest.raises(AuthorizationDenied) as denied:
        record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert denied.value.code == "no_effective_authorization"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_authorized_business_inputs
        ).where(rfp_authorized_business_inputs.c.input_id == value.input_id)).scalar_one() == 1


def test_postgres_wrong_binding_and_official_reassignment_are_denied(engine: Engine) -> None:
    scenario = _scenario(engine)
    value, identity, reference, key, _result = _record_input(engine, scenario)
    wrong = ResolvedIdentity._from_persisted_binding(
        environment="qa", auth_source=identity.auth_source, issuer=identity.issuer,
        subject=identity.subject, trackflow_user_id=identity.trackflow_user_id,
        general_role=identity.general_role, principal_id=identity.principal_id,
        binding_id=uuid4(), responsible_code="ana_whitfield", synthetic=True,
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as denied:
        record_authorized_business_input(
            session, business_input=value, identity=wrong,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert denied.value.code == "binding_not_found"
    other_principal = ResolvedIdentity._from_persisted_binding(
        environment="qa", auth_source=identity.auth_source,
        issuer=identity.issuer, subject=identity.subject,
        trackflow_user_id=identity.trackflow_user_id,
        general_role=identity.general_role, principal_id=uuid4(),
        binding_id=uuid4(), responsible_code="ana_whitfield", synthetic=True,
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as denied:
        record_authorized_business_input(
            session, business_input=value, identity=other_principal,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert denied.value.code == "principal_not_found"
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_principals).where(
            rfp_principals.c.principal_id == identity.principal_id
        ).values(responsible_code="carlos_vega"))
    with Session(engine) as session, pytest.raises(AuthorizationDenied) as denied:
        record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    assert denied.value.code == "no_effective_authorization"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_authorized_business_inputs
        ).where(
            rfp_authorized_business_inputs.c.input_id == value.input_id
        )).scalar_one() == 1


def test_postgres_expired_grant_and_production_context_are_denied(engine: Engine) -> None:
    scenario = _scenario(engine)
    reference = _grant(
        engine, scenario, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    value = _business_input(scenario, reference)
    with Session(engine) as session, pytest.raises(AuthorizationDenied) as denied:
        record_authorized_business_input(
            session, business_input=value, identity=scenario.identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert denied.value.code == "no_effective_authorization"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_authorized_business_inputs
        ).where(
            rfp_authorized_business_inputs.c.approval_run_id
            == scenario.approval_run_id
        )).scalar_one() == 0
    production_principal = AuthenticatedPrincipal(
        issuer="https://identity.trackflow.example/oidc",
        subject="production-ana-whitfield",
        trackflow_user_id=scenario.identity.trackflow_user_id,
        auth_source="oidc",
        general_role=scenario.identity.general_role,
        synthetic=False,
    )
    production_actor = AuthorizedHumanActor(
        principal=production_principal,
        authorization_id=reference.authorization_id,
        official_responsible="Ana Whitfield",
        permission="provide_business_input",
        department_scope=DepartmentId.warehouse,
    )
    production_value = _business_input(
        scenario, reference, actor=production_actor
    )
    production_identity = ResolvedIdentity._from_persisted_binding(
        environment="production", auth_source=production_principal.auth_source,
        issuer=production_principal.issuer, subject=production_principal.subject,
        trackflow_user_id=production_principal.trackflow_user_id,
        general_role=production_principal.general_role,
        principal_id=scenario.identity.principal_id,
        binding_id=scenario.identity.binding_id,
        responsible_code="ana_whitfield", synthetic=False,
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        record_authorized_business_input(
            session, business_input=production_value, identity=production_identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert raised.value.code == "approval_run_not_found_or_environment_mismatch"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_authorized_business_inputs
        ).where(
            rfp_authorized_business_inputs.c.approval_run_id
            == scenario.approval_run_id
        )).scalar_one() == 0


def test_postgres_audit_projection_and_replay_are_nonduplicating(engine: Engine) -> None:
    scenario = _scenario(engine)
    value, identity, reference, key, _result = _record_input(engine, scenario, "currency_conversion")
    with Session(engine) as session, session.begin():
        record_authorized_business_input(
            session, business_input=value, identity=identity,
            authorization_reference=reference, idempotency_key=key,
            execution_id=uuid4(),
        )
    with Session(engine) as session:
        events = session.execute(select(rfp_workflow_events).where(
            rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
            rfp_workflow_events.c.event_type == "authorized_business_input_recorded",
        )).mappings().all()
        assert len(events) == 1
        projected = repr((events[0]["safe_input_ref"], events[0]["safe_output_ref"]))
        assert value.payload_hash in projected
        assert "conversion_rate" not in projected and "1.08" not in projected


def test_postgres_rejected_command_leaves_no_partial_revision_or_event(engine: Engine) -> None:
    scenario = _scenario(engine)
    with Session(engine) as session, pytest.raises(ApprovalStoreError):
        create_corrected_department_revision(
            session, revision=scenario.revision, expected_predecessor_status="approved",
            expected_predecessor_content_hash=scenario.content_hash,
            expected_predecessor_state_version=0, identity=scenario.identity,
            authorization_reference=scenario.approval_reference,
            idempotency_key=uuid4(), execution_id=uuid4(),
        )
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_approval_department_revisions
        ).where(rfp_approval_department_revisions.c.approval_run_id == scenario.approval_run_id)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(rfp_workflow_events).where(
            rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
            rfp_workflow_events.c.event_type == "department_revision_corrected",
        )).scalar_one() == 0


def test_postgres_insert_failure_rolls_back_predecessor_cas_and_event(engine: Engine) -> None:
    scenario = _scenario(engine)
    _approve(engine, scenario.revision_id)
    value, _identity, reference, _key, _record = _record_input(engine, scenario)
    draft, bundle = _chain(engine, scenario, value, content="S2B rollback candidate")
    revision = _revision(scenario, scenario.revision, draft, bundle, reference, 2)
    revision = revision.model_copy(
        update={"revision_id": UUID(IDS["department_1a"])}
    )
    with Session(engine) as session, pytest.raises(DBAPIError):
        with session.begin():
            create_corrected_department_revision(
                session, revision=revision, expected_predecessor_status="approved",
                expected_predecessor_content_hash=scenario.content_hash,
                expected_predecessor_state_version=0, identity=scenario.identity,
                authorization_reference=reference, idempotency_key=uuid4(),
                execution_id=uuid4(),
            )
    with Session(engine) as session:
        predecessor = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == scenario.revision_id
        )).mappings().one()
        assert predecessor["is_current"] and predecessor["status"] == "approved"
        assert predecessor["superseded_by_revision_id"] is None
        assert session.execute(select(func.count()).select_from(rfp_workflow_events).where(
            rfp_workflow_events.c.department_revision_id == revision.revision_id,
            rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
        )).scalar_one() == 0


def test_postgres_initial_proposal_import_is_faithful_and_not_synthesized(engine: Engine) -> None:
    scenario = _scenario(engine)
    command, _identity, _reference, record = _create_initial(
        engine, scenario, mode="part2_import"
    )
    assert record.revision_no == 1 and record.eligible_for_synthesis
    with Session(engine) as session:
        row = session.execute(select(rfp_approval_proposal_revisions).where(
            rfp_approval_proposal_revisions.c.proposal_revision_id
            == command.proposal.revision_id
        )).mappings().one()
        assert row["validation_bundle_id"] == command.validation_bundle_id
        assert row["supersedes_revision_id"] is None
        assert session.execute(select(func.count()).select_from(rfp_workflow_events).where(
            rfp_workflow_events.c.proposal_revision_id == command.proposal.revision_id
        )).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(rfp_final_documents).where(
            rfp_final_documents.c.approval_run_id == scenario.approval_run_id
        )).scalar_one() == 0


def test_postgres_initial_proposal_accepts_miguel_authorized_change(engine: Engine) -> None:
    scenario = _scenario(engine)
    command, _identity, _reference, record = _create_initial(
        engine, scenario, mode="authorized_commercial_change"
    )
    assert record.revision_no == 1
    assert command.created_by is not None
    assert command.created_by.official_responsible == "Miguel Torres"
    with Session(engine) as session:
        row = session.execute(select(rfp_approval_proposal_revisions).where(
            rfp_approval_proposal_revisions.c.proposal_revision_id
            == command.proposal.revision_id
        )).mappings().one()
        assert row["content_payload"]["authorized_input_ids"] == [
            str(command.proposal.authorized_input_ids[0])
        ]


def test_postgres_part2_import_rejects_new_terms_without_partial_rows(engine: Engine) -> None:
    scenario = _scenario(engine)
    command, identity, reference = _prepare_initial_proposal(
        engine, scenario, mode="part2_import", changed_terms=True
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        create_initial_proposal_revision(
            session, command=command, identity=identity,
            authorization_reference=reference, execution_id=uuid4(),
        )
    assert raised.value.code == "part2_import_terms_changed"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_approval_proposal_revisions
        ).where(rfp_approval_proposal_revisions.c.approval_run_id == scenario.approval_run_id)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(rfp_workflow_events).where(
            rfp_workflow_events.c.approval_run_id == scenario.approval_run_id,
            rfp_workflow_events.c.event_type == "proposal_revision_initial_created",
        )).scalar_one() == 0


def test_postgres_two_initial_proposals_serialize_on_the_run(engine: Engine) -> None:
    scenario = _scenario(engine)
    first, identity, reference = _prepare_initial_proposal(
        engine, scenario, mode="part2_import"
    )
    second_proposal = first.proposal.model_copy(update={"revision_id": uuid4()})
    second = first.model_copy(update={
        "proposal": second_proposal,
        "idempotency_key": uuid4(),
        "requested_at": datetime.now(timezone.utc),
    })
    ready, release = Event(), Event()
    loser_name = f"s2b-initial-loser-{uuid4()}"

    def attempt(command: InitialProposalRevisionCommand, hold: bool, name=None):
        try:
            with Session(engine) as session, session.begin():
                if name:
                    session.execute(text("SET LOCAL application_name=:name"), {"name": name})
                result = create_initial_proposal_revision(
                    session, command=command, identity=identity,
                    authorization_reference=reference, execution_id=uuid4(),
                )
                if hold:
                    ready.set()
                    assert release.wait(10)
                return result.revision_id
        except ApprovalStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(attempt, first, True)
        assert ready.wait(10)
        loser = pool.submit(attempt, second, False, loser_name)
        _wait_for_lock(engine, loser_name)
        release.set()
        assert winner.result(timeout=10) == first.proposal.revision_id
        assert loser.result(timeout=10) == "initial_proposal_already_exists"


def test_postgres_global_a_to_b_accepts_real_blocked_bundle_without_synthesis(
    engine: Engine,
) -> None:
    scenario = _scenario(engine)
    initial, _old_identity, _old_reference, _record = _create_initial(
        engine, scenario, mode="part2_import"
    )
    revision, identity, reference = _prepare_corrected_global(
        engine, scenario, initial, blocked=True
    )
    with Session(engine) as session, session.begin():
        result = create_corrected_proposal_revision(
            session, revision=revision,
            expected_predecessor_content_hash=initial.proposal.content_hash,
            expected_predecessor_state_version=0, identity=identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert result.revision_no == 2
    assert result.eligible_for_synthesis is False
    with Session(engine) as session:
        rows = session.execute(select(rfp_approval_proposal_revisions).where(
            rfp_approval_proposal_revisions.c.proposal_revision_id.in_(
                [initial.proposal.revision_id, revision.revision_id]
            )
        )).mappings().all()
        by_id = {row["proposal_revision_id"]: row for row in rows}
        assert not by_id[initial.proposal.revision_id]["is_current"]
        assert by_id[revision.revision_id]["is_current"]
        assert by_id[revision.revision_id]["validation_bundle_id"] == revision.validation_bundle_id
        assert by_id[revision.revision_id]["eligible_for_synthesis"] is False
        assert by_id[revision.revision_id]["validation_payload"]["blocking_rule_ids"] == [
            "TF-GLOBAL-COMPLIANCE"
        ]


def test_postgres_global_revision_rejects_source_superseded_after_bundle(engine: Engine) -> None:
    scenario = _scenario(engine)
    initial, _old_identity, _old_reference, _record = _create_initial(
        engine, scenario, mode="part2_import"
    )
    revision, identity, reference = _prepare_corrected_global(
        engine, scenario, initial
    )
    _correct(engine, scenario)
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        create_corrected_proposal_revision(
            session, revision=revision,
            expected_predecessor_content_hash=initial.proposal.content_hash,
            expected_predecessor_state_version=0, identity=identity,
            authorization_reference=reference, idempotency_key=uuid4(),
            execution_id=uuid4(),
        )
    assert raised.value.code == "source_department_revision_not_eligible"
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(
            rfp_approval_proposal_revisions
        ).where(rfp_approval_proposal_revisions.c.approval_run_id == scenario.approval_run_id)).scalar_one() == 1


def test_postgres_global_rechecks_source_after_concurrent_supersession(engine: Engine) -> None:
    scenario = _scenario(engine)
    initial, _old_identity, _old_reference, _record = _create_initial(
        engine, scenario, mode="part2_import"
    )
    global_revision, global_identity, global_reference = _prepare_corrected_global(
        engine, scenario, initial
    )
    value, _identity, department_reference, _key, _input_record = _record_input(
        engine, scenario
    )
    draft, bundle = _chain(
        engine, scenario, value, content="S2B concurrent department successor"
    )
    department_revision = _revision(
        scenario, scenario.revision, draft, bundle, department_reference, 2
    )
    ready, release = Event(), Event()
    global_name = f"s2b-global-waiter-{uuid4()}"

    def supersede():
        with Session(engine) as session, session.begin():
            result = create_corrected_department_revision(
                session, revision=department_revision,
                expected_predecessor_status="approved",
                expected_predecessor_content_hash=scenario.content_hash,
                expected_predecessor_state_version=0, identity=scenario.identity,
                authorization_reference=department_reference,
                idempotency_key=uuid4(), execution_id=uuid4(),
            )
            ready.set()
            assert release.wait(10)
            return result.revision_id

    def global_worker():
        assert ready.wait(10)
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET LOCAL application_name=:name"), {"name": global_name})
                return create_corrected_proposal_revision(
                    session, revision=global_revision,
                    expected_predecessor_content_hash=initial.proposal.content_hash,
                    expected_predecessor_state_version=0, identity=global_identity,
                    authorization_reference=global_reference,
                    idempotency_key=uuid4(), execution_id=uuid4(),
                ).revision_id
        except ApprovalStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        department = pool.submit(supersede)
        assert ready.wait(10)
        global_future = pool.submit(global_worker)
        _wait_for_lock(engine, global_name)
        release.set()
        assert department.result(timeout=10) == department_revision.revision_id
        assert global_future.result(timeout=10) == "source_department_revision_not_eligible"
