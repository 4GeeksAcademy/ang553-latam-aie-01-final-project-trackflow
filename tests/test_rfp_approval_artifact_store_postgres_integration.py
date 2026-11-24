"""Real PostgreSQL integration tests for the S2A artifact store.

Every scenario owns a confirmed schema and is skipped unless all S2A-specific
guardrails identify one newly authorized disposable database.  Synthetic
producer contexts exercise the internal boundary; they are not workload
authentication evidence.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

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
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_validation_artifacts,
    rfp_approval_validation_bundles,
    rfp_workflow_events,
)
from services.rfp_approval.store import ApprovalStoreError
from tests.test_rfp_approval_postgres_migration import (
    H,
    IDS,
    _seed_part1,
    _seed_part2,
    _seed_part3_roots,
)


ROOT = Path(__file__).resolve().parents[1]
URL = os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_TEST_DATABASE_URL")
AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_DDL_DML_AUTHORIZED") == "YES"
)
EXPECTED_DATABASE = os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_EXPECTED_DATABASE")
EXPECTED_USER = os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_EXPECTED_USER")
EXPECTED_PORT = os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_EXPECTED_PORT")
EXPECTED_ENVIRONMENT = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_ARTIFACT_EXPECTED_ENVIRONMENT"
)
VERIFIED_CONTAINER_ID = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_ARTIFACT_VERIFIED_CONTAINER_ID"
)
CONTAINER_VERIFIED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_CONTAINER_VERIFIED") == "YES"
)
SCHEMA_ISOLATION_AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_ARTIFACT_SCHEMA_ISOLATION_AUTHORIZED")
    == "YES"
)

SCHEMA_PREFIX = "gate2b3b1_s2a_test_"
HISTORICAL_MIGRATIONS_AND_SEEDS = (
    (ROOT / "infra/sql/rfp_intake.sql", _seed_part1),
    (ROOT / "infra/sql/rfp_response_generation.sql", _seed_part2),
    (ROOT / "infra/sql/rfp_approval.sql", _seed_part3_roots),
)
EXTENSION_MIGRATION = ROOT / "infra/sql/rfp_approval_extension.sql"
MIGRATIONS = tuple(path for path, _seed in HISTORICAL_MIGRATIONS_AND_SEEDS) + (
    EXTENSION_MIGRATION,
)
SKIP_REASON = "requires an explicitly authorized isolated S2A PostgreSQL database"
RUN_ID = UUID(IDS["approval_1"])
TICKET_ID = UUID(IDS["ticket_1"])
RESPONSE_RUN_ID = UUID(IDS["response_1"])
EXECUTION_ID = UUID("52000000-0000-0000-0000-000000000001")


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
        )
    ):
        return SKIP_REASON
    parsed = make_url(database_url or "")
    if not parsed.drivername.startswith("postgresql"):
        return "S2A URL is not PostgreSQL"
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        return "S2A PostgreSQL host is not loopback-isolated"
    try:
        expected_port_number = int(expected_port or "")
    except ValueError:
        return "S2A PostgreSQL port is invalid"
    if (
        parsed.database != expected_database
        or parsed.username != expected_user
        or parsed.port != expected_port_number
    ):
        return "S2A PostgreSQL target does not match its expected identity"
    if not (expected_database or "").startswith(
        "trackflow_rfp_approval_artifact_test_s2a"
    ):
        return "S2A database does not use the disposable prefix"
    if not (expected_user or "").startswith("trackflow_gate2b3b1_s2a"):
        return "S2A user does not use the gate prefix"
    if expected_environment != "qa":
        return "S2A environment is not qa"
    if not re.fullmatch(r"[a-f0-9]{64}", verified_container_id or ""):
        return "S2A container identity is not verified"
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
    )


def _quoted_schema(schema_name: str) -> str:
    assert re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema_name)
    return f'"{schema_name}"'


def _schema_exists(admin: Engine, schema_name: str) -> bool:
    with admin.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace "
                    "WHERE nspname=:schema_name)"
                ),
                {"schema_name": schema_name},
            ).scalar_one()
        )


def _create_schema(admin: Engine, schema_name: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {_quoted_schema(schema_name)}")


def _run_migration(admin: Engine, schema_name: str, path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 0
    with admin.connect().execution_options(
        isolation_level="AUTOCOMMIT",
        no_parameters=True,
    ) as connection:
        connection.exec_driver_sql(
            f"SET search_path TO {_quoted_schema(schema_name)}"
        )
        assert (
            connection.exec_driver_sql("SELECT current_schema()").scalar_one()
            == schema_name
        )
        connection.exec_driver_sql(path.read_text(encoding="utf-8"))
        assert (
            connection.exec_driver_sql("SELECT current_schema()").scalar_one()
            == schema_name
        )


def _drop_schema(admin: Engine, schema_name: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql(
            f"DROP SCHEMA {_quoted_schema(schema_name)} CASCADE"
        )
    if _schema_exists(admin, schema_name):
        raise RuntimeError(f"S2A schema cleanup failed: {schema_name}")


def _seed_schema(
    engine: Engine,
    schema_name: str,
    seed: Callable[[object], None],
) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert (
            connection.execute(text("SELECT current_schema()")).scalar_one()
            == schema_name
        )
        dbapi_connection = connection.connection.driver_connection
        assert dbapi_connection.autocommit is True
        seed(dbapi_connection)
        assert (
            connection.execute(text("SELECT current_schema()")).scalar_one()
            == schema_name
        )

    # SQLAlchemy must restore the engine's default isolation before returning
    # the DBAPI connection to the pool.
    with engine.connect() as connection:
        assert connection.connection.driver_connection.autocommit is False


@contextmanager
def _isolated_schema(
    admin: Engine,
    scenario: Engine,
    schema_name: str,
) -> Iterator[None]:
    confirmed = False
    try:
        _create_schema(admin, schema_name)
        if not _schema_exists(admin, schema_name):
            raise RuntimeError(f"S2A schema creation was not confirmed: {schema_name}")
        confirmed = True
        for migration, seed in HISTORICAL_MIGRATIONS_AND_SEEDS:
            _run_migration(admin, schema_name, migration)
            _seed_schema(scenario, schema_name, seed)
        _run_migration(admin, schema_name, EXTENSION_MIGRATION)
        yield
    except Exception as primary:
        if confirmed:
            try:
                _drop_schema(admin, schema_name)
            except Exception as cleanup:
                raise ExceptionGroup(
                    "S2A scenario and targeted cleanup both failed",
                    [primary, cleanup],
                ) from None
        raise
    else:
        if confirmed:
            _drop_schema(admin, schema_name)


@pytest.fixture
def engine() -> Iterator[Engine]:
    reason = _authorization_reason()
    if reason is not None:
        pytest.skip(reason)
    assert URL is not None
    admin = create_engine(URL, pool_pre_ping=True)
    with admin.connect() as connection:
        database, user, table_count, schema_count = connection.execute(
            text(
                "SELECT current_database(),current_user,"
                "(SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
                "('pg_catalog','information_schema')),"
                "(SELECT count(*) FROM pg_namespace WHERE nspname LIKE "
                f"'{SCHEMA_PREFIX}%')"
            )
        ).one()
        assert database == EXPECTED_DATABASE
        assert user == EXPECTED_USER
        assert table_count == 0
        assert schema_count == 0
    schema_name = f"{SCHEMA_PREFIX}{uuid4().hex}"
    scenario = create_engine(
        URL,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema_name}"},
    )
    try:
        with _isolated_schema(admin, scenario, schema_name):
            try:
                with scenario.connect() as connection:
                    assert (
                        connection.execute(
                            text("SELECT current_schema()")
                        ).scalar_one()
                        == schema_name
                    )
                yield scenario
            finally:
                scenario.dispose()
    finally:
        admin.dispose()


def _producer(*, environment: str = "qa", execution_id: UUID = EXECUTION_ID):
    return ArtifactProducerContext._from_verified_orchestrator(
        environment=environment,
        producer_node="gate2b3b1_s2a_artifact_producer",
        execution_id=execution_id,
    )


def _provenance(kind: str, pairs=(), *, execution_id: UUID = EXECUTION_ID):
    data = {
        "producer_node": "gate2b3b1_s2a_artifact_producer",
        "execution_id": execution_id,
        "source_kind": kind,
        "source_artifact_ids": [item[0] for item in pairs],
        "source_hashes": [item[1] for item in pairs],
        "produced_at": datetime.now(timezone.utc),
    }
    return ApprovalArtifactProvenance(**data, provenance_hash=canonical_hash(data))


def _draft(
    *,
    artifact_id: UUID | None = None,
    content: str = "Corrected warehouse evidence for S2A.",
    scope: str = "warehouse",
    run_id: UUID = RUN_ID,
    ticket_id: UUID = TICKET_ID,
) -> CorrectedDraftArtifact:
    payload = DepartmentCorrectedDraftPayload(
        department=DepartmentId(scope),
        content=content,
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
    provenance = _provenance("human_correction", [(uuid4(), canonical_hash(content))])
    return CorrectedDraftArtifact(
        draft_artifact_id=artifact_id or uuid4(),
        approval_run_id=run_id,
        ticket_id=ticket_id,
        scope_key=scope,
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=provenance,
        created_at=provenance.produced_at,
    )


def _artifact(
    draft: CorrectedDraftArtifact,
    *,
    artifact_type: str = "evaluation",
    artifact_id: UUID | None = None,
    gate_status: str = "passed",
    eligible: bool = True,
    blocker: str | None = None,
) -> ApprovalValidationArtifact:
    if artifact_type == "safe_preview":
        rendered = "Safe S2A preview" if gate_status == "passed" else None
        payload = SafePreviewArtifactPayload(
            gate_status=gate_status,
            rendered_content=rendered,
            rendered_content_hash=(
                canonical_hash({"rendered_content": rendered}) if rendered else None
            ),
            reason_codes=[] if gate_status == "passed" else ["PREVIEW_NOT_PASSED"],
        )
        source_kind = "compliance_evaluator"
    else:
        payload = ContentEvaluationArtifactPayload(
            mandatory_compliance_passed=blocker is None,
            blocks_commercial_completeness=False,
            blocking_rule_ids=[] if blocker is None else [blocker],
            unresolved_input_codes=[],
            open_conflict_ids=[],
            eligible=eligible,
        )
        source_kind = "deterministic_evaluator"
    provenance = _provenance(
        source_kind, [(draft.draft_artifact_id, draft.content_hash)]
    )
    return ApprovalValidationArtifact(
        artifact_id=artifact_id or uuid4(),
        artifact_type=artifact_type,
        approval_run_id=draft.approval_run_id,
        ticket_id=draft.ticket_id,
        scope_key=draft.scope_key,
        corrected_draft_id=draft.draft_artifact_id,
        evaluated_content_hash=draft.content_hash,
        payload=payload,
        payload_hash=canonical_hash(payload),
        provenance=provenance,
        created_at=provenance.produced_at,
    )


def _bundle(
    draft: CorrectedDraftArtifact,
    evaluation: ApprovalValidationArtifact,
    preview: ApprovalValidationArtifact,
    *,
    bundle_id: UUID | None = None,
    summary_override: ContentEvaluationArtifactPayload | None = None,
    source_departments_approved: bool | None = None,
) -> ApprovalValidationBundle:
    summary = summary_override or evaluation.payload
    assert isinstance(summary, ContentEvaluationArtifactPayload)
    global_scope = draft.scope_key == "proposal"
    approved = (
        global_scope
        if source_departments_approved is None
        else source_departments_approved
    )
    provenance = _provenance(
        "compliance_evaluator",
        [
            (draft.draft_artifact_id, draft.content_hash),
            (evaluation.artifact_id, evaluation.payload_hash),
            (preview.artifact_id, preview.payload_hash),
        ],
    )
    validated_at = provenance.produced_at
    evaluation_ref = ImmutableArtifactReference(
        artifact_type=evaluation.artifact_type,
        artifact_id=evaluation.artifact_id,
        contract_version=evaluation.contract_version,
        payload_hash=evaluation.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=preview.artifact_id,
        contract_version=preview.contract_version,
        payload_hash=preview.payload_hash,
        evaluated_content_hash=draft.content_hash,
    )
    if global_scope:
        validation = {
            "revision_content_hash": draft.content_hash,
            "global_evaluation": evaluation_ref,
            "safe_preview": preview_ref,
            "mandatory_compliance_passed": summary.mandatory_compliance_passed,
            "unresolved_input_codes": summary.unresolved_input_codes,
            "blocking_rule_ids": summary.blocking_rule_ids,
            "open_conflict_ids": summary.open_conflict_ids,
            "source_departments_approved": approved,
            "eligible_for_synthesis": summary.eligible and approved,
            "validated_at": validated_at,
        }
    else:
        validation = {
            "revision_content_hash": draft.content_hash,
            "evaluation": evaluation_ref,
            "safe_preview": preview_ref,
            "validated_at": validated_at,
        }
    data = {
        "approval_run_id": draft.approval_run_id,
        "ticket_id": draft.ticket_id,
        "scope_key": draft.scope_key,
        "corrected_draft_id": draft.draft_artifact_id,
        "content_hash": draft.content_hash,
        "evaluation_artifact_id": evaluation.artifact_id,
        "evaluation_artifact_type": evaluation.artifact_type,
        "evaluation_contract_version": evaluation.contract_version,
        "evaluation_payload_hash": evaluation.payload_hash,
        "safe_preview_artifact_id": preview.artifact_id,
        "safe_preview_contract_version": preview.contract_version,
        "safe_preview_payload_hash": preview.payload_hash,
        "revision_validation_hash": canonical_hash(validation),
        "mandatory_compliance_passed": summary.mandatory_compliance_passed,
        "blocks_commercial_completeness": summary.blocks_commercial_completeness,
        "blocking_rule_ids": summary.blocking_rule_ids,
        "unresolved_input_codes": summary.unresolved_input_codes,
        "open_conflict_ids": summary.open_conflict_ids,
        "source_departments_approved": approved,
        "eligible": summary.eligible and (not global_scope or approved),
        "provenance": provenance,
        "validated_at": validated_at,
    }
    return ApprovalValidationBundle(
        validation_bundle_id=bundle_id or uuid4(),
        **data,
        bundle_hash=canonical_hash(data),
    )


def _revision_validation_payload(bundle: ApprovalValidationBundle) -> dict:
    evaluation_ref = ImmutableArtifactReference(
        artifact_type=bundle.evaluation_artifact_type,
        artifact_id=bundle.evaluation_artifact_id,
        contract_version=bundle.evaluation_contract_version,
        payload_hash=bundle.evaluation_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview",
        artifact_id=bundle.safe_preview_artifact_id,
        contract_version=bundle.safe_preview_contract_version,
        payload_hash=bundle.safe_preview_payload_hash,
        evaluated_content_hash=bundle.content_hash,
    )
    if bundle.scope_key == "proposal":
        payload = {
            "revision_content_hash": bundle.content_hash,
            "global_evaluation": evaluation_ref,
            "safe_preview": preview_ref,
            "mandatory_compliance_passed": bundle.mandatory_compliance_passed,
            "unresolved_input_codes": bundle.unresolved_input_codes,
            "blocking_rule_ids": bundle.blocking_rule_ids,
            "open_conflict_ids": bundle.open_conflict_ids,
            "source_departments_approved": bundle.source_departments_approved,
            "eligible_for_synthesis": bundle.eligible,
            "validated_at": bundle.validated_at,
        }
    else:
        payload = {
            "revision_content_hash": bundle.content_hash,
            "evaluation": evaluation_ref,
            "safe_preview": preview_ref,
            "validated_at": bundle.validated_at,
        }
    assert canonical_hash(payload) == bundle.revision_validation_hash
    return canonical_value(payload)


def _persist_chain(engine: Engine, *, evaluation_eligible: bool = True, blocker=None):
    draft = _draft()
    evaluation = _artifact(
        draft,
        eligible=evaluation_eligible,
        blocker=blocker,
    )
    preview = _artifact(draft, artifact_type="safe_preview")
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
    return draft, evaluation, preview


def _row_count(engine: Engine, table, condition=None) -> int:
    with Session(engine) as session:
        statement = select(table)
        if condition is not None:
            statement = statement.where(condition)
        return len(session.execute(statement).all())


def _assert_pgcode(error: pytest.ExceptionInfo[DBAPIError], expected: str) -> None:
    assert getattr(error.value.orig, "pgcode", None) == expected


def test_guardrail_requires_every_exact_s2a_control() -> None:
    valid = (
        "postgresql+psycopg2://trackflow_gate2b3b1_s2a:test@127.0.0.1:65474/"
        "trackflow_rfp_approval_artifact_test_s2a"
    )
    values = (
        valid,
        True,
        "trackflow_rfp_approval_artifact_test_s2a",
        "trackflow_gate2b3b1_s2a",
        "65474",
        "qa",
        "a" * 64,
        True,
        True,
    )
    assert _guardrail_reason(*values) is None
    assert "loopback" in (
        _guardrail_reason(
            *((valid.replace("127.0.0.1", "shared-db"),) + values[1:])
        )
        or ""
    )
    assert _guardrail_reason(*(values[:-1] + (False,))) == SKIP_REASON
    assert (
        tuple(path for path, _seed in HISTORICAL_MIGRATIONS_AND_SEEDS)
        == MIGRATIONS[:-1]
    )
    assert MIGRATIONS[-1] == EXTENSION_MIGRATION

    draft = _draft()
    evaluation = _artifact(draft)
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview)
    assert bundle.validated_at == bundle.provenance.produced_at
    assert (
        canonical_hash(_revision_validation_payload(bundle))
        == bundle.revision_validation_hash
    )
    collision_evaluation = _artifact(
        draft,
        eligible=False,
        blocker="S2A_OFFLINE_COLLISION_BLOCK",
    )
    collision = _bundle(
        draft,
        collision_evaluation,
        _artifact(draft, artifact_type="safe_preview"),
        bundle_id=bundle.validation_bundle_id,
    )
    assert collision.validation_bundle_id == bundle.validation_bundle_id
    assert collision.bundle_hash != bundle.bundle_hash

    class ScalarResult:
        def scalar_one(self):
            return "gate2b3b1_s2a_test_" + "a" * 32

    class MigrationConnection:
        def __init__(self):
            self.options = {}
            self.calls = []

        def execution_options(self, **options):
            self.options = options
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def exec_driver_sql(self, statement, *args, **kwargs):
            self.calls.append((statement, args, kwargs))
            return ScalarResult()

    class MigrationAdmin:
        def __init__(self, connection):
            self.connection = connection

        def connect(self):
            return self.connection

    schema_name = "gate2b3b1_s2a_test_" + "a" * 32
    migration_connection = MigrationConnection()
    _run_migration(
        MigrationAdmin(migration_connection),
        schema_name,
        EXTENSION_MIGRATION,
    )
    original_script = EXTENSION_MIGRATION.read_text(encoding="utf-8")
    assert "%" in original_script
    assert migration_connection.options == {
        "isolation_level": "AUTOCOMMIT",
        "no_parameters": True,
    }
    assert migration_connection.calls[2] == (original_script, (), {})
    assert all(not args and not kwargs for _, args, kwargs in migration_connection.calls)
    assert migration_connection.calls[0][0] == f'SET search_path TO "{schema_name}"'
    assert "public" not in migration_connection.calls[0][0]

    dispatched = []
    probe = create_engine("sqlite://")
    event.listen(
        probe.dialect,
        "do_execute_no_params",
        lambda _cursor, statement, _context: dispatched.append(
            ("no_parameters", statement)
        ),
    )
    event.listen(
        probe.dialect,
        "do_execute",
        lambda _cursor, statement, parameters, _context: dispatched.append(
            ("parameters", statement, parameters)
        ),
    )
    literal_percent_statement = "SELECT '%'"
    try:
        with probe.connect().execution_options(no_parameters=True) as connection:
            assert connection.exec_driver_sql(literal_percent_statement).scalar_one() == "%"
    finally:
        probe.dispose()
    assert dispatched == [("no_parameters", literal_percent_statement)]


def test_corrected_draft_insert_replay_collision_and_audit(engine: Engine) -> None:
    draft = _draft()
    with Session(engine) as session, session.begin():
        first = persist_corrected_draft(session, draft=draft, producer=_producer())
    with Session(engine) as session, session.begin():
        replay = persist_corrected_draft(session, draft=draft, producer=_producer())
    assert not first.replayed and replay.replayed
    collision = _draft(artifact_id=draft.draft_artifact_id, content="Divergent content")
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        with session.begin():
            persist_corrected_draft(
                session, draft=collision, producer=_producer()
            )
    assert raised.value.code == "corrected_draft_id_collision"
    assert _row_count(engine, rfp_approval_corrected_drafts) == 1
    assert _row_count(engine, rfp_workflow_events) == 1


def test_corrected_draft_rejects_run_environment_scope_and_provenance(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE rfp_approval_runs SET status='failed' WHERE approval_run_id=:run"), {"run": RUN_ID})
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(session, draft=_draft(), producer=_producer())
    assert raised.value.code == "approval_run_not_active"
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE rfp_approval_runs SET status='active' WHERE approval_run_id=:run"), {"run": RUN_ID})
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(
            session, draft=_draft(), producer=_producer(environment="production")
        )
    assert raised.value.code == "approval_run_not_found_or_environment_mismatch"
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(
            session, draft=_draft(scope="lastmile"), producer=_producer()
        )
    assert raised.value.code == "artifact_scope_not_active"
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_corrected_draft(
            session,
            draft=_draft(),
            producer=_producer(execution_id=uuid4()),
        )
    assert raised.value.code == "artifact_producer_provenance_mismatch"
    assert _row_count(engine, rfp_approval_corrected_drafts) == 0


def test_evaluation_preview_replay_collision_and_immutability(engine: Engine) -> None:
    draft, evaluation, preview = _persist_chain(engine)
    with Session(engine) as session, session.begin():
        assert persist_validation_artifact(
            session, artifact=evaluation, producer=_producer()
        ).replayed
        assert persist_validation_artifact(
            session, artifact=preview, producer=_producer()
        ).replayed
    collision = _artifact(
        draft,
        artifact_id=evaluation.artifact_id,
        eligible=False,
        blocker="DIFFERENT_EVALUATION",
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        with session.begin():
            persist_validation_artifact(
                session, artifact=collision, producer=_producer()
            )
    assert raised.value.code == "validation_artifact_id_collision"
    for statement in (
        text("UPDATE rfp_approval_validation_artifacts SET payload_hash=:hash WHERE artifact_id=:id"),
        text("DELETE FROM rfp_approval_validation_artifacts WHERE artifact_id=:id"),
    ):
        with Session(engine) as session, pytest.raises(DBAPIError) as error:
            with session.begin():
                session.execute(statement, {"hash": H["9"], "id": evaluation.artifact_id})
        _assert_pgcode(error, "P0001")
    assert _row_count(engine, rfp_approval_validation_artifacts) == 2


def test_artifact_rejects_other_draft_run_ticket_scope_hash_and_version(
    engine: Engine,
) -> None:
    draft = _draft()
    other = _draft(content=draft.payload.content, artifact_id=uuid4())
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_corrected_draft(session, draft=other, producer=_producer())
    artifact = _artifact(other)
    incompatible = artifact.model_copy(
        update={"corrected_draft_id": draft.draft_artifact_id}
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_artifact(
            session, artifact=incompatible, producer=_producer()
        )
    assert raised.value.code == "validation_artifact_provenance_draft_mismatch"
    for changed in (
        artifact.model_copy(update={"approval_run_id": uuid4()}),
        artifact.model_copy(update={"ticket_id": uuid4()}),
        artifact.model_copy(update={"scope_key": "reverse"}),
        artifact.model_copy(update={"payload_hash": H["9"]}),
        artifact.model_copy(update={"contract_version": "incompatible-v9"}),
    ):
        with Session(engine) as session, pytest.raises(ApprovalStoreError):
            persist_validation_artifact(
                session, artifact=changed, producer=_producer()
            )


def test_bundle_valid_noneligible_replay_collision_and_cross_draft(engine: Engine) -> None:
    draft, evaluation, preview = _persist_chain(engine)
    bundle = _bundle(draft, evaluation, preview)
    with Session(engine) as session, session.begin():
        first = persist_validation_bundle(
            session, bundle=bundle, producer=_producer()
        )
    with Session(engine) as session, session.begin():
        replay = persist_validation_bundle(
            session, bundle=bundle, producer=_producer()
        )
    assert first.eligible and not first.replayed and replay.replayed
    collision_evaluation = _artifact(
        draft,
        eligible=False,
        blocker="S2A_COLLISION_BLOCK",
    )
    collision_preview = _artifact(draft, artifact_type="safe_preview")
    with Session(engine) as session, session.begin():
        persist_validation_artifact(
            session,
            artifact=collision_evaluation,
            producer=_producer(),
        )
        persist_validation_artifact(
            session,
            artifact=collision_preview,
            producer=_producer(),
        )
    collision = _bundle(
        draft,
        collision_evaluation,
        collision_preview,
        bundle_id=bundle.validation_bundle_id,
    )
    assert collision.bundle_hash != bundle.bundle_hash
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=collision, producer=_producer())
    assert raised.value.code == "validation_bundle_id_collision"

    blocked_draft, blocked_evaluation, blocked_preview = _persist_chain(
        engine, evaluation_eligible=False, blocker="COMPLIANCE_BLOCK"
    )
    blocked_bundle = _bundle(blocked_draft, blocked_evaluation, blocked_preview)
    with Session(engine) as session, session.begin():
        record = persist_validation_bundle(
            session, bundle=blocked_bundle, producer=_producer()
        )
    assert not record.eligible

    other = _draft(content=draft.payload.content)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=other, producer=_producer())
        foreign_preview = _artifact(other, artifact_type="safe_preview")
        persist_validation_artifact(
            session, artifact=foreign_preview, producer=_producer()
        )
    foreign = _bundle(draft, evaluation, foreign_preview)
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=foreign, producer=_producer())
    assert raised.value.code == "validation_bundle_artifact_mismatch"


@pytest.mark.parametrize("gate_status", ["blocked", "indeterminate", "technical_error"])
def test_bundle_rejects_nonpassed_preview(engine: Engine, gate_status: str) -> None:
    draft = _draft()
    evaluation = _artifact(draft)
    preview = _artifact(draft, artifact_type="safe_preview", gate_status=gate_status)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
    bundle = _bundle(draft, evaluation, preview)
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert raised.value.code == "eligible_bundle_preview_not_passed"
    assert _row_count(engine, rfp_approval_validation_bundles) == 0


def test_bundle_rejects_summary_contradiction(engine: Engine) -> None:
    draft, evaluation, preview = _persist_chain(engine)
    contradictory = ContentEvaluationArtifactPayload(
        mandatory_compliance_passed=False,
        blocks_commercial_completeness=False,
        blocking_rule_ids=["CONTRADICTORY_SUMMARY"],
        unresolved_input_codes=[],
        open_conflict_ids=[],
        eligible=False,
    )
    bundle = _bundle(
        draft, evaluation, preview, summary_override=contradictory
    )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    assert raised.value.code == "validation_bundle_evaluation_summary_mismatch"


def _seed_global_departments(engine: Engine) -> dict[str, UUID]:
    revision_ids = {
        "warehouse": UUID(IDS["department_1a"]),
        "lastmile": uuid4(),
        "reverse": uuid4(),
    }
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "UPDATE rfp_approval_runs SET active_departments="
                "CAST(:departments AS jsonb) WHERE approval_run_id=:run"
            ),
            {
                "departments": '["warehouse","lastmile","reverse"]',
                "run": RUN_ID,
            },
        )
        session.execute(
            text(
                "UPDATE rfp_approval_department_revisions SET status='approved' "
                "WHERE revision_id=:revision"
            ),
            {"revision": revision_ids["warehouse"]},
        )
        for index, department in enumerate(("lastmile", "reverse"), start=1):
            source_draft, source_evaluation = uuid4(), uuid4()
            draft_hash = canonical_hash({"source": department, "kind": "draft"})
            evaluation_hash = canonical_hash(
                {"source": department, "kind": "evaluation"}
            )
            content_hash = canonical_hash({"department": department, "revision": 1})
            session.execute(
                text(
                    "INSERT INTO rfp_response_drafts("
                    "draft_id,run_id,scope_key,iteration_count,contract_version,"
                    "payload,payload_hash) VALUES ("
                    ":draft,:run,:scope,1,'rfp-department-draft-v1',"
                    "'{}'::jsonb,:hash)"
                ),
                {
                    "draft": source_draft,
                    "run": RESPONSE_RUN_ID,
                    "scope": department,
                    "hash": draft_hash,
                },
            )
            session.execute(
                text(
                    "INSERT INTO rfp_response_evaluations("
                    "evaluation_id,run_id,draft_id,scope_key,contract_version,"
                    "overall_pass,terminal_status,payload,payload_hash) VALUES ("
                    ":evaluation,:run,:draft,:scope,'section-evaluation-v1',"
                    "true,'passed','{}'::jsonb,:hash)"
                ),
                {
                    "evaluation": source_evaluation,
                    "run": RESPONSE_RUN_ID,
                    "draft": source_draft,
                    "scope": department,
                    "hash": evaluation_hash,
                },
            )
            session.execute(
                text(
                    "INSERT INTO rfp_approval_department_revisions("
                    "revision_id,approval_run_id,ticket_id,response_run_id,"
                    "department_id,review_revision_no,source_part2_draft_id,"
                    "source_part2_draft_hash,source_part2_evaluation_id,"
                    "source_part2_evaluation_hash,source_part2_iteration,"
                    "content_payload,content_hash,validation_payload,validation_hash,"
                    "evaluated_content_hash,status,created_by_principal_id,created_at) "
                    "VALUES (:revision,:approval,:ticket,:response,:department,1,"
                    ":draft,:draft_hash,:evaluation,:evaluation_hash,1,'{}'::jsonb,"
                    ":content_hash,'{}'::jsonb,:validation_hash,:content_hash,"
                    "'approved',:principal,clock_timestamp())"
                ),
                {
                    "revision": revision_ids[department],
                    "approval": RUN_ID,
                    "ticket": TICKET_ID,
                    "response": RESPONSE_RUN_ID,
                    "department": department,
                    "draft": source_draft,
                    "draft_hash": draft_hash,
                    "evaluation": source_evaluation,
                    "evaluation_hash": evaluation_hash,
                    "content_hash": content_hash,
                    "validation_hash": canonical_hash(
                        {"department": department, "validation": index}
                    ),
                    "principal": UUID(IDS["principal"]),
                },
            )
    return revision_ids


def _global_draft(source_ids: list[UUID]) -> CorrectedDraftArtifact:
    evidence = EvidenceReference(
        document_id="s2a-authorized-global-terms",
        section_id="global-terms",
        evidence_hash=H["1"],
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
        source_department_revision_ids=source_ids,
        authorized_input_ids=[],
    )
    content_hash = proposal_revision_content_hash(
        currency=payload.currency,
        sla=payload.sla,
        volume_discounts=payload.volume_discounts,
        source_department_revision_ids=payload.source_department_revision_ids,
        authorized_input_ids=payload.authorized_input_ids,
    )
    provenance = _provenance("human_correction", [(uuid4(), H["2"])])
    return CorrectedDraftArtifact(
        draft_artifact_id=uuid4(),
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        scope_key="proposal",
        payload=payload,
        content_hash=content_hash,
        payload_hash=canonical_hash(payload),
        provenance=provenance,
        created_at=provenance.produced_at,
    )


def _global_chain(engine: Engine, source_ids: list[UUID]):
    draft = _global_draft(source_ids)
    evaluation = _artifact(draft, artifact_type="global_evaluation")
    preview = _artifact(draft, artifact_type="safe_preview")
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
    return draft, evaluation, preview, _bundle(draft, evaluation, preview)


def _prepare_warehouse_successor(
    engine: Engine,
) -> tuple[CorrectedDraftArtifact, ApprovalValidationBundle]:
    draft = _draft(content="Corrected warehouse evidence for the successor revision.")
    evaluation = _artifact(draft)
    preview = _artifact(draft, artifact_type="safe_preview")
    bundle = _bundle(draft, evaluation, preview)
    with Session(engine) as session, session.begin():
        persist_corrected_draft(session, draft=draft, producer=_producer())
        persist_validation_artifact(session, artifact=evaluation, producer=_producer())
        persist_validation_artifact(session, artifact=preview, producer=_producer())
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    return draft, bundle


def _supersede_warehouse(
    engine: Engine,
    old_id: UUID,
    evidence: tuple[CorrectedDraftArtifact, ApprovalValidationBundle] | None = None,
) -> UUID:
    draft, bundle = evidence or _prepare_warehouse_successor(engine)
    validation_payload = _revision_validation_payload(bundle)
    successor = uuid4()
    with Session(engine) as session, session.begin():
        session.execute(text("SET CONSTRAINTS fk_rfp_department_revision_successor DEFERRED"))
        session.execute(
            text(
                "UPDATE rfp_approval_department_revisions SET status='superseded',"
                "is_current=false,superseded_by_revision_id=:successor,"
                "state_version=state_version+1 WHERE revision_id=:old"
            ),
            {"successor": successor, "old": old_id},
        )
        session.execute(
            text(
                "INSERT INTO rfp_approval_department_revisions("
                "revision_id,approval_run_id,ticket_id,response_run_id,department_id,"
                "review_revision_no,source_part2_draft_id,source_part2_draft_hash,"
                "source_part2_evaluation_id,source_part2_evaluation_hash,"
                "source_part2_iteration,supersedes_revision_id,content_payload,"
                "content_hash,validation_payload,validation_hash,evaluated_content_hash,"
                "status,created_by_principal_id,validation_bundle_id,created_at) "
                "SELECT :successor,"
                "approval_run_id,ticket_id,response_run_id,department_id,2,"
                "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
                "source_part2_evaluation_hash,source_part2_iteration,revision_id,"
                "CAST(:content_payload AS jsonb),:content_hash,"
                "CAST(:validation_payload AS jsonb),:validation_hash,"
                ":content_hash,'approved',created_by_principal_id,:validation_bundle_id,"
                "clock_timestamp() "
                "FROM rfp_approval_department_revisions WHERE revision_id=:old"
            ),
            {
                "successor": successor,
                "old": old_id,
                "content_payload": json.dumps(
                    canonical_value(draft.payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "content_hash": bundle.content_hash,
                "validation_payload": json.dumps(
                    validation_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "validation_hash": bundle.revision_validation_hash,
                "validation_bundle_id": bundle.validation_bundle_id,
            },
        )
    return successor


def test_global_bundle_exact_current_sources_and_stale_source(engine: Engine) -> None:
    revisions = _seed_global_departments(engine)
    current_ids = [revisions[name] for name in ("warehouse", "lastmile", "reverse")]
    draft, evaluation, preview, bundle = _global_chain(engine, current_ids)
    with Session(engine) as session, session.begin():
        assert persist_validation_bundle(
            session, bundle=bundle, producer=_producer()
        ).eligible
    stale_draft, stale_evaluation, stale_preview, stale_bundle = _global_chain(
        engine, current_ids
    )
    _supersede_warehouse(engine, revisions["warehouse"])
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(
            session, bundle=stale_bundle, producer=_producer()
        )
    assert raised.value.code == "global_draft_source_revision_mismatch"
    assert _row_count(
        engine,
        rfp_approval_validation_bundles,
        rfp_approval_validation_bundles.c.validation_bundle_id
        == stale_bundle.validation_bundle_id,
    ) == 0


@pytest.mark.parametrize(
    "case",
    ["missing", "other_run", "same_department", "not_approved"],
)
def test_global_bundle_rejects_invalid_source_sets(engine: Engine, case: str) -> None:
    revisions = _seed_global_departments(engine)
    ids = [revisions[name] for name in ("warehouse", "lastmile", "reverse")]
    if case == "missing":
        ids = ids[:2]
    elif case == "other_run":
        ids[0] = UUID(IDS["department_2a"])
    elif case == "same_department":
        ids = [revisions["warehouse"], UUID(IDS["department_2a"]), revisions["lastmile"]]
    else:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "UPDATE rfp_approval_department_revisions "
                    "SET status='changes_requested' WHERE revision_id=:revision"
                ),
                {"revision": revisions["reverse"]},
            )
    draft, evaluation, preview, bundle = _global_chain(engine, ids)
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    expected = (
        "validation_bundle_department_state_mismatch"
        if case == "not_approved"
        else "global_draft_source_revision_scope_mismatch"
        if case == "missing"
        else "global_draft_source_revision_mismatch"
    )
    assert raised.value.code == expected


def _wait_for_lock(engine: Engine, application_name: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name=:name AND wait_event_type='Lock')"
                ),
                {"name": application_name},
            ).scalar_one()
        if waiting:
            return
        Event().wait(0.05)
    raise AssertionError(f"transaction {application_name} did not reach a lock wait")


@pytest.mark.parametrize("divergent", [False, True])
def test_concurrent_draft_insert_waits_and_resolves_winner(
    engine: Engine, divergent: bool
) -> None:
    draft = _draft()
    contender = (
        _draft(artifact_id=draft.draft_artifact_id, content="Divergent contender")
        if divergent
        else draft
    )
    winner_ready, release_winner = Event(), Event()
    loser_name = f"s2a-draft-loser-{uuid4()}"

    def winner():
        with Session(engine) as session, session.begin():
            result = persist_corrected_draft(
                session, draft=draft, producer=_producer()
            )
            winner_ready.set()
            assert release_winner.wait(10)
            return result.replayed

    def loser():
        assert winner_ready.wait(10)
        try:
            with Session(engine) as session, session.begin():
                session.execute(
                    text("SET LOCAL application_name=:name"),
                    {"name": loser_name},
                )
                return persist_corrected_draft(
                    session, draft=contender, producer=_producer()
                ).replayed
        except ApprovalStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        winning = pool.submit(winner)
        assert winner_ready.wait(10)
        losing = pool.submit(loser)
        _wait_for_lock(engine, loser_name)
        release_winner.set()
        assert winning.result(timeout=10) is False
        loser_result = losing.result(timeout=10)
    assert loser_result == (
        "corrected_draft_id_collision" if divergent else True
    )
    assert _row_count(engine, rfp_approval_corrected_drafts) == 1
    assert _row_count(engine, rfp_workflow_events) == 1


@pytest.mark.parametrize("change", ["status", "supersede"])
def test_global_bundle_rechecks_sources_after_waiting_for_draft_lock(
    engine: Engine, change: str
) -> None:
    revisions = _seed_global_departments(engine)
    ids = [revisions[name] for name in ("warehouse", "lastmile", "reverse")]
    draft, evaluation, preview, bundle = _global_chain(engine, ids)
    successor_evidence = (
        _prepare_warehouse_successor(engine) if change == "supersede" else None
    )
    blocker_ready, release_blocker = Event(), Event()
    worker_name = f"s2a-global-bundle-{uuid4()}"

    def blocker():
        with Session(engine) as session, session.begin():
            session.execute(
                select(rfp_approval_corrected_drafts)
                .where(
                    rfp_approval_corrected_drafts.c.draft_artifact_id
                    == draft.draft_artifact_id
                )
                .with_for_update()
            ).one()
            blocker_ready.set()
            assert release_blocker.wait(10)

    def bundle_worker():
        assert blocker_ready.wait(10)
        try:
            with Session(engine) as session, session.begin():
                session.execute(
                    text("SET LOCAL application_name=:name"),
                    {"name": worker_name},
                )
                persist_validation_bundle(
                    session, bundle=bundle, producer=_producer()
                )
        except ApprovalStoreError as error:
            return error.code
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        blocking = pool.submit(blocker)
        assert blocker_ready.wait(10)
        worker = pool.submit(bundle_worker)
        _wait_for_lock(engine, worker_name)
        if change == "status":
            with Session(engine) as session, session.begin():
                session.execute(
                    text(
                        "UPDATE rfp_approval_department_revisions "
                        "SET status='changes_requested' WHERE revision_id=:revision"
                    ),
                    {"revision": revisions["reverse"]},
                )
        else:
            _supersede_warehouse(
                engine,
                revisions["warehouse"],
                successor_evidence,
            )
        release_blocker.set()
        blocking.result(timeout=10)
        result = worker.result(timeout=10)
    assert result == (
        "validation_bundle_department_state_mismatch"
        if change == "status"
        else "global_draft_source_revision_mismatch"
    )
    assert _row_count(
        engine,
        rfp_approval_validation_bundles,
        rfp_approval_validation_bundles.c.validation_bundle_id
        == bundle.validation_bundle_id,
    ) == 0
    if change == "supersede":
        assert successor_evidence is not None
        _warehouse_draft, warehouse_bundle = successor_evidence
        with Session(engine) as session:
            persisted_warehouse_bundle = session.execute(
                select(
                    rfp_approval_validation_bundles.c.validation_bundle_id,
                    rfp_approval_validation_bundles.c.approval_run_id,
                    rfp_approval_validation_bundles.c.ticket_id,
                    rfp_approval_validation_bundles.c.scope_key,
                    rfp_approval_validation_bundles.c.corrected_draft_id,
                    rfp_approval_validation_bundles.c.content_hash,
                    rfp_approval_validation_bundles.c.evaluation_artifact_id,
                    rfp_approval_validation_bundles.c.evaluation_artifact_type,
                    rfp_approval_validation_bundles.c.evaluation_contract_version,
                    rfp_approval_validation_bundles.c.evaluation_payload_hash,
                    rfp_approval_validation_bundles.c.safe_preview_artifact_id,
                    rfp_approval_validation_bundles.c.safe_preview_contract_version,
                    rfp_approval_validation_bundles.c.safe_preview_payload_hash,
                    rfp_approval_validation_bundles.c.revision_validation_hash,
                    rfp_approval_validation_bundles.c.bundle_hash,
                ).where(
                    rfp_approval_validation_bundles.c.validation_bundle_id
                    == warehouse_bundle.validation_bundle_id
                )
            ).one()
        assert persisted_warehouse_bundle == (
            warehouse_bundle.validation_bundle_id,
            warehouse_bundle.approval_run_id,
            warehouse_bundle.ticket_id,
            warehouse_bundle.scope_key,
            warehouse_bundle.corrected_draft_id,
            warehouse_bundle.content_hash,
            warehouse_bundle.evaluation_artifact_id,
            warehouse_bundle.evaluation_artifact_type,
            warehouse_bundle.evaluation_contract_version,
            warehouse_bundle.evaluation_payload_hash,
            warehouse_bundle.safe_preview_artifact_id,
            warehouse_bundle.safe_preview_contract_version,
            warehouse_bundle.safe_preview_payload_hash,
            warehouse_bundle.revision_validation_hash,
            warehouse_bundle.bundle_hash,
        )


def _audit_call(
    session: Session,
    *,
    event_key: str,
    execution_id: UUID,
    safe_input: dict,
    safe_output: dict,
    event_type: str = "s2a_concurrent_audit",
) -> None:
    _audit(
        session,
        event_key=event_key,
        run_id=RUN_ID,
        ticket_id=TICKET_ID,
        thread_id=event_key,
        producer=_producer(execution_id=execution_id),
        node_name="s2a_audit_test",
        event_type=event_type,
        safe_input=safe_input,
        safe_output=safe_output,
    )


def test_incompatible_audit_event_rolls_back_artifact(engine: Engine) -> None:
    draft = _draft()
    event_key = f"artifact:draft:{draft.draft_artifact_id}"
    with Session(engine) as session, session.begin():
        session.execute(
            insert(rfp_workflow_events).values(
                event_id=uuid4(),
                event_key=event_key,
                approval_run_id=RUN_ID,
                ticket_id=TICKET_ID,
                thread_id=event_key,
                execution_id=EXECUTION_ID,
                sequence_no=1,
                node_name="incompatible_node",
                agent_name="incompatible_agent",
                event_type="incompatible_event",
                safe_input_ref={"artifact_id": str(uuid4())},
                safe_output_ref={"result_id": str(uuid4())},
                result="persisted",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
    with Session(engine) as session, pytest.raises(ApprovalStoreError) as raised:
        with session.begin():
            persist_corrected_draft(session, draft=draft, producer=_producer())
    assert raised.value.code == "artifact_audit_event_collision"
    assert _row_count(engine, rfp_approval_corrected_drafts) == 0
    assert _row_count(engine, rfp_workflow_events) == 1


def test_concurrent_audit_collision_waits_and_rejects_difference(
    engine: Engine,
) -> None:
    event_key = f"artifact:audit-concurrency:{uuid4()}"
    first_execution, second_execution = uuid4(), uuid4()
    safe_input = {"artifact_id": uuid4(), "content_hash": H["1"]}
    safe_output = {"result_id": uuid4()}
    winner_ready, release_winner = Event(), Event()
    loser_name = f"s2a-audit-loser-{uuid4()}"

    def winner():
        with Session(engine) as session, session.begin():
            _audit_call(
                session,
                event_key=event_key,
                execution_id=first_execution,
                safe_input=safe_input,
                safe_output=safe_output,
            )
            winner_ready.set()
            assert release_winner.wait(10)

    def loser():
        assert winner_ready.wait(10)
        try:
            with Session(engine) as session, session.begin():
                session.execute(
                    text("SET LOCAL application_name=:name"),
                    {"name": loser_name},
                )
                _audit_call(
                    session,
                    event_key=event_key,
                    execution_id=second_execution,
                    safe_input=safe_input,
                    safe_output=safe_output,
                )
        except ApprovalStoreError as error:
            return error.code
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(winner)
        assert winner_ready.wait(10)
        second = pool.submit(loser)
        _wait_for_lock(engine, loser_name)
        release_winner.set()
        first.result(timeout=10)
        assert second.result(timeout=10) == "artifact_audit_event_collision"
    assert _row_count(engine, rfp_workflow_events) == 1


def test_sql_integrity_fk_immutability_and_safe_audit(engine: Engine) -> None:
    draft, evaluation, preview = _persist_chain(engine)
    bundle = _bundle(draft, evaluation, preview)
    with Session(engine) as session, session.begin():
        persist_validation_bundle(session, bundle=bundle, producer=_producer())
    targets = (
        ("rfp_approval_corrected_drafts", "draft_artifact_id", draft.draft_artifact_id),
        ("rfp_approval_validation_artifacts", "artifact_id", evaluation.artifact_id),
        ("rfp_approval_validation_bundles", "validation_bundle_id", bundle.validation_bundle_id),
    )
    for table_name, key, value in targets:
        for statement in (
            text(f"UPDATE {table_name} SET created_at=clock_timestamp() WHERE {key}=:id"),
            text(f"DELETE FROM {table_name} WHERE {key}=:id"),
        ):
            with Session(engine) as session, pytest.raises(DBAPIError) as error:
                with session.begin():
                    session.execute(statement, {"id": value})
            _assert_pgcode(error, "P0001")

    cross_id = uuid4()
    with Session(engine) as session, pytest.raises(DBAPIError) as error:
        with session.begin():
            session.execute(
                text(
                    "INSERT INTO rfp_approval_validation_artifacts("
                    "artifact_id,artifact_type,approval_run_id,ticket_id,scope_key,"
                    "corrected_draft_id,evaluated_content_hash,contract_version,payload,"
                    "payload_hash,provenance,provenance_hash,producer_node,execution_id,"
                    "produced_at,created_at) SELECT :new_id,artifact_type,approval_run_id,"
                    "ticket_id,scope_key,:foreign_draft,evaluated_content_hash,"
                    "contract_version,payload,payload_hash,provenance,provenance_hash,"
                    "producer_node,execution_id,produced_at,created_at FROM "
                    "rfp_approval_validation_artifacts WHERE artifact_id=:source"
                ),
                {
                    "new_id": cross_id,
                    "foreign_draft": uuid4(),
                    "source": evaluation.artifact_id,
                },
            )
    _assert_pgcode(error, "23503")
    with Session(engine) as session:
        events = session.execute(
            select(
                rfp_workflow_events.c.safe_input_ref,
                rfp_workflow_events.c.safe_output_ref,
            )
        ).all()
    serialized = str(events).lower()
    assert draft.payload.content.lower() not in serialized
    for forbidden in ("password", "access_token", "tariff", "price", "rate"):
        assert forbidden not in serialized
    assert _row_count(
        engine,
        rfp_approval_validation_artifacts,
        rfp_approval_validation_artifacts.c.artifact_id == cross_id,
    ) == 0
