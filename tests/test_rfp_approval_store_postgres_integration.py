"""Real PostgreSQL concurrency tests; skipped without a new Gate 2B.3A database."""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Iterator, Sequence
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import DepartmentHandoffResult, RFPResponseHandoff
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ApprovalRevisionValidation, ApprovalRunStart, AuthenticatedPrincipal,
    AuthorizedHumanActor, DecisionApplicationCommand, DepartmentRevision,
    HumanDecision, ImmutableArtifactReference, InitialRevisionReference,
    RevisionValidationReferences, RunInitiator, decision_application_key,
    department_revision_content_hash,
)
from services.rfp_approval.authorization import AuthorizationDenied, AuthorizationReference
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_department_revisions, rfp_approval_runs, rfp_authorizations,
    rfp_human_decisions, rfp_identity_bindings, rfp_principals,
)
from services.rfp_approval.store import (
    ApprovalStoreError, apply_claimed_decision, approval_start_request_hash, claim_next_decision,
    register_human_decision, start_approval_run,
)
import services.rfp_approval.store as store_module
from services.rfp_intake.models import rfp_tickets
from services.rfp_response.models import (
    rfp_response_drafts, rfp_response_evaluations, rfp_response_handoffs,
    rfp_response_runs,
)


_URL = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_TEST_DATABASE_URL")
_AUTHORIZED = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_DML_AUTHORIZED") == "YES"
_EXPECTED_DATABASE = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_EXPECTED_DATABASE")
_EXPECTED_USER = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_EXPECTED_USER")
_EXPECTED_PORT = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_EXPECTED_PORT")
_EXPECTED_ENVIRONMENT = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_EXPECTED_ENVIRONMENT")
_CONTAINER_ID = os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_VERIFIED_CONTAINER_ID")
_CONTAINER_VERIFIED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_CONTAINER_VERIFIED") == "YES"
)
_SCHEMA_ISOLATION_AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_STORE_SCHEMA_ISOLATION_AUTHORIZED") == "YES"
)
_GUARDRAILS_PRESENT = all(
    (
        _URL,
        _AUTHORIZED,
        _EXPECTED_DATABASE,
        _EXPECTED_USER,
        _EXPECTED_PORT,
        _EXPECTED_ENVIRONMENT,
        _CONTAINER_ID,
        _CONTAINER_VERIFIED,
        _SCHEMA_ISOLATION_AUTHORIZED,
    )
)
_POSTGRES_SKIP_REASON = (
    "requires a newly authorized isolated Gate 2B.3A PostgreSQL database; "
    "DML and per-test schema DDL must be authorized explicitly; prior Gate "
    "2B.1 authorization is not accepted"
)


_SCHEMA_PREFIX = "gate2b3a_test_"
_SCHEMA_SQL_PATHS = (
    Path(__file__).resolve().parents[1] / "infra" / "sql" / "rfp_intake.sql",
    Path(__file__).resolve().parents[1] / "infra" / "sql" / "rfp_response_generation.sql",
    Path(__file__).resolve().parents[1] / "infra" / "sql" / "rfp_approval.sql",
)


def _quoted_schema(schema_name: str) -> str:
    assert re.fullmatch(rf"{_SCHEMA_PREFIX}[a-f0-9]{{32}}", schema_name)
    return f'"{schema_name}"'


class SchemaIsolationError(RuntimeError):
    """Raised when a per-test schema cannot be verified or removed safely."""


def _schema_exists(admin_engine: Engine, schema_name: str) -> bool:
    with admin_engine.connect() as connection:
        return bool(connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_namespace WHERE nspname=:schema_name"
                ")"
            ),
            {"schema_name": schema_name},
        ).scalar_one())


def _create_schema(admin_engine: Engine, schema_name: str) -> None:
    quoted_schema = _quoted_schema(schema_name)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {quoted_schema}")


def _run_schema_migration(
    admin_engine: Engine, schema_name: str, migration_sql: str
) -> None:
    quoted_schema = _quoted_schema(schema_name)
    with admin_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.exec_driver_sql(f"SET search_path TO {quoted_schema}")
        active_schema = connection.exec_driver_sql(
            "SELECT current_schema()"
        ).scalar_one()
        if active_schema != schema_name:
            raise SchemaIsolationError(
                f"exclusive search_path did not select schema {schema_name}"
            )
        # Each historical script owns its explicit BEGIN/COMMIT block.
        connection.exec_driver_sql(migration_sql)


def _drop_schema(admin_engine: Engine, schema_name: str) -> None:
    quoted_schema = _quoted_schema(schema_name)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql(f"DROP SCHEMA {quoted_schema} CASCADE")
    if _schema_exists(admin_engine, schema_name):
        raise SchemaIsolationError(f"schema cleanup was not confirmed for {schema_name}")


@contextmanager
def _isolated_schema(
    admin_engine: Engine,
    schema_name: str,
    migration_scripts: Sequence[str],
) -> Iterator[None]:
    schema_confirmed = False
    try:
        _create_schema(admin_engine, schema_name)
        if not _schema_exists(admin_engine, schema_name):
            raise SchemaIsolationError(
                f"schema creation was not confirmed for {schema_name}"
            )
        schema_confirmed = True
        for migration_sql in migration_scripts:
            _run_schema_migration(admin_engine, schema_name, migration_sql)
        yield
    except Exception as primary_error:
        if schema_confirmed:
            try:
                _drop_schema(admin_engine, schema_name)
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    "schema operation and targeted cleanup both failed",
                    [primary_error, cleanup_error],
                ) from None
        raise
    else:
        if schema_confirmed:
            _drop_schema(admin_engine, schema_name)


@pytest.fixture
def engine() -> Engine:
    if not _GUARDRAILS_PRESENT:
        pytest.skip(_POSTGRES_SKIP_REASON)
    assert _URL is not None
    parsed = make_url(_URL.replace("postgresql+psycopg2", "postgresql"))
    assert parsed.drivername.startswith("postgresql")
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.database == _EXPECTED_DATABASE
    assert parsed.username == _EXPECTED_USER
    assert parsed.port == int(_EXPECTED_PORT)
    assert _EXPECTED_ENVIRONMENT == "qa"
    assert _CONTAINER_VERIFIED is True
    assert _CONTAINER_ID is not None
    assert re.fullmatch(r"[a-f0-9]{64}", _CONTAINER_ID)
    assert _SCHEMA_ISOLATION_AUTHORIZED is True
    admin_engine = create_engine(_URL, pool_pre_ping=True)
    with admin_engine.connect() as connection:
        database, user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert database == _EXPECTED_DATABASE
        assert user == _EXPECTED_USER
    schema_name = f"{_SCHEMA_PREFIX}{uuid4().hex}"
    migration_scripts: list[str] = []
    for sql_path in _SCHEMA_SQL_PATHS:
        assert sql_path.is_file()
        sql = sql_path.read_text(encoding="utf-8")
        assert sql.strip()
        migration_scripts.append(sql)
    try:
        with _isolated_schema(admin_engine, schema_name, migration_scripts):
            value = create_engine(
                _URL,
                pool_pre_ping=True,
                connect_args={"options": f"-csearch_path={schema_name}"},
            )
            try:
                with value.connect() as connection:
                    database, user, active_schema = connection.execute(text(
                        "SELECT current_database(), current_user, current_schema()"
                    )).one()
                    assert database == _EXPECTED_DATABASE
                    assert user == _EXPECTED_USER
                    assert active_schema == schema_name
                yield value
            finally:
                value.dispose()
    finally:
        admin_engine.dispose()


@dataclass(frozen=True)
class Scenario:
    ticket_id: UUID
    approval_run_id: UUID
    revision_id: UUID
    draft_id: UUID
    content_hash: str
    identity: ResolvedIdentity
    start: ApprovalRunStart
    revision: DepartmentRevision
    start_reference: AuthorizationReference
    approval_reference: AuthorizationReference
    idempotency_key: UUID
    request_hash: str


def _scenario(engine: Engine, *, start_now: bool = True, expires_at=None) -> Scenario:
    now = datetime.now(timezone.utc)
    ticket_id, response_run_id, approval_run_id = uuid4(), uuid4(), uuid4()
    source_draft_id, source_evaluation_id, revision_id, draft_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    preview_id, principal_id, binding_id = uuid4(), uuid4(), uuid4()
    start_auth_id, approval_auth_id = uuid4(), uuid4()
    source_draft_hash = canonical_hash({"draft": str(source_draft_id)})
    source_evaluation_hash = canonical_hash({"evaluation": str(source_evaluation_id)})
    content = "Validated warehouse response for transactional approval."
    content_hash = department_revision_content_hash(
        content=content, commercial_terms=[], pending_commercial_items=[], evidence=[]
    )
    evaluation_ref = ImmutableArtifactReference(
        artifact_type="evaluation", artifact_id=source_evaluation_id,
        contract_version="section-evaluation-v1", payload_hash=source_evaluation_hash,
        evaluated_content_hash=content_hash,
    )
    preview_ref = ImmutableArtifactReference(
        artifact_type="safe_preview", artifact_id=preview_id,
        contract_version="safe-preview-v1",
        payload_hash=canonical_hash({"preview": str(preview_id)}),
        evaluated_content_hash=content_hash,
    )
    validation_hash = canonical_hash({
        "revision_content_hash": content_hash, "evaluation": evaluation_ref,
        "safe_preview": preview_ref, "validated_at": now,
    })
    validation = ApprovalRevisionValidation(
        references=RevisionValidationReferences(
            revision_content_hash=content_hash, evaluation=evaluation_ref,
            safe_preview=preview_ref, validation_hash=validation_hash,
            validated_at=now,
        ), mandatory_compliance_passed=True, blocks_commercial_completeness=False,
        blocking_rule_ids=[], unresolved_input_codes=[], open_conflict_ids=[],
        global_revalidation_required=False, eligible_for_approval=True,
    )
    revision = DepartmentRevision(
        revision_id=revision_id, draft_id=draft_id, ticket_id=ticket_id,
        approval_run_id=approval_run_id, department=DepartmentId.warehouse,
        review_revision_no=1, source_part2_draft_id=source_draft_id,
        source_part2_draft_hash=source_draft_hash,
        source_part2_evaluation_id=source_evaluation_id,
        source_part2_evaluation_hash=source_evaluation_hash,
        source_part2_iteration=1, content=content, content_hash=content_hash,
        commercial_terms=[], pending_commercial_items=[], evidence=[],
        validation=validation, status="pending_approval", creation_reason="part2_import",
        created_at=now,
    )
    principal = AuthenticatedPrincipal(
        issuer="urn:trackflow:qa:oidc:gate2b3a",
        subject=f"subject-{principal_id}", trackflow_user_id=f"user-{principal_id}",
        auth_source="oidc", general_role="user", synthetic=True,
    )
    identity = ResolvedIdentity._from_persisted_binding(
        environment="qa", auth_source="oidc", issuer=principal.issuer,
        subject=principal.subject, trackflow_user_id=principal.trackflow_user_id,
        general_role="user", principal_id=principal_id, binding_id=binding_id,
        responsible_code="ana_whitfield", synthetic=True,
    )
    handoff = RFPResponseHandoff(
        ticket_id=ticket_id, run_id=response_run_id,
        source_handoff_hash=canonical_hash({"intake": str(ticket_id)}),
        outcome="ready_for_part3", all_sections_passed=True,
        global_compliance_passed=True, selected_departments=[DepartmentId.warehouse],
        department_results=[DepartmentHandoffResult(
            department=DepartmentId.warehouse, terminal_status="passed", iteration_count=1,
            final_draft_id=source_draft_id, evaluation_result_id=source_evaluation_id,
            safe_preview_id=preview_id, blocks_commercial_completeness=False,
            requires_human_input=False, blocking_rule_ids=[],
        )], global_draft_id=uuid4(), global_evaluation_result_id=uuid4(),
        unresolved_items=[], created_at=now,
    )
    handoff_payload = canonical_value(handoff)
    handoff_hash = canonical_hash(handoff_payload)
    start = ApprovalRunStart(
        approval_run_id=approval_run_id, ticket_id=ticket_id,
        response_run_id=response_run_id, response_handoff_hash=handoff_hash,
        active_departments=[DepartmentId.warehouse],
        initial_revisions=[InitialRevisionReference(
            department=DepartmentId.warehouse, source_draft_id=source_draft_id,
            source_draft_hash=source_draft_hash,
            source_evaluation_id=source_evaluation_id,
            source_evaluation_hash=source_evaluation_hash,
            source_part2_iteration=1, terminal_status="passed",
        )], started_by=RunInitiator(
            principal=principal, authorization_id=start_auth_id,
            permission="start_approval_run",
        ), started_at=now,
    )
    idempotency_key = uuid4()
    request_hash = approval_start_request_hash(start, [revision], idempotency_key)
    with Session(engine) as session, session.begin():
        session.execute(insert(rfp_tickets).values(
            ticket_id=ticket_id, status="en_evaluación", raw_pdf_path="gate2b3a/input.pdf",
            uploaded_by_subject="gate2b3a",
        ))
        session.execute(insert(rfp_response_runs).values(
            run_id=response_run_id, ticket_id=ticket_id,
            source_handoff_version="rfp-intake-v1",
            source_handoff_hash=handoff.source_handoff_hash,
            status="ready_for_part3", global_iteration_count=1,
        ))
        session.execute(insert(rfp_response_drafts).values(
            draft_id=source_draft_id, run_id=response_run_id, scope_key="warehouse",
            iteration_count=1, contract_version="rfp-department-draft-v1",
            payload={"artifact": "department"}, payload_hash=source_draft_hash,
        ))
        session.execute(insert(rfp_response_evaluations).values(
            evaluation_id=source_evaluation_id, run_id=response_run_id,
            draft_id=source_draft_id, scope_key="warehouse",
            contract_version="section-evaluation-v1", overall_pass=True,
            terminal_status="passed", payload={"artifact": "evaluation"},
            payload_hash=source_evaluation_hash,
        ))
        session.execute(insert(rfp_response_handoffs).values(
            run_id=response_run_id, ticket_id=ticket_id, contract_version="rfp-response-v1",
            outcome="ready_for_part3", payload=handoff_payload, payload_hash=handoff_hash,
        ))
        session.execute(insert(rfp_principals).values(
            principal_id=principal_id, environment="qa",
            trackflow_user_id=identity.trackflow_user_id,
            responsible_code="ana_whitfield", synthetic=True,
        ))
        session.execute(insert(rfp_identity_bindings).values(
            binding_id=binding_id, principal_id=principal_id, environment="qa",
            auth_source="oidc", issuer=identity.issuer, subject=identity.subject,
        ))
        grants = (
            (start_auth_id, "start_approval_run", None, "run", "start"),
            (approval_auth_id, "approve_department", "warehouse", "department_exact", "warehouse"),
        )
        for authorization_id, permission, department, policy, scope in grants:
            session.execute(insert(rfp_authorizations).values(
                authorization_id=authorization_id, principal_id=principal_id,
                environment="qa", permission=permission, department_scope=department,
                trigger_scope=None, target_policy=policy,
                scope_fingerprint=canonical_hash({"scope": scope}),
                authorization_version=1, active_from=now - timedelta(minutes=1),
                expires_at=expires_at if permission == "approve_department" else None,
            ))
    scenario = Scenario(
        ticket_id, approval_run_id, revision_id, draft_id, content_hash, identity,
        start, revision, AuthorizationReference(start_auth_id, 1),
        AuthorizationReference(approval_auth_id, 1), idempotency_key, request_hash,
    )
    if start_now:
        with Session(engine) as session, session.begin():
            start_approval_run(
                session, start=start, initial_revisions=[revision], identity=identity,
                authorization_reference=scenario.start_reference,
                idempotency_key=idempotency_key, request_hash=request_hash,
                execution_id=uuid4(),
            )
    return scenario


def _command(scenario: Scenario) -> DecisionApplicationCommand:
    actor = AuthorizedHumanActor(
        principal=scenario.start.started_by.principal,
        authorization_id=scenario.approval_reference.authorization_id,
        official_responsible="Ana Whitfield", permission="approve_department",
        department_scope=DepartmentId.warehouse,
    )
    decision = HumanDecision(
        decision_id=uuid4(), idempotency_key=uuid4(), ticket_id=scenario.ticket_id,
        approval_run_id=scenario.approval_run_id, department=DepartmentId.warehouse,
        revision_id=scenario.revision_id, draft_id=scenario.draft_id,
        expected_content_hash=scenario.content_hash, interrupt_id=f"interrupt-{uuid4()}",
        action="approve", actor=actor, decided_at=datetime.now(timezone.utc),
    )
    preparation_id = uuid4()
    return DecisionApplicationCommand(
        decision=decision, preparation_id=preparation_id,
        application_key=decision_application_key(decision, preparation_id),
    )


def _register(engine: Engine, scenario: Scenario, command=None):
    command = command or _command(scenario)
    with Session(engine) as session, session.begin():
        result = register_human_decision(
            session, command=command, identity=scenario.identity,
            authorization_reference=scenario.approval_reference, execution_id=uuid4(),
        )
    return command, result


def test_concurrent_double_start_is_atomic_and_idempotent(engine: Engine) -> None:
    scenario = _scenario(engine, start_now=False)
    def start_once():
        with Session(engine) as session, session.begin():
            return start_approval_run(
                session, start=scenario.start, initial_revisions=[scenario.revision],
                identity=scenario.identity, authorization_reference=scenario.start_reference,
                idempotency_key=scenario.idempotency_key,
                request_hash=scenario.request_hash, execution_id=uuid4(),
            )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start_once(), range(2)))
    assert sorted(item.replayed for item in results) == [False, True]
    with Session(engine) as session:
        rows = session.execute(select(rfp_approval_runs).where(
            rfp_approval_runs.c.ticket_id == scenario.ticket_id)).mappings().all()
    assert len(rows) == 1


def test_concurrent_double_decision_is_idempotent(engine: Engine) -> None:
    scenario = _scenario(engine)
    command = _command(scenario)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _register(engine, scenario, command)[1], range(2)))
    assert sorted(item.replayed for item in results) == [False, True]


def test_two_workers_claim_one_effective_owner_with_skip_locked(engine: Engine) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    def claim_once():
        with Session(engine) as session, session.begin():
            return claim_next_decision(session, execution_id=uuid4(), lease_seconds=60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim_once(), range(2)))
    assert sum(item is not None for item in results) == 1


def test_claim_event_and_apply_lock_order_has_no_fk_deadlock(
    engine: Engine, monkeypatch
) -> None:
    scenario = _scenario(engine)
    command, _ = _register(engine, scenario)
    claim_token = uuid4()
    claim_at_event = Event()
    release_claim_event = Event()
    original_insert_event = store_module._insert_event

    def coordinated_event(*args, **kwargs):
        if kwargs.get("event_type") == "human_decision_claimed":
            claim_at_event.set()
            assert release_claim_event.wait(timeout=10)
        return original_insert_event(*args, **kwargs)

    monkeypatch.setattr(store_module, "_insert_event", coordinated_event)

    def claim_worker():
        with Session(engine) as session, session.begin():
            return claim_next_decision(
                session,
                execution_id=uuid4(),
                lease_seconds=30,
                claim_token=claim_token,
            )

    apply_pid: list[int] = []
    apply_started = Event()

    def apply_worker():
        with Session(engine) as session, session.begin():
            apply_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            apply_started.set()
            return apply_claimed_decision(
                session,
                decision_id=command.decision.decision_id,
                claim_token=claim_token,
                claim_epoch=1,
                expected_revision_state_version=0,
                identity=scenario.identity,
                authorization_reference=scenario.approval_reference,
                execution_id=uuid4(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim_worker)
        assert claim_at_event.wait(timeout=5)
        apply_future = pool.submit(apply_worker)
        assert apply_started.wait(timeout=5)
        deadline = time.monotonic() + 5
        waiting = False
        while time.monotonic() < deadline:
            with Session(engine) as observer:
                waiting = bool(observer.execute(text(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity "
                    "WHERE pid=:pid"
                ), {"pid": apply_pid[0]}).scalar_one_or_none())
            if waiting:
                break
            time.sleep(0.05)
        assert waiting, "apply worker did not reach the coordinated decision lock"
        release_claim_event.set()
        claim = claim_future.result(timeout=10)
        applied = apply_future.result(timeout=10)
    assert claim is not None and claim.claim_epoch == 1
    assert applied.processing_status == "applied"


def test_crash_after_queued_preserves_recoverable_decision(engine: Engine) -> None:
    scenario = _scenario(engine)
    command, _ = _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None and claim.decision_id == command.decision.decision_id


def test_expired_lease_recovery_fences_old_epoch(engine: Engine) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        first = claim_next_decision(session, execution_id=uuid4())
    assert first is not None
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_human_decisions).where(
            rfp_human_decisions.c.decision_id == first.decision_id
        ).values(
            claimed_at=text("clock_timestamp() - interval '2 seconds'"),
            lease_until=text("clock_timestamp() - interval '1 second'"),
        ))
    with Session(engine) as session, session.begin():
        second = claim_next_decision(session, execution_id=uuid4())
    assert second is not None
    assert second.claim_epoch == first.claim_epoch + 1
    assert second.claim_token != first.claim_token
    assert (
        second.expected_revision_state_version
        == first.expected_revision_state_version
    )
    with Session(engine) as session:
        revision_before = session.execute(select(
            rfp_approval_department_revisions.c.state_version,
            rfp_approval_department_revisions.c.status,
        ).where(
            rfp_approval_department_revisions.c.revision_id == scenario.revision_id
        )).one()
    assert revision_before.state_version == second.expected_revision_state_version

    stale_attempts = (
        (first.claim_token, first.claim_epoch, "claim_token_mismatch"),
        (second.claim_token, first.claim_epoch, "claim_epoch_mismatch"),
    )
    for claim_token, claim_epoch, expected_code in stale_attempts:
        with Session(engine) as session, session.begin():
            with pytest.raises(ApprovalStoreError) as raised:
                apply_claimed_decision(
                    session,
                    decision_id=first.decision_id,
                    claim_token=claim_token,
                    claim_epoch=claim_epoch,
                    expected_revision_state_version=(
                        first.expected_revision_state_version
                    ),
                    identity=scenario.identity,
                    authorization_reference=scenario.approval_reference,
                    execution_id=uuid4(),
                )
        assert raised.value.code == expected_code

    with Session(engine) as session:
        revision_after = session.execute(select(
            rfp_approval_department_revisions.c.state_version,
            rfp_approval_department_revisions.c.status,
        ).where(
            rfp_approval_department_revisions.c.revision_id == scenario.revision_id
        )).one()
    assert revision_after == revision_before


def test_retry_after_application_commit_is_idempotent(engine: Engine) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None
    kwargs = dict(
        decision_id=claim.decision_id, claim_token=claim.claim_token,
        claim_epoch=claim.claim_epoch,
        expected_revision_state_version=claim.expected_revision_state_version,
        identity=scenario.identity, authorization_reference=scenario.approval_reference,
    )
    with Session(engine) as session, session.begin():
        first = apply_claimed_decision(session, **kwargs, execution_id=uuid4())
    with Session(engine) as session, session.begin():
        replay = apply_claimed_decision(session, **kwargs, execution_id=uuid4())
    assert first.replayed is False and replay.replayed is True


def _confirmed_decision(engine: Engine, scenario: Scenario):
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None
    kwargs = dict(
        decision_id=claim.decision_id,
        claim_token=claim.claim_token,
        claim_epoch=claim.claim_epoch,
        expected_revision_state_version=claim.expected_revision_state_version,
        identity=scenario.identity,
        authorization_reference=scenario.approval_reference,
    )
    with Session(engine) as session, session.begin():
        apply_claimed_decision(session, **kwargs, execution_id=uuid4())
    return claim, kwargs


def test_applied_replay_rejects_other_principal(engine: Engine) -> None:
    scenario = _scenario(engine)
    _claim, kwargs = _confirmed_decision(engine, scenario)
    other = ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer=scenario.identity.issuer,
        subject=scenario.identity.subject,
        trackflow_user_id=scenario.identity.trackflow_user_id,
        general_role="user",
        principal_id=uuid4(),
        binding_id=uuid4(),
        responsible_code="ana_whitfield",
        synthetic=True,
    )
    with Session(engine) as session, session.begin():
        with pytest.raises(ApprovalStoreError) as raised:
            apply_claimed_decision(
                session, **{**kwargs, "identity": other}, execution_id=uuid4()
            )
    assert raised.value.code == "decision_actor_mismatch"


@pytest.mark.parametrize("mutation", ["revoked", "expired", "reassigned"])
def test_applied_replay_rechecks_current_grant_and_official_assignment(
    engine: Engine, mutation: str
) -> None:
    scenario = _scenario(engine)
    _claim, kwargs = _confirmed_decision(engine, scenario)
    with Session(engine) as session, session.begin():
        if mutation == "revoked":
            session.execute(
                update(rfp_authorizations)
                .where(
                    rfp_authorizations.c.authorization_id
                    == scenario.approval_reference.authorization_id
                )
                .values(revoked_at=text("clock_timestamp()"))
            )
        elif mutation == "expired":
            session.execute(
                update(rfp_authorizations)
                .where(
                    rfp_authorizations.c.authorization_id
                    == scenario.approval_reference.authorization_id
                )
                .values(expires_at=text("clock_timestamp() - interval '1 second'"))
            )
        else:
            session.execute(
                update(rfp_principals)
                .where(rfp_principals.c.principal_id == scenario.identity.principal_id)
                .values(responsible_code="carlos_vega")
            )
    with Session(engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied):
            apply_claimed_decision(session, **kwargs, execution_id=uuid4())


def test_applied_replay_rejects_disabled_binding(engine: Engine) -> None:
    scenario = _scenario(engine)
    _claim, kwargs = _confirmed_decision(engine, scenario)
    with Session(engine) as session, session.begin():
        session.execute(
            update(rfp_identity_bindings)
            .where(rfp_identity_bindings.c.binding_id == scenario.identity.binding_id)
            .values(active=False, disabled_at=text("clock_timestamp()"))
        )
    with Session(engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied):
            apply_claimed_decision(session, **kwargs, execution_id=uuid4())


def test_start_and_registration_replays_reject_revoked_grants(engine: Engine) -> None:
    scenario = _scenario(engine)
    command, _ = _register(engine, scenario)
    with Session(engine) as session, session.begin():
        session.execute(
            update(rfp_authorizations)
            .where(
                rfp_authorizations.c.authorization_id.in_(
                    [
                        scenario.start_reference.authorization_id,
                        scenario.approval_reference.authorization_id,
                    ]
                )
            )
            .values(revoked_at=text("clock_timestamp()"))
        )
    with Session(engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied):
            start_approval_run(
                session,
                start=scenario.start,
                initial_revisions=[scenario.revision],
                identity=scenario.identity,
                authorization_reference=scenario.start_reference,
                idempotency_key=scenario.idempotency_key,
                request_hash=scenario.request_hash,
                execution_id=uuid4(),
            )
    with Session(engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied):
            register_human_decision(
                session,
                command=command,
                identity=scenario.identity,
                authorization_reference=scenario.approval_reference,
                execution_id=uuid4(),
            )


def test_revocation_committed_before_apply_is_observed(engine: Engine) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_authorizations).where(
            rfp_authorizations.c.authorization_id == scenario.approval_reference.authorization_id
        ).values(revoked_at=text("clock_timestamp()")))
    with Session(engine) as session, session.begin(), pytest.raises(AuthorizationDenied) as raised:
        apply_claimed_decision(
            session, decision_id=claim.decision_id, claim_token=claim.claim_token,
            claim_epoch=claim.claim_epoch,
            expected_revision_state_version=claim.expected_revision_state_version,
            identity=scenario.identity, authorization_reference=scenario.approval_reference,
            execution_id=uuid4(),
        )
    assert raised.value.code == "no_effective_authorization"


def _apply_expect_denied(engine: Engine, scenario: Scenario, claim) -> str:
    with Session(engine) as session, session.begin():
        with pytest.raises(AuthorizationDenied) as raised:
            apply_claimed_decision(
                session, decision_id=claim.decision_id, claim_token=claim.claim_token,
                claim_epoch=claim.claim_epoch,
                expected_revision_state_version=claim.expected_revision_state_version,
                identity=scenario.identity,
                authorization_reference=scenario.approval_reference,
                execution_id=uuid4(),
            )
    return raised.value.code


def test_lease_expiry_during_authorization_lock_wait_is_not_permission_revocation(
    engine: Engine,
) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(
            session, execution_id=uuid4(), lease_seconds=1
        )
    assert claim is not None
    locked, release = Event(), Event()

    def hold_authorization_lock():
        with Session(engine) as session, session.begin():
            session.execute(
                select(rfp_authorizations.c.authorization_id)
                .where(
                    rfp_authorizations.c.authorization_id
                    == scenario.approval_reference.authorization_id
                )
                .with_for_update()
            ).scalar_one()
            locked.set()
            assert release.wait(timeout=10)

    def apply_after_wait():
        with Session(engine) as session, session.begin():
            with pytest.raises(ApprovalStoreError) as raised:
                apply_claimed_decision(
                    session,
                    decision_id=claim.decision_id,
                    claim_token=claim.claim_token,
                    claim_epoch=claim.claim_epoch,
                    expected_revision_state_version=claim.expected_revision_state_version,
                    identity=scenario.identity,
                    authorization_reference=scenario.approval_reference,
                    execution_id=uuid4(),
                )
        return raised.value.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_authorization_lock)
        assert locked.wait(timeout=5)
        applier = pool.submit(apply_after_wait)
        time.sleep(1.2)
        release.set()
        holder.result(timeout=10)
        assert applier.result(timeout=10) == "claim_lease_expired"


def test_expiry_during_lock_wait_uses_post_wait_statement_time(engine: Engine) -> None:
    scenario = _scenario(
        engine, expires_at=datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None
    locked, release = Event(), Event()
    def hold_authorization_lock():
        with Session(engine) as session, session.begin():
            session.execute(select(rfp_authorizations.c.authorization_id).where(
                rfp_authorizations.c.authorization_id
                == scenario.approval_reference.authorization_id
            ).with_for_update()).scalar_one()
            locked.set()
            assert release.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_authorization_lock)
        assert locked.wait(timeout=5)
        future = pool.submit(_apply_expect_denied, engine, scenario, claim)
        time.sleep(2.2)
        release.set()
        holder.result(timeout=10)
        assert future.result(timeout=10) == "no_effective_authorization"


def test_revision_cas_rejects_obsolete_state_version(engine: Engine) -> None:
    scenario = _scenario(engine)
    _register(engine, scenario)
    with Session(engine) as session, session.begin():
        claim = claim_next_decision(session, execution_id=uuid4())
    assert claim is not None
    with Session(engine) as session, session.begin():
        session.execute(update(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == scenario.revision_id
        ).values(state_version=rfp_approval_department_revisions.c.state_version + 1))
    with Session(engine) as session, session.begin():
        result = apply_claimed_decision(
            session, decision_id=claim.decision_id, claim_token=claim.claim_token,
            claim_epoch=claim.claim_epoch,
            expected_revision_state_version=claim.expected_revision_state_version,
            identity=scenario.identity, authorization_reference=scenario.approval_reference,
            execution_id=uuid4(),
        )
    assert result.processing_status == "rejected_stale"


def _offline_schema_name() -> str:
    return f"{_SCHEMA_PREFIX}{'a' * 32}"


def test_schema_create_commits_before_independent_existence_check() -> None:
    schema_name = _offline_schema_name()
    events: list[object] = []

    class Result:
        def scalar_one(self):
            return True

    class Connection:
        def __init__(self, kind: str):
            self.kind = kind

        def __enter__(self):
            events.append((self.kind, "enter"))
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append((self.kind, "commit" if exc_type is None else "rollback"))

        def exec_driver_sql(self, statement: str):
            events.append((self.kind, statement))

        def execute(self, statement, parameters):
            events.append((self.kind, "exists", parameters["schema_name"]))
            return Result()

    class AdminEngine:
        def begin(self):
            events.append("begin")
            return Connection("transaction")

        def connect(self):
            events.append("connect")
            return Connection("independent")

    admin_engine = AdminEngine()
    _create_schema(admin_engine, schema_name)  # type: ignore[arg-type]
    assert _schema_exists(admin_engine, schema_name)  # type: ignore[arg-type]
    assert events == [
        "begin",
        ("transaction", "enter"),
        ("transaction", f'CREATE SCHEMA "{schema_name}"'),
        ("transaction", "commit"),
        "connect",
        ("independent", "enter"),
        ("independent", "exists", schema_name),
        ("independent", "commit"),
    ]


def test_schema_lifecycle_orders_confirmation_migrations_and_cleanup(
    monkeypatch,
) -> None:
    module = sys.modules[__name__]
    schema_name = _offline_schema_name()
    scripts = ("BEGIN; SELECT 1; COMMIT;", "BEGIN; SELECT 2; COMMIT;")
    events: list[object] = []
    monkeypatch.setattr(
        module, "_create_schema", lambda engine, schema: events.append(("create", schema))
    )
    monkeypatch.setattr(
        module,
        "_schema_exists",
        lambda engine, schema: events.append(("verify", schema)) or True,
    )
    monkeypatch.setattr(
        module,
        "_run_schema_migration",
        lambda engine, schema, sql: events.append(("migrate", schema, sql)),
    )
    monkeypatch.setattr(
        module, "_drop_schema", lambda engine, schema: events.append(("drop", schema))
    )

    with _isolated_schema(object(), schema_name, scripts):  # type: ignore[arg-type]
        events.append("body")

    assert events == [
        ("create", schema_name),
        ("verify", schema_name),
        ("migrate", schema_name, scripts[0]),
        ("migrate", schema_name, scripts[1]),
        "body",
        ("drop", schema_name),
    ]


def test_schema_create_failure_runs_neither_migration_nor_drop(monkeypatch) -> None:
    module = sys.modules[__name__]
    events: list[str] = []

    def fail_create(engine, schema):
        events.append("create")
        raise RuntimeError("create failed")

    monkeypatch.setattr(module, "_create_schema", fail_create)
    monkeypatch.setattr(
        module, "_run_schema_migration", lambda *args: events.append("migrate")
    )
    monkeypatch.setattr(module, "_drop_schema", lambda *args: events.append("drop"))

    with pytest.raises(RuntimeError, match="create failed"):
        with _isolated_schema(object(), _offline_schema_name(), ("BEGIN; COMMIT;",)):
            pass
    assert events == ["create"]


def test_unconfirmed_schema_runs_neither_migration_nor_drop(monkeypatch) -> None:
    module = sys.modules[__name__]
    events: list[str] = []
    monkeypatch.setattr(module, "_create_schema", lambda *args: events.append("create"))
    monkeypatch.setattr(
        module, "_schema_exists", lambda *args: events.append("verify") or False
    )
    monkeypatch.setattr(
        module, "_run_schema_migration", lambda *args: events.append("migrate")
    )
    monkeypatch.setattr(module, "_drop_schema", lambda *args: events.append("drop"))

    with pytest.raises(SchemaIsolationError, match="creation was not confirmed"):
        with _isolated_schema(object(), _offline_schema_name(), ("BEGIN; COMMIT;",)):
            pass
    assert events == ["create", "verify"]


def test_migration_failure_drops_only_the_confirmed_schema(monkeypatch) -> None:
    module = sys.modules[__name__]
    schema_name = _offline_schema_name()
    dropped: list[str] = []
    monkeypatch.setattr(module, "_create_schema", lambda *args: None)
    monkeypatch.setattr(module, "_schema_exists", lambda *args: True)
    monkeypatch.setattr(
        module,
        "_run_schema_migration",
        lambda *args: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    monkeypatch.setattr(
        module, "_drop_schema", lambda engine, schema: dropped.append(schema)
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        with _isolated_schema(object(), schema_name, ("BEGIN; COMMIT;",)):
            pass
    assert dropped == [schema_name]


def test_primary_and_cleanup_errors_remain_visible(monkeypatch) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_create_schema", lambda *args: None)
    monkeypatch.setattr(module, "_schema_exists", lambda *args: True)
    monkeypatch.setattr(
        module,
        "_run_schema_migration",
        lambda *args: (_ for _ in ()).throw(RuntimeError("primary migration error")),
    )
    monkeypatch.setattr(
        module,
        "_drop_schema",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cleanup error")),
    )

    with pytest.raises(ExceptionGroup) as raised:
        with _isolated_schema(
            object(), _offline_schema_name(), ("BEGIN; COMMIT;",)
        ):
            pass
    assert [str(error) for error in raised.value.exceptions] == [
        "primary migration error",
        "cleanup error",
    ]


def test_migration_uses_exclusive_search_path_and_preserves_transaction_block() -> None:
    schema_name = _offline_schema_name()
    migration_sql = "BEGIN;\nSELECT 1;\nCOMMIT;"
    events: list[object] = []

    class Result:
        def scalar_one(self):
            return schema_name

    class Connection:
        def execution_options(self, **options):
            events.append(("options", options))
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append("close")

        def exec_driver_sql(self, statement: str):
            events.append(("sql", statement))
            return Result()

    class AdminEngine:
        def connect(self):
            events.append("connect")
            return Connection()

    _run_schema_migration(
        AdminEngine(), schema_name, migration_sql  # type: ignore[arg-type]
    )
    assert events == [
        "connect",
        ("options", {"isolation_level": "AUTOCOMMIT"}),
        ("sql", f'SET search_path TO "{schema_name}"'),
        ("sql", "SELECT current_schema()"),
        ("sql", migration_sql),
        "close",
    ]
    assert "public" not in str(events)
