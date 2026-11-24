"""Authenticated HTTP + PostgreSQL coverage for department corrections.

This module is deliberately opt-in.  Collection performs no I/O.  The first
integration fixture verifies an explicitly authorized disposable Docker
container and PostgreSQL identity before it creates a UUID schema.  Every
scenario uses the existing FastAPI routes, correction coordinator, stores and
historical migrations; no business state is manufactured after the scenario
fixture has been prepared.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, insert, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    DepartmentHandoffResult,
    DepartmentDraft,
    GlobalCommercialTermsDraft,
    MissingCommercialInput,
    ProposalEvaluationResult,
    RFPResponseStart,
    RFPResponseHandoff,
    RequestedAspect,
)
from data.pipelines.rfp_response.compliance import (
    build_global_safe_preview,
    build_safe_preview,
)
from data.pipelines.rfp_response.evaluators import (
    evaluate_department,
    evaluate_global,
    inspect_department_safety,
    inspect_global_safety,
)
from data.pipelines.rfp_response.evaluation import reduce_proposal_evaluations
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from data.pipelines.rfp_response.generators import (
    generate_department_draft,
    generate_global_terms,
)
from pipelines.rfp_approval.contracts import (
    ApprovalArtifactProvenance,
    ApprovalRunStart,
    AuthenticatedPrincipal,
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    InitialRevisionReference,
    RunInitiator,
)
from pipelines.rfp_approval.hitl import DepartmentApprovalTarget
from services.rfp_approval.artifact_store import (
    ArtifactProducerContext,
    _assert_producer,
    persist_corrected_draft,
    persist_validation_artifact,
    persist_validation_bundle,
)
from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_department_revisions,
    rfp_approval_runs,
    rfp_approval_validation_artifacts,
    rfp_approval_validation_bundles,
    rfp_arbitration_conflicts,
    rfp_authorizations,
    rfp_authorized_business_inputs,
    rfp_conflict_assessments,
    rfp_final_documents,
    rfp_human_decisions,
    rfp_identity_bindings,
    rfp_principals,
    rfp_workflow_events,
)
from services.rfp_approval.run_coordinator import _build_chain
from services.rfp_approval.store import (
    ApprovalStoreError,
    approval_start_request_hash,
    start_approval_run,
)
from services.rfp_intake.models import (
    rfp_department_sections,
    rfp_handoffs,
    rfp_metadata,
    rfp_tickets,
)
from services.rfp_response.models import (
    rfp_response_drafts,
    rfp_response_evaluations,
    rfp_response_handoffs,
    rfp_response_previews,
    rfp_response_runs,
)
from services.rfp_response.store import load_response_start, start_response_run


PREFIX = "TRACKFLOW_RFP_APPROVAL_CORRECTION_"
URL_ENV = PREFIX + "TEST_DATABASE_URL"
AUTHORIZED_ENV = PREFIX + "DDL_DML_AUTHORIZED"
EXPECTED_DATABASE_ENV = PREFIX + "EXPECTED_DATABASE"
EXPECTED_USER_ENV = PREFIX + "EXPECTED_USER"
EXPECTED_PORT_ENV = PREFIX + "EXPECTED_PORT"
EXPECTED_ENVIRONMENT_ENV = PREFIX + "EXPECTED_ENVIRONMENT"
EXPECTED_CONTAINER_ENV = PREFIX + "EXPECTED_CONTAINER_NAME"
EXPECTED_IMAGE_ENV = PREFIX + "EXPECTED_IMAGE"
VERIFIED_CONTAINER_ID_ENV = PREFIX + "VERIFIED_CONTAINER_ID"
CONTAINER_VERIFIED_ENV = PREFIX + "CONTAINER_VERIFIED"
SCHEMA_ISOLATION_ENV = PREFIX + "SCHEMA_ISOLATION_AUTHORIZED"
DATABASE_PREFIX = "trackflow_rfp_approval_correction_test_"
USER_PREFIX = "trackflow_rfp_approval_correction_"
CONTAINER_PREFIX = "trackflow-rfp-approval-correction-postgres-"
SCHEMA_PREFIX = "rfp_correction_test_"
POSTGRES_DATA_DIR = "/var/lib/postgresql/data"
SKIP_REASON = "isolated correction HTTP/PostgreSQL gate is not authorized"
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
HISTORICAL_PART3_DDL = (
    ROOT / "tests/fixtures/rfp_approval_gate_2b1_seven_input_kinds.sql"
)
HISTORICAL_PART3_DDL_SHA256 = (
    "0ccbb806fb77a6b9a4ca652ac3fc511365fe9057c893d87cc8909e58676b882e"
)
HISTORICAL_INPUT_KINDS = (
    "client_price_quote",
    "warehouse_capacity",
    "operational_scope",
    "currency_quote",
    "currency_conversion",
    "global_commercial_term",
    "volume_discount",
)
CURRENT_INPUT_KINDS = (*HISTORICAL_INPUT_KINDS, "monthly_storage_volume")


def _response_terminal_state(*, initial_hitl: bool) -> tuple[str, str]:
    """Return the coherent response-run and ticket states for the fixture path."""

    if initial_hitl:
        return "ready_for_part3", "en_evaluación"
    return "needs_human_review", "needs_human_review"


def _global_import_failures(
    draft,
    evaluation,
    preview,
    *,
    require_department_rollup_pass: bool = True,
) -> tuple[str, ...]:
    """Mirror the importer's strict and corrected-history validation modes."""

    checks = {
        "global_compliance": evaluation.global_compliance.passed,
        "pending_commercial_items": not draft.pending_commercial_items,
        "safe_preview_gate": preview.gate_status == "passed",
        "safe_preview_content": bool(preview.content),
    }
    if require_department_rollup_pass:
        checks = {
            "evaluation_overall_pass": evaluation.overall_pass,
            "evaluation_outcome": evaluation.outcome == "ready_for_part3",
            **checks,
        }
    return tuple(name for name, passed in checks.items() if not passed)


def _build_evaluated_global_source(
    response_start,
    source,
    section_evaluation,
    *,
    require_department_rollup_pass: bool = True,
):
    """Build an honest global P2 source for initial or corrected-history use."""

    sources = source if isinstance(source, list) else [source]
    section_evaluations = (
        section_evaluation
        if isinstance(section_evaluation, list)
        else [section_evaluation]
    )
    draft = generate_global_terms(
        response_start,
        department_draft_ids=[item.draft_id for item in sources],
        iteration_count=1,
        generated_at=NOW,
    )
    inspection = inspect_global_safety(response_start, draft, inspected_at=NOW)
    preview = build_global_safe_preview(draft, inspection)
    section_currencies = frozenset(
        term.currency
        for item in sources
        for term in item.commercial_terms
        if term.currency is not None
    )
    compliance, feedback = evaluate_global(
        response_start,
        draft,
        section_currencies=section_currencies,
        consistency_issues=[],
        evaluated_at=NOW,
    )
    evaluation = reduce_proposal_evaluations(
        ticket_id=response_start.ticket_id,
        run_id=response_start.run_id,
        section_results=section_evaluations,
        global_draft_id=draft.draft_id,
        global_iteration_count=draft.global_iteration_count,
        global_compliance=compliance,
        evaluated_at=NOW,
    )
    failures = _global_import_failures(
        draft,
        evaluation,
        preview,
        require_department_rollup_pass=require_department_rollup_pass,
    )
    if feedback or failures:
        raise RuntimeError(
            "generated Part 2 global source is not eligible for import: "
            f"{','.join((*feedback, *failures))}"
        )
    return draft, evaluation, preview


@dataclass(frozen=True)
class IsolatedDatabase:
    engine: Engine
    schema: str
    url: str = field(repr=False)


class _RedactedSecret(str):
    """Keep secrets usable as strings without exposing them in pytest reprs."""

    def __repr__(self) -> str:
        return "'[REDACTED]'"


def _producer_from_chain(chain, *, environment: str) -> ArtifactProducerContext:
    identities = {
        (artifact.provenance.producer_node, artifact.provenance.execution_id)
        for artifact in (
            chain.draft,
            chain.evaluation,
            chain.preview,
            chain.bundle,
        )
    }
    if len(identities) != 1:
        raise RuntimeError("fixture artifact provenance identities diverged")
    producer_node, execution_id = identities.pop()
    return ArtifactProducerContext._from_verified_orchestrator(
        environment=environment,
        producer_node=producer_node,
        execution_id=execution_id,
    )


def _guardrail_reason(environment: dict[str, str] | None = None) -> str | None:
    values = os.environ if environment is None else environment
    required = {
        AUTHORIZED_ENV: "YES",
        EXPECTED_ENVIRONMENT_ENV: "qa",
        CONTAINER_VERIFIED_ENV: "YES",
        SCHEMA_ISOLATION_ENV: "YES",
    }
    url = values.get(URL_ENV)
    if not url or any(values.get(key) != expected for key, expected in required.items()):
        return SKIP_REASON
    database = values.get(EXPECTED_DATABASE_ENV)
    user = values.get(EXPECTED_USER_ENV)
    port = values.get(EXPECTED_PORT_ENV)
    container = values.get(EXPECTED_CONTAINER_ENV)
    image = values.get(EXPECTED_IMAGE_ENV)
    container_id = values.get(VERIFIED_CONTAINER_ID_ENV)
    if not all((database, user, port, container, image, container_id)):
        return SKIP_REASON
    try:
        parsed = make_url(url)
        expected_port = int(port)
    except (TypeError, ValueError):
        return "invalid PostgreSQL guardrail configuration"
    if not parsed.drivername.startswith("postgresql"):
        return "test URL is not PostgreSQL"
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        return "PostgreSQL host is not loopback"
    if (parsed.database, parsed.username, parsed.port) != (database, user, expected_port):
        return "PostgreSQL target identity mismatch"
    if not database.startswith(DATABASE_PREFIX) or not user.startswith(USER_PREFIX):
        return "database or user is not dedicated to the correction gate"
    if not container.startswith(CONTAINER_PREFIX):
        return "container name is not dedicated to the correction gate"
    if not image.startswith("postgres:16"):
        return "container image is not an explicit PostgreSQL 16 image"
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        return "container identity was not externally verified"
    return None


def _inspect_container(environment: dict[str, str]) -> None:
    """Corroborate the declared identity against Docker before opening PostgreSQL."""

    name = environment[EXPECTED_CONTAINER_ENV]
    expected_id = environment[VERIFIED_CONTAINER_ID_ENV]
    completed = subprocess.run(
        ["docker", "inspect", name],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    inspected = json.loads(completed.stdout)
    if len(inspected) != 1:
        raise RuntimeError("Docker returned an ambiguous correction-gate container")
    value = inspected[0]
    state = value.get("State", {})
    health = state.get("Health", {}).get("Status")
    if value.get("Id") != expected_id or not state.get("Running") or health != "healthy":
        raise RuntimeError("correction-gate container identity or health mismatch")
    configured_healthcheck = value.get("Config", {}).get("Healthcheck", {}).get("Test")
    if not configured_healthcheck or configured_healthcheck == ["NONE"]:
        raise RuntimeError("correction-gate container lacks a configured healthcheck")
    expected_image = environment[EXPECTED_IMAGE_ENV]
    if value.get("Config", {}).get("Image") != expected_image:
        raise RuntimeError("correction-gate container image mismatch")
    host_config = value.get("HostConfig", {})
    tmpfs = host_config.get("Tmpfs") or {}
    if POSTGRES_DATA_DIR not in tmpfs:
        raise RuntimeError("PostgreSQL data directory is not backed by tmpfs")
    unauthorized_mounts = [
        mount
        for mount in value.get("Mounts", [])
        if mount.get("Type") != "tmpfs"
        or mount.get("Destination") != POSTGRES_DATA_DIR
    ]
    if unauthorized_mounts:
        raise RuntimeError("correction-gate container has a persistent or shared mount")
    if host_config.get("RestartPolicy", {}).get("Name") not in {"", "no"}:
        raise RuntimeError("correction-gate container restart policy is not disabled")
    networks = value.get("NetworkSettings", {}).get("Networks", {})
    if len(networks) != 1:
        raise RuntimeError("correction-gate container must have one isolated network")
    network_name = next(iter(networks))
    network_result = subprocess.run(
        ["docker", "network", "inspect", network_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    network_values = json.loads(network_result.stdout)
    if len(network_values) != 1:
        raise RuntimeError("Docker returned an ambiguous correction-gate network")
    members = set((network_values[0].get("Containers") or {}).keys())
    if members != {expected_id}:
        raise RuntimeError("correction-gate network contains an unauthorized member")
    expected_port = environment[EXPECTED_PORT_ENV]
    bindings = value.get("NetworkSettings", {}).get("Ports", {}).get("5432/tcp") or []
    if bindings != [{"HostIp": "127.0.0.1", "HostPort": expected_port}]:
        raise RuntimeError("PostgreSQL is not published exclusively on loopback")


def _quoted_schema(schema: str) -> str:
    if not re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema):
        raise RuntimeError("unsafe correction-gate schema name")
    return f'"{schema}"'


def _schema_exists(admin: Engine, schema: str) -> bool:
    with admin.connect() as connection:
        return bool(connection.execute(text(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"
        ), {"schema": schema}).scalar_one())


def _run_migration(admin: Engine, schema: str, path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"missing migration: {path.name}")
    with admin.connect().execution_options(
        isolation_level="AUTOCOMMIT", no_parameters=True
    ) as connection:
        connection.exec_driver_sql(f"SET search_path TO {_quoted_schema(schema)}")
        if connection.exec_driver_sql("SELECT current_schema()").scalar_one() != schema:
            raise RuntimeError("exclusive correction schema was not selected")
        connection.exec_driver_sql(path.read_text(encoding="utf-8"))


def _seed_history(engine: Engine, schema: str, seed) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if connection.execute(text("SELECT current_schema()")).scalar_one() != schema:
            raise RuntimeError("history seed escaped the correction schema")
        seed(connection.connection.driver_connection)


def _selected_historical_migrations(shared, *, seven_kinds: bool):
    """Select the immutable Gate 2B1 P3 snapshot without rewriting its SQL."""

    return tuple(
        (
            HISTORICAL_PART3_DDL
            if seven_kinds and migration.name == "rfp_approval.sql"
            else migration,
            seed,
        )
        for migration, seed in shared.HISTORICAL_MIGRATIONS_AND_SEEDS
    )


@pytest.fixture
def isolated_engine(request: pytest.FixtureRequest) -> Iterator[IsolatedDatabase]:
    reason = _guardrail_reason()
    if reason is not None:
        pytest.skip(reason)
    environment = dict(os.environ)
    _inspect_container(environment)
    url = environment[URL_ENV]
    admin = create_engine(url, pool_pre_ping=True)
    schema = f"{SCHEMA_PREFIX}{uuid4().hex}"
    scenario = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    confirmed = False
    try:
        with admin.connect() as connection:
            database, user, internal_port, version, user_tables, residual = connection.execute(text(
                "SELECT current_database(),current_user,inet_server_port(),"
                "current_setting('server_version_num')::integer,"
                "(SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
                "('pg_catalog','information_schema')),(SELECT count(*) FROM "
                "pg_namespace WHERE nspname LIKE :prefix)"
            ), {"prefix": f"{SCHEMA_PREFIX}%"}).one()
            # Docker's bridge legitimately exposes PostgreSQL's internal 5432;
            # loopback isolation belongs to the client URL and published port.
            if internal_port != 5432:
                raise RuntimeError("PostgreSQL internal port is not 5432")
            if not 160000 <= version < 170000:
                raise RuntimeError("PostgreSQL server is not version 16")
            expected_identity = (
                environment[EXPECTED_DATABASE_ENV], environment[EXPECTED_USER_ENV]
            )
            if (database, user) != expected_identity:
                raise RuntimeError("live PostgreSQL database or user identity mismatch")
            if user_tables != 0 or residual != 0:
                raise RuntimeError("correction database is not empty or has residual schemas")
        with admin.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {_quoted_schema(schema)}")
        if not _schema_exists(admin, schema):
            raise RuntimeError("correction schema creation was not confirmed")
        confirmed = True
        # Reuse the single, reviewed historical lifecycle source of truth.
        from tests import rfp_approval_postgres_fixture as shared

        historical_seven_kinds = getattr(request, "param", None) == "seven-kinds"
        for migration, seed in _selected_historical_migrations(
            shared, seven_kinds=historical_seven_kinds
        ):
            _run_migration(admin, schema, migration)
            _seed_history(scenario, schema, seed)
        _run_migration(admin, schema, shared.EXTENSION_MIGRATION)
        if not historical_seven_kinds:
            _run_migration(
                admin,
                schema,
                ROOT / "infra/sql/rfp_approval_monthly_storage_volume.sql",
            )
        _run_migration(
            admin,
            schema,
            shared.INDICATIVE_VALIDATION_BUNDLE_MIGRATION,
        )
        with scenario.connect() as connection:
            if connection.execute(text("SELECT current_schema()")).scalar_one() != schema:
                raise RuntimeError("scenario engine selected an unexpected schema")
            public_tables = connection.execute(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            )).scalar_one()
            if public_tables != 0:
                raise RuntimeError("public contains unexpected tables")
        yield IsolatedDatabase(scenario, schema, url)
    except Exception as primary:
        scenario.dispose()
        if confirmed:
            try:
                with admin.begin() as connection:
                    connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
                    connection.exec_driver_sql(
                        f"DROP SCHEMA {_quoted_schema(schema)} CASCADE"
                    )
                if _schema_exists(admin, schema):
                    raise RuntimeError("targeted correction schema cleanup failed")
            except Exception as cleanup:
                admin.dispose()
                raise ExceptionGroup(
                    "Gate 1A scenario and schema cleanup failed",
                    [primary, cleanup],
                ) from None
        admin.dispose()
        raise
    else:
        scenario.dispose()
        if confirmed:
            with admin.begin() as connection:
                connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
                connection.exec_driver_sql(
                    f"DROP SCHEMA {_quoted_schema(schema)} CASCADE"
                )
            if _schema_exists(admin, schema):
                admin.dispose()
                raise RuntimeError("targeted correction schema cleanup failed")
        admin.dispose()


@dataclass(frozen=True)
class Account:
    email: str
    user_id: str
    token: str


@dataclass(frozen=True)
class CorrectionScenario:
    engine: Engine
    database_url: str = field(repr=False)
    schema: str
    client: TestClient
    ticket_id: UUID
    response_run_id: UUID
    approval_run_id: UUID
    revision_id: UUID
    validation_bundle_id: UUID
    content_hash: str
    state_version: int
    department: str
    service_code: str
    pending_code: str
    accounts: dict[str, Account]
    checkpoint: Path
    initial_hitl: bool = False

    @property
    def base(self) -> str:
        return f"/rfps/{self.ticket_id}/approval/{self.approval_run_id}"

    def headers(self, actor: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.accounts[actor].token}"}

    def record_input(self, actor: str | None = None, **overrides):
        actor = actor or self.department
        now = datetime.now(timezone.utc)
        request = {
            "input_id": str(uuid4()),
            "input_kind": "client_price_quote",
            "payload": {
                "input_kind": "client_price_quote",
                "department": self.department,
                "service_code": self.service_code,
                "amount": "12.5000",
                "currency": "USD",
                "unit": "shipment",
                "effective_from": now.date().isoformat(),
                "effective_until": None,
            },
            "provenance": {
                "evidence_type": "approved_quote",
                "evidence_id": str(uuid4()),
                "source_name": "Gate 1A isolated QA authorized quote",
                "issued_at": now.isoformat(),
                "source_payload_hash": canonical_hash({"qa_quote": str(uuid4())}),
            },
            "idempotency_key": str(uuid4()),
            "recorded_at": now.isoformat(),
        }
        request.update(overrides)
        response = self.client.post(
            f"{self.base}/business-inputs",
            headers=self.headers(actor),
            json=request,
        )
        return request, response

    def correction_body(self, input_id: str, **overrides) -> dict[str, object]:
        body: dict[str, object] = {
            "expected_predecessor_status": "blocked_input",
            "expected_predecessor_content_hash": self.content_hash,
            "expected_predecessor_state_version": self.state_version,
            "authorized_input_ids": [input_id],
            "idempotency_key": str(uuid4()),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        body.update(overrides)
        return body

    def correct(self, body, actor: str | None = None):
        actor = actor or self.department
        return self.client.post(
            f"{self.base}/departments/{self.department}/revisions/"
            f"{self.revision_id}/correct",
            headers=self.headers(actor),
            json=body,
        )


@dataclass(frozen=True)
class JointWarehouseReverseScenario:
    reverse: CorrectionScenario
    warehouse_revision_id: UUID
    warehouse_bundle_id: UUID
    warehouse_content_hash: str

    @property
    def engine(self) -> Engine:
        return self.reverse.engine

    @property
    def client(self) -> TestClient:
        return self.reverse.client

    @property
    def base(self) -> str:
        return self.reverse.base

    def headers(self, actor: str) -> dict[str, str]:
        return self.reverse.headers(actor)


def _insert_principal(
    session: Session,
    *,
    account: Account,
    responsible: str | None,
    permission: str,
    department: str | None,
) -> tuple[ResolvedIdentity, AuthorizationReference]:
    principal_id, binding_id, authorization_id = uuid4(), uuid4(), uuid4()
    issuer = "urn:trackflow:qa:local"
    session.execute(insert(rfp_principals).values(
        principal_id=principal_id,
        environment="qa",
        trackflow_user_id=account.user_id,
        responsible_code=responsible,
        synthetic=True,
    ))
    session.execute(insert(rfp_identity_bindings).values(
        binding_id=binding_id,
        principal_id=principal_id,
        environment="qa",
        auth_source="local",
        issuer=issuer,
        subject=account.user_id,
    ))
    session.execute(insert(rfp_authorizations).values(
        authorization_id=authorization_id,
        principal_id=principal_id,
        environment="qa",
        permission=permission,
        department_scope=department,
        target_policy="department_exact" if department else "run",
        scope_fingerprint=canonical_hash({
            "gate": "correction",
            "permission": permission,
            "department": department,
            "authorization_id": str(authorization_id),
        }),
        authorization_version=1,
        active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        granted_by_principal_id=principal_id,
    ))
    identity = ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="local",
        issuer=issuer,
        subject=account.user_id,
        trackflow_user_id=account.user_id,
        general_role="user",
        principal_id=principal_id,
        binding_id=binding_id,
        responsible_code=responsible,
        synthetic=True,
    )
    return identity, AuthorizationReference(authorization_id, 1)


def _insert_additional_authorization(
    session: Session,
    *,
    identity: ResolvedIdentity,
    permission: str,
    department: str | None,
) -> AuthorizationReference:
    """Grant a second exact permission without inventing another identity."""

    authorization_id = uuid4()
    session.execute(insert(rfp_authorizations).values(
        authorization_id=authorization_id,
        principal_id=identity.principal_id,
        environment=identity.environment,
        permission=permission,
        department_scope=department,
        target_policy=("proposal_wide" if department is None else "department_exact"),
        scope_fingerprint=canonical_hash({
            "gate": "correction-hitl",
            "permission": permission,
            "department": department,
            "authorization_id": str(authorization_id),
        }),
        authorization_version=1,
        active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
        granted_by_principal_id=identity.principal_id,
    ))
    return AuthorizationReference(authorization_id, 1)


def _seed_intake(session: Session, ticket_id: UUID, department: str) -> None:
    _seed_intake_departments(session, ticket_id, [department])


def _seed_intake_departments(
    session: Session,
    ticket_id: UUID,
    departments: list[str],
) -> None:
    source_ref = {"line_start": 1, "line_end": 2}
    aspects = {
        "warehouse": {
            "fact": "Client requests warehouse storage in the United States.",
            "requested_action": "Describe the documented storage service.",
        },
        "lastmile": {
            "fact": "Client requests last-mile carrier coverage in the United States.",
            "requested_action": "Describe the available carrier coverage.",
        },
        "reverse": {
            "fact": "Client requests a documented return inspection process.",
            "requested_action": "Describe return inspection and classification.",
        },
    }
    session.execute(insert(rfp_tickets).values(
        ticket_id=ticket_id,
        status="analisis_completo",
        raw_pdf_path=f"gate1a/{ticket_id}/source.pdf",
        markdown_path=f"gate1a/{ticket_id}/source.md",
        uploaded_by_subject="gate1a-qa",
    ))
    session.execute(insert(rfp_metadata).values(
        ticket_id=ticket_id,
        client_name="Gate 1A QA Client",
        client_country_code="US",
        client_country_original="United States",
        operation_country_codes=["US"],
        currency="USD",
        services_requested=[
            {
                "service_id": department,
                "requested": True,
                "source_ref": source_ref,
            }
            for department in departments
        ],
        monthly_volume_value=20,
        monthly_volume_unit="m3",
        monthly_volume_source_ref=source_ref,
        departments_needed=departments,
        readability_metrics={},
        conversion_metrics={},
    ))
    for department in departments:
        aspect = aspects[department]
        session.execute(insert(rfp_department_sections).values(
            ticket_id=ticket_id,
            department_id=department,
            key_aspects=[{
                "fact": aspect["fact"],
                "source_ref": source_ref,
                "requested_action": aspect["requested_action"],
            }],
            open_questions=[],
            source_references=[source_ref],
            detected_conflicts=[],
        ))
    responsibles = {
        "warehouse": "Ana Whitfield",
        "lastmile": "Carlos Vega",
        "reverse": "Sofía Ramos",
    }
    payload = {
        "contract_version": "rfp-intake-v1",
        "ticket_id": str(ticket_id),
        "metadata_reference": str(ticket_id),
        "summary_for_sales": "Isolated QA handoff for correction integration.",
        "selected_departments": departments,
        "department_section_references": [
            {
                "department_id": department,
                "responsible": responsibles[department],
                "ticket_id": str(ticket_id),
            }
            for department in departments
        ],
        "open_questions": [],
        "detected_conflicts": [],
        "created_at": NOW.isoformat(),
    }
    session.execute(insert(rfp_handoffs).values(
        ticket_id=ticket_id,
        contract_version="rfp-intake-v1",
        summary_for_sales=payload["summary_for_sales"],
        payload=payload,
    ))


def _source_draft(
    *,
    response_start,
    extra_pending: bool,
) -> DepartmentDraft:
    department = response_start.selected_departments[0]
    source = generate_department_draft(
        response_start,
        department=department,
        iteration_count=1,
        generated_at=NOW,
    )
    if not extra_pending:
        return source
    pending = [
        *source.pending_commercial_items,
        MissingCommercialInput(
            code="AUTHORIZED_OPERATING_WINDOW_REQUIRED",
            description="The authorized operating window remains missing.",
            owner={
                DepartmentId.warehouse: "Ana Whitfield",
                DepartmentId.lastmile: "Carlos Vega",
                DepartmentId.reverse: "Sofía Ramos",
            }[department],
            blocks_commercial_completeness=True,
        ),
    ]
    return DepartmentDraft.model_validate(
        {
            **source.model_dump(mode="python"),
            "pending_commercial_items": pending,
            "generated_content": (
                f"{source.generated_content}\n"
                "Pending commercial input: AUTHORIZED_OPERATING_WINDOW_REQUIRED."
            ),
        }
    )


def _prepare_joint_warehouse_reverse_scenario(
    *,
    engine: Engine,
    isolated_engine: IsolatedDatabase,
    client: TestClient,
    accounts: dict[str, Account],
    checkpoint: Path,
) -> JointWarehouseReverseScenario:
    departments = [DepartmentId.warehouse, DepartmentId.reverse]
    ticket_id, approval_run_id = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        _seed_intake_departments(
            session, ticket_id, [department.value for department in departments]
        )
        response = start_response_run(
            session,
            ticket_id=ticket_id,
            carrier_model="unknown",
            confirmation_actor=None,
            confirmation_source=None,
        )
    response_run_id = response["run_id"]
    with Session(engine) as session:
        response_start = load_response_start(
            session, run_id=response_run_id, language="es"
        )
    assert response_start.selected_departments == departments

    sources: dict[DepartmentId, DepartmentDraft] = {}
    evaluations = {}
    previews = {}
    source_hashes: dict[DepartmentId, str] = {}
    evaluation_hashes: dict[DepartmentId, str] = {}
    preview_hashes: dict[DepartmentId, str] = {}
    chains = {}
    for department in departments:
        source = generate_department_draft(
            response_start,
            department=department,
            iteration_count=1,
            generated_at=NOW,
        )
        evaluation, _feedback = evaluate_department(
            response_start, source, evaluated_at=NOW
        )
        inspection = inspect_department_safety(
            response_start, source, inspected_at=NOW
        )
        preview = build_safe_preview(source, inspection)
        source_hash = canonical_hash(canonical_value(source))
        evaluation_hash = canonical_hash(canonical_value(evaluation))
        preview_hash = canonical_hash(canonical_value(preview))
        sources[department] = source
        evaluations[department] = evaluation
        previews[department] = preview
        source_hashes[department] = source_hash
        evaluation_hashes[department] = evaluation_hash
        preview_hashes[department] = preview_hash
        chains[department] = _build_chain(
            approval_run_id=approval_run_id,
            handoff=SimpleNamespace(ticket_id=ticket_id),
            department_result=SimpleNamespace(department=department),
            source=(
                source,
                evaluation,
                preview,
                source_hash,
                evaluation_hash,
                preview_hash,
                NOW,
            ),
            execution_id=uuid4(),
        )

    assert evaluations[DepartmentId.warehouse].overall_pass is True
    assert evaluations[DepartmentId.warehouse].terminal_status == "passed"
    assert {
        item.code
        for item in sources[DepartmentId.reverse].pending_commercial_items
    } == {"RETURN_PROCESSING_RATE_REQUIRED"}
    assert evaluations[DepartmentId.reverse].terminal_status == "needs_human_input"
    global_draft, global_evaluation, global_preview = _build_evaluated_global_source(
        response_start,
        [sources[department] for department in departments],
        [evaluations[department] for department in departments],
        require_department_rollup_pass=False,
    )
    assert _global_import_failures(
        global_draft,
        global_evaluation,
        global_preview,
        require_department_rollup_pass=False,
    ) == ()

    handoff = RFPResponseHandoff(
        ticket_id=ticket_id,
        run_id=response_run_id,
        source_handoff_hash=response["source_handoff_hash"],
        outcome="needs_human_review",
        all_sections_passed=False,
        global_compliance_passed=global_evaluation.global_compliance.passed,
        selected_departments=departments,
        department_results=[
            DepartmentHandoffResult(
                department=department,
                terminal_status=evaluations[department].terminal_status,
                iteration_count=1,
                final_draft_id=sources[department].draft_id,
                evaluation_result_id=evaluations[department].evaluation_id,
                safe_preview_id=previews[department].preview_id,
                blocks_commercial_completeness=(
                    evaluations[department].blocks_commercial_completeness
                ),
                requires_human_input=evaluations[department].requires_human_input,
                blocking_rule_ids=evaluations[department].blocking_rule_ids,
            )
            for department in departments
        ],
        global_draft_id=global_draft.draft_id,
        global_evaluation_result_id=global_evaluation.evaluation_id,
        unresolved_items=[
            item
            for department in departments
            for item in sources[department].pending_commercial_items
        ],
        created_at=NOW,
    )
    handoff_payload = canonical_value(handoff)
    handoff_hash = canonical_hash(handoff_payload)

    with Session(engine) as session, session.begin():
        manager_identity, manager_reference = _insert_principal(
            session,
            account=accounts["manager"],
            responsible=None,
            permission="manage_approval_run",
            department=None,
        )
        _ana_identity, _ana_reference = _insert_principal(
            session,
            account=accounts["warehouse"],
            responsible="ana_whitfield",
            permission="approve_department",
            department="warehouse",
        )
        sofia_identity, _sofia_input_reference = _insert_principal(
            session,
            account=accounts["reverse"],
            responsible="sofia_ramos",
            permission="provide_business_input",
            department="reverse",
        )
        _insert_additional_authorization(
            session,
            identity=sofia_identity,
            permission="approve_department",
            department="reverse",
        )
        for department in departments:
            source = sources[department]
            evaluation = evaluations[department]
            preview = previews[department]
            session.execute(insert(rfp_response_drafts).values(
                draft_id=source.draft_id,
                run_id=response_run_id,
                scope_key=department.value,
                iteration_count=1,
                contract_version=source.contract_version,
                payload=canonical_value(source),
                payload_hash=source_hashes[department],
            ))
            session.execute(insert(rfp_response_evaluations).values(
                evaluation_id=evaluation.evaluation_id,
                run_id=response_run_id,
                draft_id=source.draft_id,
                scope_key=department.value,
                contract_version=evaluation.contract_version,
                overall_pass=evaluation.overall_pass,
                terminal_status=evaluation.terminal_status,
                payload=canonical_value(evaluation),
                payload_hash=evaluation_hashes[department],
            ))
            session.execute(insert(rfp_response_previews).values(
                preview_id=preview.preview_id,
                draft_id=source.draft_id,
                gate_status=preview.gate_status,
                safe_content=preview.content,
                reason_codes=preview.reason_codes,
                created_at=NOW,
            ))
        global_draft_payload = canonical_value(global_draft)
        global_evaluation_payload = canonical_value(global_evaluation)
        session.execute(insert(rfp_response_drafts).values(
            draft_id=global_draft.draft_id,
            run_id=response_run_id,
            scope_key="proposal",
            iteration_count=1,
            contract_version=global_draft.contract_version,
            payload=global_draft_payload,
            payload_hash=canonical_hash(global_draft_payload),
        ))
        session.execute(insert(rfp_response_evaluations).values(
            evaluation_id=global_evaluation.evaluation_id,
            run_id=response_run_id,
            draft_id=global_draft.draft_id,
            scope_key="proposal",
            contract_version=global_evaluation.contract_version,
            overall_pass=global_evaluation.overall_pass,
            terminal_status=global_evaluation.terminal_status,
            payload=global_evaluation_payload,
            payload_hash=canonical_hash(global_evaluation_payload),
        ))
        session.execute(insert(rfp_response_previews).values(
            preview_id=global_preview.preview_id,
            draft_id=global_draft.draft_id,
            gate_status=global_preview.gate_status,
            safe_content=global_preview.content,
            reason_codes=global_preview.reason_codes,
            created_at=NOW,
        ))
        session.execute(insert(rfp_response_handoffs).values(
            run_id=response_run_id,
            ticket_id=ticket_id,
            contract_version=handoff.contract_version,
            outcome=handoff.outcome,
            payload=handoff_payload,
            payload_hash=handoff_hash,
            created_at=NOW,
        ))
        session.execute(update(rfp_response_runs).where(
            rfp_response_runs.c.run_id == response_run_id
        ).values(status="needs_human_review"))
        session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id
        ).values(status="needs_human_review"))
        principal = AuthenticatedPrincipal(
            issuer=manager_identity.issuer,
            subject=manager_identity.subject,
            trackflow_user_id=manager_identity.trackflow_user_id,
            auth_source="local",
            general_role="user",
            synthetic=True,
        )
        start = ApprovalRunStart(
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            response_run_id=response_run_id,
            response_handoff_hash=handoff_hash,
            active_departments=departments,
            initial_revisions=[
                InitialRevisionReference(
                    department=department,
                    source_draft_id=sources[department].draft_id,
                    source_draft_hash=source_hashes[department],
                    source_evaluation_id=evaluations[department].evaluation_id,
                    source_evaluation_hash=evaluation_hashes[department],
                    source_part2_iteration=1,
                    terminal_status=evaluations[department].terminal_status,
                )
                for department in departments
            ],
            started_by=RunInitiator(
                principal=principal,
                authorization_id=manager_reference.authorization_id,
                permission="manage_approval_run",
            ),
            started_at=NOW,
        )
        idempotency_key = uuid4()
        start_approval_run(
            session,
            start=start,
            initial_revisions=[chains[department].revision for department in departments],
            identity=manager_identity,
            authorization_reference=manager_reference,
            idempotency_key=idempotency_key,
            request_hash=approval_start_request_hash(
                start,
                [chains[department].revision for department in departments],
                idempotency_key,
            ),
            execution_id=uuid4(),
        )
        for department in departments:
            chain = chains[department]
            producer = _producer_from_chain(chain, environment="qa")
            persist_corrected_draft(session, draft=chain.draft, producer=producer)
            persist_validation_artifact(
                session, artifact=chain.evaluation, producer=producer
            )
            persist_validation_artifact(
                session, artifact=chain.preview, producer=producer
            )
            persist_validation_bundle(session, bundle=chain.bundle, producer=producer)

    with Session(engine) as session:
        revision_rows = {
            row["department_id"]: row
            for row in session.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.approval_run_id
                    == approval_run_id
                )
            ).mappings()
        }
    warehouse_row = revision_rows["warehouse"]
    reverse_row = revision_rows["reverse"]
    assert warehouse_row["status"] == "pending_assessment"
    assert reverse_row["status"] == "blocked_input"
    reverse = CorrectionScenario(
        engine=engine,
        database_url=isolated_engine.url,
        schema=isolated_engine.schema,
        client=client,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        approval_run_id=approval_run_id,
        revision_id=reverse_row["revision_id"],
        validation_bundle_id=reverse_row["validation_bundle_id"],
        content_hash=reverse_row["content_hash"],
        state_version=reverse_row["state_version"],
        department="reverse",
        service_code="return_processing_rate",
        pending_code="RETURN_PROCESSING_RATE_REQUIRED",
        accounts=accounts,
        checkpoint=checkpoint,
        initial_hitl=False,
    )
    return JointWarehouseReverseScenario(
        reverse=reverse,
        warehouse_revision_id=warehouse_row["revision_id"],
        warehouse_bundle_id=warehouse_row["validation_bundle_id"],
        warehouse_content_hash=warehouse_row["content_hash"],
    )


@pytest.fixture(scope="session")
def qa_jwt_secret() -> str:
    return _RedactedSecret(f"gate1a-{uuid4().hex}-{uuid4().hex}")


@pytest.fixture(params=["lastmile"], ids=["lastmile"])
def correction_scenario(
    request,
    isolated_engine: IsolatedDatabase,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    qa_jwt_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CorrectionScenario | JointWarehouseReverseScenario]:
    engine = isolated_engine.engine
    requested_variant = str(request.param)
    extra_pending = requested_variant.endswith("-extra")
    initial_hitl = requested_variant.endswith("-initial-hitl")
    corrected_hitl = requested_variant.endswith("-corrected-hitl")
    response_terminal_status, ticket_terminal_status = _response_terminal_state(
        initial_hitl=initial_hitl
    )
    department = (
        requested_variant.removesuffix("-extra")
        .removesuffix("-initial-hitl")
        .removesuffix("-corrected-hitl")
    )
    monkeypatch.setenv("TRACKFLOW_ENVIRONMENT", "qa")
    monkeypatch.setenv("JWT_SECRET_KEY", qa_jwt_secret)
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    auth_path = tmp_path_factory.getbasetemp() / "gate1a-auth.json"
    monkeypatch.setenv("TRACKFLOW_AUTH_DATABASE_PATH", str(auth_path))
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR", str(tmp_path / "runtime"))
    checkpoint = tmp_path / "runtime" / "approval.sqlite3"
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH", str(checkpoint))
    application_url = str(
        make_url(isolated_engine.url).update_query_dict(
            {"options": f"-csearch_path={isolated_engine.schema}"}
        ).render_as_string(hide_password=False)
    )
    monkeypatch.setenv("DATABASE_URL", application_url)

    # Delayed imports are intentional: collection cannot initialize auth or FastAPI.
    from services.api import auth_database, database as api_database
    from services.api.auth_models import Role, UserCreate, UserUpdate
    from services.api.auth_services import create_user, update_user
    from services.api.main import app

    # The production database module intentionally caches one lazy engine.
    # Resetting that cache is the minimum test-only intervention required to
    # make it consume this scenario's DATABASE_URL; requests still use the
    # real get_db/open_db_session functions and no dependency is overridden.
    previous_application_engine = api_database._engine
    api_database._engine = None

    account_suffix = uuid4().hex
    password = f"Gate1A-{account_suffix}"
    account_specs = {
        "manager": f"gate1a-manager-{account_suffix}@example.invalid",
        "warehouse": f"gate1a-ana-{account_suffix}@example.invalid",
        "lastmile": f"gate1a-carlos-{account_suffix}@example.invalid",
        "reverse": f"gate1a-sofia-{account_suffix}@example.invalid",
        "wrong": f"gate1a-wrong-{account_suffix}@example.invalid",
        "admin": f"gate1a-admin-{account_suffix}@example.invalid",
    }
    client_context = TestClient(app)
    lifecycle = {"entered": False}

    def cleanup_application() -> None:
        cleanup_errors = []
        if lifecycle["entered"]:
            try:
                client_context.__exit__(None, None, None)
            except Exception as error:
                cleanup_errors.append(error)
        try:
            if api_database._engine is not None:
                api_database._engine.dispose()
        except Exception as error:
            cleanup_errors.append(error)
        finally:
            api_database._engine = previous_application_engine
        try:
            auth_database.users.truncate()
            auth_database.profiles.truncate()
            auth_database.password_reset_tokens.truncate()
        except Exception as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            raise ExceptionGroup("Gate 1A application cleanup failed", cleanup_errors)

    request.addfinalizer(cleanup_application)
    users = {
        key: create_user(UserCreate(email=email, password=password))
        for key, email in account_specs.items()
    }
    users["admin"] = update_user(
        users["admin"].id, UserUpdate(role=Role.admin)
    )
    client = client_context.__enter__()
    lifecycle["entered"] = True
    application_engine = api_database._get_engine()
    with application_engine.connect() as connection:
        selected_schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        if selected_schema != isolated_engine.schema:
            raise RuntimeError("FastAPI engine did not select the isolated schema")
    with api_database.open_db_session() as application_session:
        selected_schema = application_session.execute(
            text("SELECT current_schema()")
        ).scalar_one()
        if selected_schema != isolated_engine.schema:
            raise RuntimeError("open_db_session escaped the isolated schema")
    with engine.connect() as connection:
        if connection.execute(text(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        )).scalar_one() != 0:
            raise RuntimeError("FastAPI lifespan wrote tables to public")
    accounts = {}
    for key, user in users.items():
        login = client.post(
            "/auth/login",
            data={"username": user.email, "password": password},
        )
        assert login.status_code == 200
        accounts[key] = Account(user.email, user.id, login.json()["access_token"])

    if requested_variant == "warehouse-reverse-joint":
        yield _prepare_joint_warehouse_reverse_scenario(
            engine=engine,
            isolated_engine=isolated_engine,
            client=client,
            accounts=accounts,
            checkpoint=checkpoint,
        )
        return

    ticket_id = uuid4()
    with Session(engine) as session, session.begin():
        _seed_intake(session, ticket_id, department)
        response = start_response_run(
            session,
            ticket_id=ticket_id,
            carrier_model=(
                "customer_managed"
                if initial_hitl and department == "lastmile"
                else "unknown"
            ),
            confirmation_actor=(
                "Gate 1B QA fixture"
                if initial_hitl and department == "lastmile"
                else None
            ),
            confirmation_source=(
                "gate1b_initial_hitl"
                if initial_hitl and department == "lastmile"
                else None
            ),
        )
    response_run_id = response["run_id"]
    approval_run_id = uuid4()
    with Session(engine) as session:
        response_start = load_response_start(
            session,
            run_id=response_run_id,
            language=("es" if initial_hitl else "en"),
        )
    source = _source_draft(
        response_start=response_start,
        extra_pending=extra_pending,
    )
    source_payload = canonical_value(source)
    source_hash = canonical_hash(source_payload)
    evaluation, _feedback = evaluate_department(
        response_start, source, evaluated_at=NOW
    )
    if initial_hitl and (
        evaluation.terminal_status != "passed"
        or not evaluation.overall_pass
        or evaluation.blocks_commercial_completeness
        or evaluation.requires_human_input
        or evaluation.blocking_rule_ids
    ):
        raise RuntimeError(
            "initial HITL fixture did not pass the real Part 2 evaluator"
        )
    inspection = inspect_department_safety(
        response_start, source, inspected_at=NOW
    )
    preview = build_safe_preview(source, inspection)
    evaluation_id = evaluation.evaluation_id
    evaluation_payload = canonical_value(evaluation)
    evaluation_hash = canonical_hash(evaluation_payload)
    preview_id = preview.preview_id
    preview_hash = canonical_hash(canonical_value(preview))
    global_draft, global_evaluation, global_preview = (
        _build_evaluated_global_source(
            response_start,
            source,
            evaluation,
            require_department_rollup_pass=initial_hitl,
        )
    )
    chain_execution_id = uuid4()
    chain = _build_chain(
        approval_run_id=approval_run_id,
        handoff=SimpleNamespace(ticket_id=ticket_id),
        department_result=SimpleNamespace(department=DepartmentId(department)),
        source=(
            source,
            evaluation,
            preview,
            source_hash,
            evaluation_hash,
            preview_hash,
            NOW,
        ),
        execution_id=chain_execution_id,
    )
    handoff = RFPResponseHandoff(
        ticket_id=ticket_id,
        run_id=response_run_id,
        source_handoff_hash=response["source_handoff_hash"],
        outcome=response_terminal_status,
        all_sections_passed=(
            evaluation.terminal_status == "passed"
            and evaluation.overall_pass
            and not evaluation.blocks_commercial_completeness
            and not evaluation.requires_human_input
            and not evaluation.blocking_rule_ids
        ),
        global_compliance_passed=global_evaluation.global_compliance.passed,
        selected_departments=[DepartmentId(department)],
        department_results=[DepartmentHandoffResult(
            department=DepartmentId(department),
            terminal_status=evaluation.terminal_status,
            iteration_count=1,
            final_draft_id=source.draft_id,
            evaluation_result_id=evaluation_id,
            safe_preview_id=preview_id,
            blocks_commercial_completeness=(
                evaluation.blocks_commercial_completeness
            ),
            requires_human_input=evaluation.requires_human_input,
            blocking_rule_ids=evaluation.blocking_rule_ids,
        )],
        global_draft_id=global_draft.draft_id,
        global_evaluation_result_id=global_evaluation.evaluation_id,
        unresolved_items=source.pending_commercial_items,
        created_at=NOW,
    )
    handoff_payload = canonical_value(handoff)
    handoff_hash = canonical_hash(handoff_payload)

    responsible = {
        "warehouse": "ana_whitfield",
        "lastmile": "carlos_vega",
        "reverse": "sofia_ramos",
    }[department]
    with Session(engine) as session, session.begin():
        manager_identity, manager_reference = _insert_principal(
            session,
            account=accounts["manager"],
            responsible=None,
            permission="manage_approval_run",
            department=None,
        )
        department_identity, _department_reference = _insert_principal(
            session,
            account=accounts[department],
            responsible=responsible,
            permission=("approve_department" if initial_hitl else "provide_business_input"),
            department=department,
        )
        if corrected_hitl:
            _insert_additional_authorization(
                session,
                identity=department_identity,
                permission="approve_department",
                department=department,
            )
        wrong_responsible, wrong_department = {
            "warehouse": ("carlos_vega", "lastmile"),
            "lastmile": ("sofia_ramos", "reverse"),
            "reverse": ("carlos_vega", "lastmile"),
        }[department]
        _insert_principal(
            session,
            account=accounts["wrong"],
            responsible=wrong_responsible,
            permission=(
                "approve_department"
                if initial_hitl or corrected_hitl
                else "provide_business_input"
            ),
            department=wrong_department,
        )
        session.execute(insert(rfp_response_drafts).values(
            draft_id=source.draft_id,
            run_id=response_run_id,
            scope_key=department,
            iteration_count=1,
            contract_version=source.contract_version,
            payload=source_payload,
            payload_hash=source_hash,
        ))
        session.execute(insert(rfp_response_evaluations).values(
            evaluation_id=evaluation_id,
            run_id=response_run_id,
            draft_id=source.draft_id,
            scope_key=department,
            contract_version="section-evaluation-v1",
            overall_pass=evaluation.overall_pass,
            terminal_status=evaluation.terminal_status,
            payload=evaluation_payload,
            payload_hash=evaluation_hash,
        ))
        session.execute(insert(rfp_response_previews).values(
            preview_id=preview.preview_id,
            draft_id=source.draft_id,
            gate_status=preview.gate_status,
            safe_content=preview.content,
            reason_codes=preview.reason_codes,
            created_at=NOW,
        ))
        global_draft_payload = canonical_value(global_draft)
        global_evaluation_payload = canonical_value(global_evaluation)
        session.execute(insert(rfp_response_drafts).values(
            draft_id=global_draft.draft_id,
            run_id=response_run_id,
            scope_key="proposal",
            iteration_count=1,
            contract_version=global_draft.contract_version,
            payload=global_draft_payload,
            payload_hash=canonical_hash(global_draft_payload),
        ))
        session.execute(insert(rfp_response_evaluations).values(
            evaluation_id=global_evaluation.evaluation_id,
            run_id=response_run_id,
            draft_id=global_draft.draft_id,
            scope_key="proposal",
            contract_version=global_evaluation.contract_version,
            overall_pass=global_evaluation.overall_pass,
            terminal_status=global_evaluation.terminal_status,
            payload=global_evaluation_payload,
            payload_hash=canonical_hash(global_evaluation_payload),
        ))
        session.execute(insert(rfp_response_previews).values(
            preview_id=global_preview.preview_id,
            draft_id=global_draft.draft_id,
            gate_status=global_preview.gate_status,
            safe_content=global_preview.content,
            reason_codes=global_preview.reason_codes,
            created_at=NOW,
        ))
        session.execute(insert(rfp_response_handoffs).values(
            run_id=response_run_id,
            ticket_id=ticket_id,
            contract_version=handoff.contract_version,
            outcome=handoff.outcome,
            payload=handoff_payload,
            payload_hash=handoff_hash,
            created_at=NOW,
        ))
        session.execute(update(rfp_response_runs).where(
            rfp_response_runs.c.run_id == response_run_id
        ).values(status=response_terminal_status))
        session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id
        ).values(status=ticket_terminal_status))
        principal = AuthenticatedPrincipal(
            issuer=manager_identity.issuer,
            subject=manager_identity.subject,
            trackflow_user_id=manager_identity.trackflow_user_id,
            auth_source="local",
            general_role="user",
            synthetic=True,
        )
        start = ApprovalRunStart(
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            response_run_id=response_run_id,
            response_handoff_hash=handoff_hash,
            active_departments=[DepartmentId(department)],
            initial_revisions=[InitialRevisionReference(
                department=DepartmentId(department),
                source_draft_id=source.draft_id,
                source_draft_hash=source_hash,
                source_evaluation_id=evaluation_id,
                source_evaluation_hash=evaluation_hash,
                source_part2_iteration=1,
                terminal_status=evaluation.terminal_status,
            )],
            started_by=RunInitiator(
                principal=principal,
                authorization_id=manager_reference.authorization_id,
                permission="manage_approval_run",
            ),
            started_at=NOW,
        )
        idempotency_key = uuid4()
        persisted_response = session.execute(
            select(
                rfp_response_runs.c.run_id,
                rfp_response_runs.c.ticket_id,
                rfp_response_runs.c.status,
            ).where(rfp_response_runs.c.run_id == response_run_id)
        ).mappings().one()
        persisted_ticket_status = session.execute(
            select(rfp_tickets.c.status).where(
                rfp_tickets.c.ticket_id == ticket_id
            )
        ).scalar_one()
        persisted_handoff = session.execute(
            select(
                rfp_response_handoffs.c.run_id,
                rfp_response_handoffs.c.ticket_id,
                rfp_response_handoffs.c.outcome,
                rfp_response_handoffs.c.payload,
                rfp_response_handoffs.c.payload_hash,
            ).where(rfp_response_handoffs.c.run_id == response_run_id)
        ).mappings().one()
        persisted_evaluation = session.execute(
            select(
                rfp_response_evaluations.c.evaluation_id,
                rfp_response_evaluations.c.draft_id,
                rfp_response_evaluations.c.overall_pass,
                rfp_response_evaluations.c.terminal_status,
                rfp_response_evaluations.c.payload,
                rfp_response_evaluations.c.payload_hash,
            ).where(
                rfp_response_evaluations.c.evaluation_id == evaluation_id,
                rfp_response_evaluations.c.run_id == response_run_id,
            )
        ).mappings().one()
        persisted_handoff_contract = RFPResponseHandoff.model_validate(
            persisted_handoff["payload"], strict=False
        )
        if (
            persisted_response["run_id"] != response_run_id
            or persisted_response["ticket_id"] != ticket_id
            or persisted_response["status"] != response_terminal_status
            or persisted_ticket_status != ticket_terminal_status
            or persisted_handoff["run_id"] != response_run_id
            or persisted_handoff["ticket_id"] != ticket_id
            or persisted_handoff["outcome"] != response_terminal_status
            or persisted_handoff_contract.run_id != response_run_id
            or persisted_handoff_contract.ticket_id != ticket_id
            or persisted_handoff_contract.outcome != response_terminal_status
            or canonical_hash(persisted_handoff["payload"])
            != persisted_handoff["payload_hash"]
            or persisted_evaluation["evaluation_id"] != evaluation_id
            or persisted_evaluation["draft_id"] != source.draft_id
            or persisted_evaluation["overall_pass"] != evaluation.overall_pass
            or persisted_evaluation["terminal_status"]
            != evaluation.terminal_status
            or canonical_hash(persisted_evaluation["payload"])
            != persisted_evaluation["payload_hash"]
            or persisted_evaluation["payload_hash"] != evaluation_hash
            or persisted_handoff_contract.department_results[0].evaluation_result_id
            != evaluation_id
            or persisted_handoff_contract.department_results[0].terminal_status
            != evaluation.terminal_status
        ):
            raise RuntimeError(
                "fixture response run, handoff and Part 2 evaluation are inconsistent"
            )
        start_approval_run(
            session,
            start=start,
            initial_revisions=[chain.revision],
            identity=manager_identity,
            authorization_reference=manager_reference,
            idempotency_key=idempotency_key,
            request_hash=approval_start_request_hash(
                start, [chain.revision], idempotency_key
            ),
            execution_id=uuid4(),
        )
        producer = _producer_from_chain(chain, environment="qa")
        persist_corrected_draft(session, draft=chain.draft, producer=producer)
        persist_validation_artifact(session, artifact=chain.evaluation, producer=producer)
        persist_validation_artifact(session, artifact=chain.preview, producer=producer)
        persist_validation_bundle(session, bundle=chain.bundle, producer=producer)

    with Session(engine) as session:
        row = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == chain.revision.revision_id
        )).mappings().one()
    scenario = CorrectionScenario(
        engine,
        isolated_engine.url,
        isolated_engine.schema,
        client,
        ticket_id,
        response_run_id,
        approval_run_id,
        chain.revision.revision_id,
        chain.bundle.validation_bundle_id,
        chain.revision.content_hash,
        row["state_version"],
        department,
        {
            "warehouse": "storage_rate_us",
            "lastmile": "last_mile_rate",
            "reverse": "return_processing_rate",
        }[department],
        {
            "warehouse": "WAREHOUSE_RATE_REQUIRED",
            "lastmile": "LAST_MILE_RATE_REQUIRED",
            "reverse": "RETURN_PROCESSING_RATE_REQUIRED",
        }[department],
        accounts,
        checkpoint,
        initial_hitl,
    )
    yield scenario


def _snapshot(engine: Engine, run_id: UUID, ticket_id: UUID) -> dict[str, str]:
    run_tables = {
        "inputs": rfp_authorized_business_inputs,
        "drafts": rfp_approval_corrected_drafts,
        "artifacts": rfp_approval_validation_artifacts,
        "bundles": rfp_approval_validation_bundles,
        "revisions": rfp_approval_department_revisions,
        "assessments": rfp_conflict_assessments,
        "conflicts": rfp_arbitration_conflicts,
        "decisions": rfp_human_decisions,
        "events": rfp_workflow_events,
    }

    def digest(rows) -> str:
        return canonical_hash(sorted(canonical_hash(dict(row)) for row in rows))

    with Session(engine) as session:
        result = {
            name: digest(session.execute(select(table).where(
                table.c.approval_run_id == run_id
            )).mappings().all())
            for name, table in run_tables.items()
        }
        result["ticket"] = digest(session.execute(select(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id
        )).mappings().all())
        result["approval_run"] = digest(session.execute(select(rfp_approval_runs).where(
            rfp_approval_runs.c.approval_run_id == run_id
        )).mappings().all())
        return result


def _authorized_correction(scenario: CorrectionScenario):
    request, recorded = scenario.record_input()
    assert recorded.status_code == 200, recorded.text
    body = scenario.correction_body(recorded.json()["input_id"])
    corrected = scenario.correct(body)
    assert corrected.status_code == 200, corrected.text
    return request, recorded.json(), body, corrected.json()


def _assert_initial_department_releases_to_real_http_hitl(
    scenario: CorrectionScenario,
) -> None:
    """Prove initial JSONB release, interrupt, scoped inbox and durable resume."""

    assert scenario.initial_hitl is True
    department = scenario.department
    independent = create_engine(
        scenario.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={scenario.schema}"},
    )
    decision_url = (
        f"{scenario.base}/departments/{department}/revisions/"
        f"{scenario.revision_id}/decision"
    )
    try:
        with independent.connect() as connection:
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == (
                scenario.schema
            )
            initial = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id
                    == scenario.revision_id
                )
            ).mappings().one()
            bundle = connection.execute(
                select(rfp_approval_validation_bundles).where(
                    rfp_approval_validation_bundles.c.validation_bundle_id
                    == scenario.validation_bundle_id
                )
            ).mappings().one()
            response_run = connection.execute(
                select(rfp_response_runs).where(
                    rfp_response_runs.c.run_id == scenario.response_run_id
                )
            ).mappings().one()
            handoff_row = connection.execute(
                select(rfp_response_handoffs).where(
                    rfp_response_handoffs.c.run_id == scenario.response_run_id
                )
            ).mappings().one()
            handoff = RFPResponseHandoff.model_validate(
                handoff_row["payload"], strict=False
            )
            global_draft_row = connection.execute(
                select(rfp_response_drafts).where(
                    rfp_response_drafts.c.draft_id == handoff.global_draft_id,
                    rfp_response_drafts.c.scope_key == "proposal",
                )
            ).mappings().one()
            global_evaluation_row = connection.execute(
                select(rfp_response_evaluations).where(
                    rfp_response_evaluations.c.evaluation_id
                    == handoff.global_evaluation_result_id,
                    rfp_response_evaluations.c.draft_id
                    == handoff.global_draft_id,
                    rfp_response_evaluations.c.scope_key == "proposal",
                )
            ).mappings().one()
            global_preview_row = connection.execute(
                select(rfp_response_previews).where(
                    rfp_response_previews.c.draft_id == handoff.global_draft_id
                )
            ).mappings().one()
            persisted_global_draft = GlobalCommercialTermsDraft.model_validate(
                global_draft_row["payload"], strict=False
            )
            persisted_global_evaluation = ProposalEvaluationResult.model_validate(
                global_evaluation_row["payload"], strict=False
            )
            persisted_global_preview = SimpleNamespace(
                gate_status=global_preview_row["gate_status"],
                content=global_preview_row["safe_content"],
            )
            assert initial["status"] == "pending_assessment"
            assert initial["content_payload"]["creation_reason"] == "part2_import"
            assert initial["is_current"] is True
            assert bundle["eligible"] is True
            assert bundle["content_hash"] == initial["content_hash"]
            assert bundle["revision_validation_hash"] == initial["validation_hash"]
            assert response_run["status"] == "ready_for_part3"
            assert handoff.outcome == "ready_for_part3"
            assert handoff.selected_departments == [DepartmentId(department)]
            assert persisted_global_draft.source_section_ids == [
                initial["source_part2_draft_id"]
            ]
            assert canonical_hash(global_draft_row["payload"]) == (
                global_draft_row["payload_hash"]
            )
            assert canonical_hash(global_evaluation_row["payload"]) == (
                global_evaluation_row["payload_hash"]
            )
            assert persisted_global_evaluation.global_draft_id == (
                persisted_global_draft.draft_id
            )
            assert _global_import_failures(
                persisted_global_draft,
                persisted_global_evaluation,
                persisted_global_preview,
            ) == ()
            assert connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(rfp_human_decisions.c.revision_id == scenario.revision_id)
            ).scalar_one() == 0

        released = scenario.client.post(
            f"{scenario.base}/assess-and-start",
            headers=scenario.headers("manager"),
        )
        assert released.status_code == 200, released.text
        ready = released.json()["ready_revisions"]
        assert ready == [
            {
                "department": department,
                "revision_id": str(scenario.revision_id),
                "status": "pending_approval",
                "validation_bundle_id": str(scenario.validation_bundle_id),
                "ready_for_hitl": True,
            }
        ]
        assert scenario.checkpoint.is_file()
        workflow_state = scenario.client.get(
            f"{scenario.base}/departments/{department}/revisions/"
            f"{scenario.revision_id}",
            headers=scenario.headers(department),
        )
        assert workflow_state.status_code == 200, workflow_state.text
        workflow_payload = workflow_state.json()
        assert workflow_payload["interrupted"] is True
        assert workflow_payload["status"] == "awaiting_human_decision"
        assert workflow_payload["thread_id"] == (
            f"rfp-{scenario.ticket_id}:{department}:{scenario.approval_run_id}:"
            f"{scenario.revision_id}"
        )
        assert workflow_payload["target"] == {
            "approval_run_id": str(scenario.approval_run_id),
            "ticket_id": str(scenario.ticket_id),
            "revision_id": str(scenario.revision_id),
            "department": department,
        }

        with independent.connect() as connection:
            released_revision = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id
                    == scenario.revision_id
                )
            ).mappings().one()
            assert released_revision["status"] == "pending_approval"
            assert released_revision["validation_bundle_id"] == (
                scenario.validation_bundle_id
            )
            assert released_revision["state_version"] == initial["state_version"] + 1
            assert connection.execute(
                select(func.count())
                .select_from(rfp_workflow_events)
                .where(
                    rfp_workflow_events.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_workflow_events.c.department_revision_id
                    == scenario.revision_id,
                    rfp_workflow_events.c.event_type
                    == "initial_validation_bundle_linked",
                )
            ).scalar_one() == 1

        authorized_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers(department)
        )
        assert authorized_inbox.status_code == 200, authorized_inbox.text
        assert [
            item["revision_id"] for item in authorized_inbox.json()["items"]
        ] == [
            str(scenario.revision_id)
        ]
        wrong_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers("wrong")
        )
        assert wrong_inbox.status_code == 200, wrong_inbox.text
        assert all(
            item["revision_id"] != str(scenario.revision_id)
            for item in wrong_inbox.json()["items"]
        )

        denied = scenario.client.post(
            decision_url,
            headers=scenario.headers("wrong"),
            json={"action": "approve", "reason": None},
        )
        assert denied.status_code == 403, denied.text

        with independent.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(rfp_human_decisions.c.revision_id == scenario.revision_id)
            ).scalar_one() == 0

        approved = scenario.client.post(
            decision_url,
            headers=scenario.headers(department),
            json={"action": "approve", "reason": None},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["interrupted"] is False
        assert approved.json()["application"]["revision_status"] == "approved"
        with independent.connect() as connection:
            persisted = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id
                    == scenario.revision_id
                )
            ).mappings().one()
            decisions = connection.execute(
                select(rfp_human_decisions).where(
                    rfp_human_decisions.c.revision_id == scenario.revision_id
                )
            ).mappings().all()
            event_counts = dict(
                connection.execute(
                    select(
                        rfp_workflow_events.c.event_type,
                        func.count().label("count"),
                    )
                    .where(
                        rfp_workflow_events.c.approval_run_id
                        == scenario.approval_run_id,
                        rfp_workflow_events.c.department_revision_id
                        == scenario.revision_id,
                    )
                    .group_by(rfp_workflow_events.c.event_type)
                ).all()
            )
            assert persisted["status"] == "approved"
            assert persisted["content_hash"] == initial["content_hash"]
            assert persisted["validation_bundle_id"] == scenario.validation_bundle_id
            assert len(decisions) == 1
            assert decisions[0]["processing_status"] == "applied"
            assert persisted["applied_decision_id"] == decisions[0]["decision_id"]
            assert event_counts["initial_validation_bundle_linked"] == 1
            assert event_counts["human_decision_queued"] == 1
            assert event_counts["human_decision_applied"] == 1

        replay = scenario.client.post(
            decision_url,
            headers=scenario.headers(department),
            json={"action": "approve", "reason": None},
        )
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"] == "workflow_not_interrupted"
        with independent.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(rfp_human_decisions.c.revision_id == scenario.revision_id)
            ).scalar_one() == 1
            assert connection.execute(
                select(func.count())
                .select_from(rfp_workflow_events)
                .where(
                    rfp_workflow_events.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_workflow_events.c.department_revision_id
                    == scenario.revision_id,
                    rfp_workflow_events.c.event_type == "human_decision_applied",
                )
            ).scalar_one() == 1
    finally:
        independent.dispose()


@pytest.mark.parametrize(
    "correction_scenario", ["lastmile-initial-hitl"], indirect=True
)
def test_initial_lastmile_revision_releases_to_real_http_hitl(
    correction_scenario: CorrectionScenario,
) -> None:
    """Retain the already accredited Carlos/Last Mile gate unchanged in scope."""

    _assert_initial_department_releases_to_real_http_hitl(correction_scenario)


@pytest.mark.parametrize(
    "correction_scenario", ["warehouse-initial-hitl"], indirect=True
)
def test_initial_warehouse_revision_releases_to_ana_http_hitl(
    correction_scenario: CorrectionScenario,
) -> None:
    """Prove Ana's Warehouse branch through the same persisted HITL mechanism."""

    _assert_initial_department_releases_to_real_http_hitl(correction_scenario)


@pytest.mark.parametrize(
    "correction_scenario", ["reverse-corrected-hitl"], indirect=True
)
def test_corrected_reverse_revision_releases_to_sofia_http_hitl(
    correction_scenario: CorrectionScenario,
) -> None:
    """Resolve Reverse's real commercial pending item before Sofía's HITL."""

    scenario = correction_scenario
    assert scenario.department == "reverse"
    assert scenario.initial_hitl is False
    _request, recorded, _body, corrected = _authorized_correction(scenario)
    revision_id = UUID(corrected["revision_id"])
    assert corrected["status"] == "pending_approval"
    assert corrected["remaining_input_codes"] == []

    independent = create_engine(
        scenario.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={scenario.schema}"},
    )
    decision_url = (
        f"{scenario.base}/departments/reverse/revisions/{revision_id}/decision"
    )
    try:
        with independent.connect() as connection:
            rows = connection.execute(
                select(rfp_approval_department_revisions)
                .where(
                    rfp_approval_department_revisions.c.approval_run_id
                    == scenario.approval_run_id
                )
                .order_by(
                    rfp_approval_department_revisions.c.review_revision_no
                )
            ).mappings().all()
            assert [row["review_revision_no"] for row in rows] == [1, 2]
            assert rows[0]["revision_id"] == scenario.revision_id
            assert rows[0]["status"] == "superseded"
            assert rows[0]["is_current"] is False
            assert rows[1]["revision_id"] == revision_id
            assert rows[1]["status"] == "pending_approval"
            assert rows[1]["is_current"] is True
            assert rows[1]["supersedes_revision_id"] == scenario.revision_id
            assert rows[1]["validation_bundle_id"] is not None
            assert connection.execute(
                select(func.count())
                .select_from(rfp_authorized_business_inputs)
                .where(
                    rfp_authorized_business_inputs.c.input_id
                    == UUID(recorded["input_id"])
                )
            ).scalar_one() == 1
            bundle_id = rows[1]["validation_bundle_id"]
            content_hash = rows[1]["content_hash"]

        started = scenario.client.post(
            f"{scenario.base}/assess-and-start",
            headers=scenario.headers("manager"),
        )
        assert started.status_code == 200, started.text
        assert started.json()["ready_revisions"] == [{
            "department": "reverse",
            "revision_id": str(revision_id),
            "status": "pending_approval",
            "validation_bundle_id": str(bundle_id),
            "ready_for_hitl": True,
        }]

        state = scenario.client.get(
            f"{scenario.base}/departments/reverse/revisions/{revision_id}",
            headers=scenario.headers("reverse"),
        )
        assert state.status_code == 200, state.text
        assert state.json()["interrupted"] is True
        assert state.json()["status"] == "awaiting_human_decision"

        sofia_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers("reverse")
        )
        assert sofia_inbox.status_code == 200, sofia_inbox.text
        assert [item["revision_id"] for item in sofia_inbox.json()["items"]] == [
            str(revision_id)
        ]
        wrong_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers("wrong")
        )
        assert wrong_inbox.status_code == 200, wrong_inbox.text
        assert all(
            item["revision_id"] != str(revision_id)
            for item in wrong_inbox.json()["items"]
        )
        denied = scenario.client.post(
            decision_url,
            headers=scenario.headers("wrong"),
            json={"action": "approve", "reason": None},
        )
        assert denied.status_code == 403, denied.text

        before_predecessor_reuse = _snapshot(
            scenario.engine, scenario.approval_run_id, scenario.ticket_id
        )
        with independent.connect() as connection:
            decisions_before = connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(
                    rfp_human_decisions.c.revision_id.in_(
                        [scenario.revision_id, revision_id]
                    )
                )
            ).scalar_one()
            applications_before = connection.execute(
                select(func.count())
                .select_from(rfp_workflow_events)
                .where(
                    rfp_workflow_events.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_workflow_events.c.event_type == "human_decision_applied",
                )
            ).scalar_one()

        # A never received a HITL thread.  This proves absence of a thread for
        # the superseded predecessor, not stale-checkpoint rejection.
        predecessor_reuse = scenario.client.post(
            f"{scenario.base}/departments/reverse/revisions/"
            f"{scenario.revision_id}/decision",
            headers=scenario.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert predecessor_reuse.status_code == 404, predecessor_reuse.text
        assert predecessor_reuse.json()["detail"] == "workflow_thread_not_found"
        assert _snapshot(
            scenario.engine, scenario.approval_run_id, scenario.ticket_id
        ) == before_predecessor_reuse
        with independent.connect() as connection:
            predecessor = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id
                    == scenario.revision_id
                )
            ).mappings().one()
            successor = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id == revision_id
                )
            ).mappings().one()
            assert predecessor["status"] == "superseded"
            assert predecessor["is_current"] is False
            assert successor["status"] == "pending_approval"
            assert successor["is_current"] is True
            assert connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(
                    rfp_human_decisions.c.revision_id.in_(
                        [scenario.revision_id, revision_id]
                    )
                )
            ).scalar_one() == decisions_before
            assert connection.execute(
                select(func.count())
                .select_from(rfp_workflow_events)
                .where(
                    rfp_workflow_events.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_workflow_events.c.event_type == "human_decision_applied",
                )
            ).scalar_one() == applications_before

        approved = scenario.client.post(
            decision_url,
            headers=scenario.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["interrupted"] is False
        assert approved.json()["application"]["revision_status"] == "approved"
        assert approved.json()["coordination"]["outcome"] == "waiting"
        assert approved.json()["coordination"]["final_document_id"] is None
        assert approved.json()["coordination"]["blockers"]

        with independent.connect() as connection:
            persisted = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id == revision_id
                )
            ).mappings().one()
            decisions = connection.execute(
                select(rfp_human_decisions).where(
                    rfp_human_decisions.c.revision_id == revision_id
                )
            ).mappings().all()
            assert persisted["status"] == "approved"
            assert persisted["content_hash"] == content_hash
            assert persisted["validation_bundle_id"] == bundle_id
            assert len(decisions) == 1
            assert decisions[0]["processing_status"] == "applied"
            assert persisted["applied_decision_id"] == decisions[0]["decision_id"]

        replay = scenario.client.post(
            decision_url,
            headers=scenario.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"] == "workflow_not_interrupted"
        with independent.connect() as connection:
            assert connection.execute(
                select(func.count())
                .select_from(rfp_human_decisions)
                .where(rfp_human_decisions.c.revision_id == revision_id)
            ).scalar_one() == 1
            assert connection.execute(
                select(func.count())
                .select_from(rfp_workflow_events)
                .where(
                    rfp_workflow_events.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_workflow_events.c.department_revision_id == revision_id,
                    rfp_workflow_events.c.event_type
                    == "human_decision_applied",
                )
            ).scalar_one() == 1
    finally:
        independent.dispose()


@pytest.mark.parametrize(
    "correction_scenario", ["warehouse-reverse-joint"], indirect=True
)
def test_joint_warehouse_reverse_http_hitl_branches_are_independent(
    correction_scenario: JointWarehouseReverseScenario,
) -> None:
    scenario = correction_scenario
    reverse = scenario.reverse
    _request, recorded, _body, corrected = _authorized_correction(reverse)
    reverse_b_id = UUID(corrected["revision_id"])
    assert corrected["status"] == "pending_approval"
    assert corrected["remaining_input_codes"] == []

    independent = create_engine(
        reverse.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={reverse.schema}"},
    )
    warehouse_url = (
        f"{scenario.base}/departments/warehouse/revisions/"
        f"{scenario.warehouse_revision_id}/decision"
    )
    reverse_url = (
        f"{scenario.base}/departments/reverse/revisions/{reverse_b_id}/decision"
    )
    try:
        with independent.connect() as connection:
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == (
                reverse.schema
            )
            revisions = connection.execute(
                select(rfp_approval_department_revisions)
                .where(
                    rfp_approval_department_revisions.c.approval_run_id
                    == reverse.approval_run_id
                )
                .order_by(
                    rfp_approval_department_revisions.c.department_id,
                    rfp_approval_department_revisions.c.review_revision_no,
                )
            ).mappings().all()
            assert {
                (row["department_id"], row["review_revision_no"]): (
                    row["status"], row["is_current"]
                )
                for row in revisions
            } == {
                ("reverse", 1): ("superseded", False),
                ("reverse", 2): ("pending_approval", True),
                ("warehouse", 1): ("pending_assessment", True),
            }
            reverse_b = next(
                row for row in revisions if row["revision_id"] == reverse_b_id
            )
            assert reverse_b["supersedes_revision_id"] == reverse.revision_id
            assert reverse_b["validation_bundle_id"] is not None
            assert connection.execute(
                select(func.count())
                .select_from(rfp_authorized_business_inputs)
                .where(
                    rfp_authorized_business_inputs.c.input_id
                    == UUID(recorded["input_id"])
                )
            ).scalar_one() == 1

        started = scenario.client.post(
            f"{scenario.base}/assess-and-start",
            headers=scenario.headers("manager"),
        )
        assert started.status_code == 200, started.text
        ready = {
            (item["department"], UUID(item["revision_id"])): item
            for item in started.json()["ready_revisions"]
        }
        assert set(ready) == {
            ("warehouse", scenario.warehouse_revision_id),
            ("reverse", reverse_b_id),
        }
        assert all(
            item["status"] == "pending_approval" and item["ready_for_hitl"] is True
            for item in ready.values()
        )

        warehouse_state = scenario.client.get(
            f"{scenario.base}/departments/warehouse/revisions/"
            f"{scenario.warehouse_revision_id}",
            headers=scenario.headers("warehouse"),
        )
        reverse_state = scenario.client.get(
            f"{scenario.base}/departments/reverse/revisions/{reverse_b_id}",
            headers=scenario.headers("reverse"),
        )
        assert warehouse_state.status_code == 200, warehouse_state.text
        assert reverse_state.status_code == 200, reverse_state.text
        warehouse_workflow = warehouse_state.json()
        reverse_workflow = reverse_state.json()
        assert warehouse_workflow["interrupted"] is True
        assert reverse_workflow["interrupted"] is True
        assert warehouse_workflow["status"] == "awaiting_human_decision"
        assert reverse_workflow["status"] == "awaiting_human_decision"
        assert warehouse_workflow["thread_id"] != reverse_workflow["thread_id"]
        for workflow, department, revision_id in (
            (warehouse_workflow, "warehouse", scenario.warehouse_revision_id),
            (reverse_workflow, "reverse", reverse_b_id),
        ):
            assert workflow["target"] == {
                "approval_run_id": str(reverse.approval_run_id),
                "ticket_id": str(reverse.ticket_id),
                "revision_id": str(revision_id),
                "department": department,
            }

        ana_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers("warehouse")
        )
        sofia_inbox = scenario.client.get(
            "/rfps/approval/inbox", headers=scenario.headers("reverse")
        )
        assert ana_inbox.status_code == 200, ana_inbox.text
        assert sofia_inbox.status_code == 200, sofia_inbox.text
        assert [item["revision_id"] for item in ana_inbox.json()["items"]] == [
            str(scenario.warehouse_revision_id)
        ]
        assert [item["revision_id"] for item in sofia_inbox.json()["items"]] == [
            str(reverse_b_id)
        ]

        before_denials = _snapshot(
            scenario.engine, reverse.approval_run_id, reverse.ticket_id
        )
        cross_denials = (
            scenario.client.post(
                reverse_url,
                headers=scenario.headers("warehouse"),
                json={"action": "approve", "reason": None},
            ),
            scenario.client.post(
                warehouse_url,
                headers=scenario.headers("reverse"),
                json={"action": "approve", "reason": None},
            ),
            scenario.client.post(
                warehouse_url,
                headers=scenario.headers("admin"),
                json={"action": "approve", "reason": None},
            ),
        )
        assert [response.status_code for response in cross_denials] == [403, 403, 403]
        assert _snapshot(
            scenario.engine, reverse.approval_run_id, reverse.ticket_id
        ) == before_denials

        ana_approved = scenario.client.post(
            warehouse_url,
            headers=scenario.headers("warehouse"),
            json={"action": "approve", "reason": None},
        )
        assert ana_approved.status_code == 200, ana_approved.text
        assert ana_approved.json()["interrupted"] is False
        assert ana_approved.json()["application"]["revision_status"] == "approved"
        assert ana_approved.json()["coordination"]["outcome"] == "waiting"
        assert ana_approved.json()["coordination"]["final_document_id"] is None

        reverse_still_waiting = scenario.client.get(
            f"{scenario.base}/departments/reverse/revisions/{reverse_b_id}",
            headers=scenario.headers("reverse"),
        )
        assert reverse_still_waiting.status_code == 200, reverse_still_waiting.text
        assert reverse_still_waiting.json()["interrupted"] is True
        assert reverse_still_waiting.json()["thread_id"] == reverse_workflow["thread_id"]
        with independent.connect() as connection:
            statuses = dict(connection.execute(
                select(
                    rfp_approval_department_revisions.c.department_id,
                    rfp_approval_department_revisions.c.status,
                ).where(
                    rfp_approval_department_revisions.c.revision_id.in_(
                        [scenario.warehouse_revision_id, reverse_b_id]
                    )
                )
            ).all())
            assert statuses == {"warehouse": "approved", "reverse": "pending_approval"}
            assert connection.execute(
                select(rfp_tickets.c.status).where(
                    rfp_tickets.c.ticket_id == reverse.ticket_id
                )
            ).scalar_one() == "esperando_aprobación"
            assert connection.execute(
                select(func.count()).select_from(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == reverse.approval_run_id
                )
            ).scalar_one() == 0

        sofia_approved = scenario.client.post(
            reverse_url,
            headers=scenario.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert sofia_approved.status_code == 200, sofia_approved.text
        assert sofia_approved.json()["interrupted"] is False
        assert sofia_approved.json()["application"]["revision_status"] == "approved"
        assert sofia_approved.json()["coordination"]["outcome"] == "waiting"
        assert sofia_approved.json()["coordination"]["blockers"] == [
            "proposal_automation_not_delegated"
        ]
        assert sofia_approved.json()["coordination"]["final_document_id"] is None

        with independent.connect() as connection:
            current = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id.in_(
                        [scenario.warehouse_revision_id, reverse_b_id]
                    )
                )
            ).mappings().all()
            assert {row["status"] for row in current} == {"approved"}
            decisions = connection.execute(
                select(rfp_human_decisions).where(
                    rfp_human_decisions.c.revision_id.in_(
                        [scenario.warehouse_revision_id, reverse_b_id]
                    )
                )
            ).mappings().all()
            assert len(decisions) == 2
            assert {row["department_id"] for row in decisions} == {
                "warehouse", "reverse"
            }
            assert {row["processing_status"] for row in decisions} == {"applied"}
            assert {
                row["department_id"]: row["expected_content_hash"]
                for row in decisions
            } == {
                "warehouse": scenario.warehouse_content_hash,
                "reverse": next(
                    row["content_hash"]
                    for row in current
                    if row["revision_id"] == reverse_b_id
                ),
            }
            assert connection.execute(
                select(func.count()).select_from(rfp_workflow_events).where(
                    rfp_workflow_events.c.approval_run_id == reverse.approval_run_id,
                    rfp_workflow_events.c.event_type == "human_decision_applied",
                )
            ).scalar_one() == 2
            assert connection.execute(
                select(func.count()).select_from(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == reverse.approval_run_id
                )
            ).scalar_one() == 0

        before_replays = _snapshot(
            scenario.engine, reverse.approval_run_id, reverse.ticket_id
        )
        ana_replay = scenario.client.post(
            warehouse_url,
            headers=scenario.headers("warehouse"),
            json={"action": "approve", "reason": None},
        )
        sofia_replay = scenario.client.post(
            reverse_url,
            headers=scenario.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert [ana_replay.status_code, sofia_replay.status_code] == [409, 409]
        assert ana_replay.json()["detail"] == "workflow_not_interrupted"
        assert sofia_replay.json()["detail"] == "workflow_not_interrupted"
        assert _snapshot(
            scenario.engine, reverse.approval_run_id, reverse.ticket_id
        ) == before_replays
    finally:
        independent.dispose()


def _record_cross_department_input(scenario: CorrectionScenario):
    other_department = "reverse" if scenario.department == "lastmile" else "lastmile"
    service_code = (
        "return_processing_rate"
        if other_department == "reverse"
        else "last_mile_rate"
    )
    now = datetime.now(timezone.utc)
    payload = {
        "input_kind": "client_price_quote",
        "department": other_department,
        "service_code": service_code,
        "amount": "15.0000",
        "currency": "USD",
        "unit": "shipment",
        "effective_from": now.date().isoformat(),
        "effective_until": None,
    }
    request_payload, response = scenario.record_input(
        actor="wrong", payload=payload, recorded_at=now.isoformat()
    )
    assert response.status_code == 200, response.text
    return request_payload, response.json(), other_department


def _persist_input_for_another_ticket_and_run(
    scenario: CorrectionScenario,
) -> tuple[UUID, UUID, UUID]:
    _request, recorded = scenario.record_input()
    assert recorded.status_code == 200, recorded.text
    source_input_id = UUID(recorded.json()["input_id"])
    foreign_ticket_id, foreign_response_run_id, foreign_approval_run_id = (
        uuid4(), uuid4(), uuid4()
    )
    foreign_input_id = uuid4()
    with Session(scenario.engine) as session, session.begin():
        ticket = dict(session.execute(select(rfp_tickets).where(
            rfp_tickets.c.ticket_id == scenario.ticket_id
        )).mappings().one())
        response_run = dict(session.execute(select(rfp_response_runs).where(
            rfp_response_runs.c.run_id == scenario.response_run_id
        )).mappings().one())
        approval_run = dict(session.execute(select(rfp_approval_runs).where(
            rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
        )).mappings().one())
        business_input = dict(session.execute(select(
            rfp_authorized_business_inputs
        ).where(
            rfp_authorized_business_inputs.c.input_id == source_input_id
        )).mappings().one())
        ticket.update({
            "ticket_id": foreign_ticket_id,
            "raw_pdf_path": f"gate1a/{foreign_ticket_id}/source.pdf",
            "markdown_path": f"gate1a/{foreign_ticket_id}/source.md",
        })
        response_run.update({
            "run_id": foreign_response_run_id,
            "ticket_id": foreign_ticket_id,
            "source_handoff_hash": canonical_hash({
                "foreign_response_run_id": str(foreign_response_run_id)
            }),
        })
        approval_run.update({
            "approval_run_id": foreign_approval_run_id,
            "ticket_id": foreign_ticket_id,
            "response_run_id": foreign_response_run_id,
            "start_idempotency_key": uuid4(),
            "start_request_hash": canonical_hash({
                "foreign_approval_run_id": str(foreign_approval_run_id)
            }),
        })
        business_input.update({
            "input_id": foreign_input_id,
            "approval_run_id": foreign_approval_run_id,
            "ticket_id": foreign_ticket_id,
            "idempotency_key": uuid4(),
        })
        session.execute(insert(rfp_tickets).values(**ticket))
        session.execute(insert(rfp_response_runs).values(**response_run))
        session.execute(insert(rfp_approval_runs).values(**approval_run))
        session.execute(insert(rfp_authorized_business_inputs).values(**business_input))
    return foreign_input_id, foreign_ticket_id, foreign_approval_run_id


def test_guardrails_fail_closed_without_authorization() -> None:
    secret = _RedactedSecret("must-not-appear")
    assert str(secret) == "must-not-appear"
    assert "must-not-appear" not in repr(secret)
    assert _guardrail_reason({}) == SKIP_REASON
    values = {
        URL_ENV: "postgresql+psycopg2://qa@db.example/test",
        AUTHORIZED_ENV: "YES",
        EXPECTED_DATABASE_ENV: DATABASE_PREFIX + "x",
        EXPECTED_USER_ENV: USER_PREFIX + "x",
        EXPECTED_PORT_ENV: "5432",
        EXPECTED_ENVIRONMENT_ENV: "qa",
        EXPECTED_CONTAINER_ENV: CONTAINER_PREFIX + "x",
        EXPECTED_IMAGE_ENV: "postgres:16-alpine",
        VERIFIED_CONTAINER_ID_ENV: "a" * 64,
        CONTAINER_VERIFIED_ENV: "YES",
        SCHEMA_ISOLATION_ENV: "YES",
    }
    assert _guardrail_reason(values) == "PostgreSQL host is not loopback"


def test_gate_2b1_snapshot_is_exact_and_replaces_only_historical_part3() -> None:
    from tests import rfp_approval_postgres_fixture as shared

    source = HISTORICAL_PART3_DDL.read_bytes()
    assert hashlib.sha256(source).hexdigest() == HISTORICAL_PART3_DDL_SHA256
    decoded = source.decode("utf-8")
    match = re.search(
        r"input_kind text NOT NULL CHECK\(input_kind IN \((.*?)\)\)",
        decoded,
        flags=re.DOTALL,
    )
    assert match is not None
    assert set(re.findall(r"'([^']+)'", match.group(1))) == set(
        HISTORICAL_INPUT_KINDS
    )
    assert "monthly_storage_volume" not in decoded

    historical = _selected_historical_migrations(shared, seven_kinds=True)
    fresh = _selected_historical_migrations(shared, seven_kinds=False)
    assert [path.name for path, _seed in historical] == [
        "rfp_intake.sql",
        "rfp_response_generation.sql",
        HISTORICAL_PART3_DDL.name,
    ]
    assert [path.name for path, _seed in fresh] == [
        "rfp_intake.sql",
        "rfp_response_generation.sql",
        "rfp_approval.sql",
    ]
    assert shared.EXTENSION_MIGRATION.name == "rfp_approval_extension.sql"


def test_monthly_storage_volume_forward_migration_matches_runtime_metadata(
    isolated_engine: IsolatedDatabase,
) -> None:
    with isolated_engine.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.conname,pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint AS c "
                "WHERE c.conrelid=to_regclass('rfp_authorized_business_inputs') "
                "AND c.contype='c' AND pg_get_constraintdef(c.oid) LIKE '%input_kind%'"
            )
        ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["conname"] == "ck_rfp_business_input_kind"
        definition = rows[0]["definition"]
        assert set(re.findall(r"'([^']+)'", definition)) == set(CURRENT_INPUT_KINDS)
        before_oid = connection.execute(
            text(
                "SELECT oid FROM pg_constraint WHERE conrelid="
                "to_regclass('rfp_authorized_business_inputs') "
                "AND conname='ck_rfp_business_input_kind'"
            )
        ).scalar_one()
    _run_migration(
        isolated_engine.engine,
        isolated_engine.schema,
        ROOT / "infra/sql/rfp_approval_monthly_storage_volume.sql",
    )
    with isolated_engine.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT oid FROM pg_constraint WHERE conrelid="
                "to_regclass('rfp_authorized_business_inputs') "
                "AND conname='ck_rfp_business_input_kind'"
            )
        ).scalar_one() == before_oid


def test_indicative_validation_bundle_forward_migration_is_reentrant(
    isolated_engine: IsolatedDatabase,
) -> None:
    """Exercise the exact pre-indicative constraint and its forward replacement."""

    engine = isolated_engine.engine
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE rfp_approval_validation_bundles DROP CONSTRAINT "
            "ck_rfp_validation_bundle_eligibility"
        )
        connection.exec_driver_sql(
            "ALTER TABLE rfp_approval_validation_bundles ADD CONSTRAINT "
            "ck_rfp_validation_bundle_eligibility CHECK (eligible=("
            "(scope_key<>'proposal' OR source_departments_approved) "
            "AND mandatory_compliance_passed "
            "AND NOT blocks_commercial_completeness "
            "AND jsonb_array_length(blocking_rule_ids)=0 "
            "AND jsonb_array_length(unresolved_input_codes)=0 "
            "AND jsonb_array_length(open_conflict_ids)=0))"
        )
    with engine.connect() as connection:
        historical = connection.execute(text(
            "SELECT pg_get_constraintdef(c.oid,true),"
            "obj_description(c.oid,'pg_constraint') "
            "FROM pg_constraint AS c WHERE c.conrelid="
            "to_regclass('rfp_approval_validation_bundles') AND "
            "c.conname='ck_rfp_validation_bundle_eligibility'"
        )).one()
        assert "NOT blocks_commercial_completeness" in historical[0]
        assert "RETURN_PROCESSING_RATE_REQUIRED" not in historical[0]
        assert historical[1] is None

    migration = ROOT / "infra/sql/rfp_approval_indicative_validation_bundle.sql"
    _run_migration(engine, isolated_engine.schema, migration)
    with engine.connect() as connection:
        migrated = connection.execute(text(
            "SELECT c.oid,pg_get_constraintdef(c.oid,true),"
            "obj_description(c.oid,'pg_constraint') "
            "FROM pg_constraint AS c WHERE c.conrelid="
            "to_regclass('rfp_approval_validation_bundles') AND "
            "c.conname='ck_rfp_validation_bundle_eligibility'"
        )).one()
        assert migrated[2] == "trackflow:indicative-validation-bundle-v1"
        assert all(code in migrated[1] for code in (
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "LAST_MILE_RATE_REQUIRED",
            "RETURN_PROCESSING_RATE_REQUIRED",
            "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
        ))
        migrated_oid = migrated[0]
    _run_migration(engine, isolated_engine.schema, migration)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT oid FROM pg_constraint WHERE conrelid="
            "to_regclass('rfp_approval_validation_bundles') AND "
            "conname='ck_rfp_validation_bundle_eligibility'"
        )).scalar_one() == migrated_oid


@pytest.mark.parametrize("isolated_engine", ["seven-kinds"], indirect=True)
def test_historical_seven_kind_constraint_migrates_without_mutating_rows(
    isolated_engine: IsolatedDatabase,
) -> None:
    historical_bytes = HISTORICAL_PART3_DDL.read_bytes()
    assert hashlib.sha256(historical_bytes).hexdigest() == (
        HISTORICAL_PART3_DDL_SHA256
    )
    historical_source = historical_bytes.decode("utf-8")
    assert "monthly_storage_volume" not in historical_source
    assert all(
        f"'{item}'" in historical_source for item in HISTORICAL_INPUT_KINDS
    )
    engine = isolated_engine.engine
    with engine.connect() as connection:
        principal = connection.execute(
            select(rfp_principals).where(
                rfp_principals.c.environment == "qa",
                rfp_principals.c.responsible_code == "miguel_torres",
            )
        ).mappings().one()
        binding = connection.execute(
            select(rfp_identity_bindings).where(
                rfp_identity_bindings.c.principal_id == principal["principal_id"],
                rfp_identity_bindings.c.environment == "qa",
                rfp_identity_bindings.c.active.is_(True),
            )
        ).mappings().one()
        # Gate 2B1 predates columns such as started_by_general_role.  Keep this
        # lookup on the two historical columns required by the representative
        # business-input row instead of projecting current runtime metadata.
        run = connection.execute(
            text(
                "SELECT approval_run_id,ticket_id FROM rfp_approval_runs "
                "WHERE environment=:environment ORDER BY approval_run_id LIMIT 1"
            ),
            {"environment": "qa"},
        ).mappings().first()
        assert run is not None
    authorization_id = uuid4()
    input_id = uuid4()
    quote = ClientPriceQuoteInput(
        department=DepartmentId.warehouse,
        service_code="historical-storage-rate",
        amount=Decimal("10.0000"),
        currency="USD",
        unit="m3/month",
        effective_from=NOW.date(),
    )
    provenance = BusinessInputProvenance(
        evidence_type="responsible_attestation",
        evidence_id=uuid4(),
        source_name="Gate 2B1 historical QA evidence",
        issued_at=NOW,
        source_payload_hash=canonical_hash(
            {"gate": "2b1", "input": str(input_id)}
        ),
    )
    quote_payload = canonical_value(quote)
    provenance_payload = canonical_value(provenance)
    with engine.begin() as connection:
        connection.execute(
            insert(rfp_authorizations).values(
                authorization_id=authorization_id,
                principal_id=principal["principal_id"],
                environment="qa",
                permission="provide_business_input",
                department_scope="warehouse",
                target_policy="department_exact",
                scope_fingerprint=canonical_hash(
                    {"gate": "2b1", "authorization_id": str(authorization_id)}
                ),
                authorization_version=1,
                active_from=NOW - timedelta(minutes=1),
                granted_by_principal_id=principal["principal_id"],
            )
        )
        connection.execute(
            insert(rfp_authorized_business_inputs).values(
                input_id=input_id,
                approval_run_id=run["approval_run_id"],
                ticket_id=run["ticket_id"],
                input_kind="client_price_quote",
                department_scope="warehouse",
                payload=quote_payload,
                provenance=provenance_payload,
                payload_hash=canonical_hash(quote_payload),
                provenance_hash=canonical_hash(provenance_payload),
                evidence_hash=provenance.source_payload_hash,
                idempotency_key=uuid4(),
                actor_principal_id=principal["principal_id"],
                actor_binding_id=binding["binding_id"],
                authorization_id=authorization_id,
                authorization_version=1,
                environment="qa",
                recorded_at=NOW,
            )
        )
    with engine.connect() as connection:
        before_inputs = connection.execute(
            select(rfp_authorized_business_inputs).order_by(
                rfp_authorized_business_inputs.c.input_id
            )
        ).mappings().all()
        before_authorizations = connection.execute(
            select(rfp_authorizations).order_by(rfp_authorizations.c.authorization_id)
        ).mappings().all()
    with engine.connect() as connection:
        historical = connection.execute(
            text(
                "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=to_regclass('rfp_authorized_business_inputs') "
                "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%input_kind%'"
            )
        ).one()
        assert historical[0] == "rfp_authorized_business_inputs_input_kind_check"
        assert set(re.findall(r"'([^']+)'", historical[1])) == set(
            HISTORICAL_INPUT_KINDS
        )
    migration = ROOT / "infra/sql/rfp_approval_monthly_storage_volume.sql"
    _run_migration(engine, isolated_engine.schema, migration)
    _run_migration(engine, isolated_engine.schema, migration)
    with engine.connect() as connection:
        migrated = connection.execute(
            text(
                "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=to_regclass('rfp_authorized_business_inputs') "
                "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%input_kind%'"
            )
        ).one()
        assert migrated[0] == "ck_rfp_business_input_kind"
        assert set(re.findall(r"'([^']+)'", migrated[1])) == set(
            CURRENT_INPUT_KINDS
        )
        assert connection.execute(
            select(rfp_authorized_business_inputs).order_by(
                rfp_authorized_business_inputs.c.input_id
            )
        ).mappings().all() == before_inputs
        assert connection.execute(
            select(rfp_authorizations).order_by(rfp_authorizations.c.authorization_id)
        ).mappings().all() == before_authorizations


def test_missing_workflow_thread_has_exact_http_contract() -> None:
    route_path = ROOT / "services" / "api" / "routes" / "rfp.py"
    route_tree = ast.parse(route_path.read_text(encoding="utf-8"))
    error_mapper = next(
        node
        for node in route_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_approval_http_error"
    )
    missing_thread_branch = next(
        node
        for node in ast.walk(error_mapper)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and any(
            isinstance(comparator, ast.Constant)
            and comparator.value == "workflow_thread_not_found"
            for comparator in node.test.comparators
        )
    )
    branch_source = ast.unparse(missing_thread_branch)
    returns = [node for node in ast.walk(error_mapper) if isinstance(node, ast.Return)]

    assert "status.HTTP_404_NOT_FOUND" in branch_source
    assert any("detail=code" in ast.unparse(node) for node in returns)


@pytest.mark.parametrize(
    ("initial_hitl", "expected_response", "expected_ticket"),
    [
        (True, "ready_for_part3", "en_evaluación"),
        (False, "needs_human_review", "needs_human_review"),
    ],
)
def test_fixture_selects_coherent_terminal_states(
    initial_hitl: bool,
    expected_response: str,
    expected_ticket: str,
) -> None:
    assert _response_terminal_state(initial_hitl=initial_hitl) == (
        expected_response,
        expected_ticket,
    )


def test_initial_lastmile_global_source_uses_real_part2_pipeline() -> None:
    ticket_id, run_id = uuid4(), uuid4()
    response_start = RFPResponseStart(
        ticket_id=ticket_id,
        run_id=run_id,
        source_handoff_hash="a" * 64,
        summary_for_sales="Isolated QA handoff for correction integration.",
        key_aspects=[RequestedAspect(
            aspect_id="lastmile:01:1-2",
            department=DepartmentId.lastmile,
            fact=(
                "Client requests last-mile carrier coverage in the United "
                "States."
            ),
            requested_action="Describe the available carrier coverage.",
            line_start=1,
            line_end=2,
        )],
        selected_departments=[DepartmentId.lastmile],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=20,
        customer_managed_carrier=True,
        carrier_confirmation_hash="b" * 64,
        language="es",
    )
    source = generate_department_draft(
        response_start,
        department=DepartmentId.lastmile,
        iteration_count=1,
        generated_at=NOW,
    )
    section_evaluation, feedback = evaluate_department(
        response_start, source, evaluated_at=NOW
    )
    assert feedback == []
    assert section_evaluation.overall_pass is True

    draft, evaluation, preview = _build_evaluated_global_source(
        response_start, source, section_evaluation
    )

    assert _global_import_failures(draft, evaluation, preview) == ()
    assert draft.currency.evidence[0].document_id == "rfp-intake-v1"
    assert draft.currency.evidence[0].section_id == "proposal_currency"
    assert draft.sla.exclusions == [
        "No TrackFlow-managed last-mile service in scope"
    ]
    assert len(draft.volume_discounts) == 1
    assert draft.volume_discounts[0].status == "pending"
    assert draft.pending_commercial_items == []
    assert evaluation.outcome == "ready_for_part3"
    assert evaluation.global_compliance.passed is True
    assert preview.gate_status == "passed"
    assert preview.content


def _reverse_historical_part2_source():
    response_start = RFPResponseStart(
        ticket_id=uuid4(),
        run_id=uuid4(),
        source_handoff_hash="a" * 64,
        summary_for_sales="Isolated QA handoff for a Reverse correction.",
        key_aspects=[RequestedAspect(
            aspect_id="reverse:01:1-2",
            department=DepartmentId.reverse,
            fact="Client requests a documented return inspection process.",
            requested_action="Describe return inspection and classification.",
            line_start=1,
            line_end=2,
        )],
        selected_departments=[DepartmentId.reverse],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=20,
        customer_managed_carrier=False,
        carrier_confirmation_hash=None,
        language="es",
    )
    source = generate_department_draft(
        response_start,
        department=DepartmentId.reverse,
        iteration_count=1,
        generated_at=NOW,
    )
    section_evaluation, feedback = evaluate_department(
        response_start, source, evaluated_at=NOW
    )
    assert feedback
    global_draft, global_evaluation, global_preview = (
        _build_evaluated_global_source(
            response_start,
            source,
            section_evaluation,
            require_department_rollup_pass=False,
        )
    )
    return (
        response_start,
        source,
        section_evaluation,
        global_draft,
        global_evaluation,
        global_preview,
    )


def test_reverse_historical_global_source_preserves_real_rollup() -> None:
    (
        response_start,
        source,
        section_evaluation,
        global_draft,
        global_evaluation,
        global_preview,
    ) = _reverse_historical_part2_source()

    assert {item.code for item in source.pending_commercial_items} == {
        "RETURN_PROCESSING_RATE_REQUIRED"
    }
    assert section_evaluation.terminal_status == "needs_human_input"
    assert section_evaluation.overall_pass is False
    assert global_evaluation.all_departments_passed is False
    assert global_evaluation.overall_pass is False
    assert global_evaluation.outcome == "needs_human_review"
    assert global_evaluation.global_compliance.passed is True
    assert global_draft.pending_commercial_items == []
    assert global_preview.gate_status == "passed"
    assert global_preview.content
    assert _global_import_failures(
        global_draft, global_evaluation, global_preview
    ) == ("evaluation_overall_pass", "evaluation_outcome")
    assert _global_import_failures(
        global_draft,
        global_evaluation,
        global_preview,
        require_department_rollup_pass=False,
    ) == ()

    handoff = RFPResponseHandoff(
        ticket_id=response_start.ticket_id,
        run_id=response_start.run_id,
        source_handoff_hash=response_start.source_handoff_hash,
        outcome="needs_human_review",
        all_sections_passed=False,
        global_compliance_passed=True,
        selected_departments=[DepartmentId.reverse],
        department_results=[DepartmentHandoffResult(
            department=DepartmentId.reverse,
            terminal_status=section_evaluation.terminal_status,
            iteration_count=1,
            final_draft_id=source.draft_id,
            evaluation_result_id=section_evaluation.evaluation_id,
            safe_preview_id=uuid4(),
            blocks_commercial_completeness=(
                section_evaluation.blocks_commercial_completeness
            ),
            requires_human_input=section_evaluation.requires_human_input,
            blocking_rule_ids=section_evaluation.blocking_rule_ids,
        )],
        global_draft_id=global_draft.draft_id,
        global_evaluation_result_id=global_evaluation.evaluation_id,
        unresolved_items=source.pending_commercial_items,
        created_at=NOW,
    )
    assert handoff.outcome == "needs_human_review"
    for artifact in (global_draft, global_evaluation):
        payload = canonical_value(artifact)
        assert canonical_hash(payload) == canonical_hash(
            canonical_value(type(artifact).model_validate(payload, strict=False))
        )


def test_joint_warehouse_reverse_preconditions_and_threads_are_independent() -> None:
    ticket_id, response_run_id, approval_run_id = uuid4(), uuid4(), uuid4()
    response_start = RFPResponseStart(
        ticket_id=ticket_id,
        run_id=response_run_id,
        source_handoff_hash="a" * 64,
        summary_for_sales="Joint Warehouse and Reverse QA handoff.",
        key_aspects=[
            RequestedAspect(
                aspect_id="warehouse:01:1-2",
                department=DepartmentId.warehouse,
                fact="Client requests warehouse storage in the United States.",
                requested_action="Describe the documented storage service.",
                line_start=1,
                line_end=2,
            ),
            RequestedAspect(
                aspect_id="reverse:01:3-4",
                department=DepartmentId.reverse,
                fact="Client requests a documented return inspection process.",
                requested_action="Describe return inspection and classification.",
                line_start=3,
                line_end=4,
            ),
        ],
        selected_departments=[DepartmentId.warehouse, DepartmentId.reverse],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=20,
        customer_managed_carrier=False,
        carrier_confirmation_hash=None,
        language="es",
    )
    sources = [
        generate_department_draft(
            response_start,
            department=department,
            iteration_count=1,
            generated_at=NOW,
        )
        for department in (DepartmentId.warehouse, DepartmentId.reverse)
    ]
    evaluations = [
        evaluate_department(response_start, source, evaluated_at=NOW)[0]
        for source in sources
    ]
    by_department = {
        evaluation.department: evaluation for evaluation in evaluations
    }

    assert by_department[DepartmentId.warehouse].overall_pass is True
    assert by_department[DepartmentId.warehouse].terminal_status == "passed"
    assert by_department[DepartmentId.reverse].overall_pass is False
    assert by_department[DepartmentId.reverse].terminal_status == "needs_human_input"
    assert {
        item.code for item in sources[1].pending_commercial_items
    } == {"RETURN_PROCESSING_RATE_REQUIRED"}
    assert by_department[DepartmentId.reverse].compliance.passed is True
    assert by_department[DepartmentId.reverse].blocking_rule_ids == []
    commercial_warning = next(
        item
        for item in by_department[DepartmentId.reverse].compliance.findings
        if item.rule_id == "TF-COMMERCIAL-001"
    )
    assert commercial_warning.blocking is False
    assert commercial_warning.requires_human_input is True
    global_draft, global_evaluation, global_preview = _build_evaluated_global_source(
        response_start,
        sources,
        evaluations,
        require_department_rollup_pass=False,
    )
    assert global_evaluation.outcome == "needs_human_review"
    assert global_evaluation.global_compliance.passed is True
    assert global_draft.pending_commercial_items == []
    assert global_preview.gate_status == "passed"
    assert global_preview.content
    assert _global_import_failures(
        global_draft,
        global_evaluation,
        global_preview,
        require_department_rollup_pass=False,
    ) == ()

    targets = [
        DepartmentApprovalTarget(
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            revision_id=uuid4(),
            department=department,
        )
        for department in (DepartmentId.warehouse, DepartmentId.reverse)
    ]
    assert targets[0].ticket_id == targets[1].ticket_id == ticket_id
    assert targets[0].approval_run_id == targets[1].approval_run_id == approval_run_id
    assert targets[0].thread_id != targets[1].thread_id


@pytest.mark.parametrize(
    ("invalid_component", "expected_failure"),
    [
        ("global_compliance", "global_compliance"),
        ("global_pending", "pending_commercial_items"),
        ("preview_gate", "safe_preview_gate"),
        ("preview_content", "safe_preview_content"),
    ],
)
def test_corrected_history_still_rejects_independent_global_blockers(
    invalid_component: str,
    expected_failure: str,
) -> None:
    (
        _response_start,
        _source,
        _section_evaluation,
        global_draft,
        global_evaluation,
        global_preview,
    ) = _reverse_historical_part2_source()
    draft = SimpleNamespace(
        pending_commercial_items=list(global_draft.pending_commercial_items)
    )
    evaluation = SimpleNamespace(
        overall_pass=global_evaluation.overall_pass,
        outcome=global_evaluation.outcome,
        global_compliance=SimpleNamespace(
            passed=global_evaluation.global_compliance.passed
        ),
    )
    preview = SimpleNamespace(
        gate_status=global_preview.gate_status,
        content=global_preview.content,
    )
    if invalid_component == "global_compliance":
        evaluation.global_compliance.passed = False
    elif invalid_component == "global_pending":
        draft.pending_commercial_items = [object()]
    elif invalid_component == "preview_gate":
        preview.gate_status = "blocked"
    else:
        preview.content = None

    assert _global_import_failures(
        draft,
        evaluation,
        preview,
        require_department_rollup_pass=False,
    ) == (expected_failure,)


@pytest.mark.parametrize(
    ("department", "fact", "requested_action", "expected_pass", "pending_code"),
    [
        (
            DepartmentId.warehouse,
            "Client requests warehouse storage in the United States.",
            "Describe the documented storage service.",
            True,
            None,
        ),
        (
            DepartmentId.reverse,
            "Client requests a documented return inspection process.",
            "Describe return inspection and classification.",
            False,
            "RETURN_PROCESSING_RATE_REQUIRED",
        ),
    ],
)
def test_department_hitl_fixture_uses_real_part2_preconditions(
    department: DepartmentId,
    fact: str,
    requested_action: str,
    expected_pass: bool,
    pending_code: str | None,
) -> None:
    """Lock Warehouse/Reverse preparation to the real deterministic evaluator."""

    response_start = RFPResponseStart(
        ticket_id=uuid4(),
        run_id=uuid4(),
        source_handoff_hash="a" * 64,
        summary_for_sales="Isolated QA handoff for departmental HITL.",
        key_aspects=[RequestedAspect(
            aspect_id=f"{department.value}:01:1-2",
            department=department,
            fact=fact,
            requested_action=requested_action,
            line_start=1,
            line_end=2,
        )],
        selected_departments=[department],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=20,
        customer_managed_carrier=False,
        carrier_confirmation_hash=None,
        language="es",
    )
    draft = generate_department_draft(
        response_start,
        department=department,
        iteration_count=1,
        generated_at=NOW,
    )
    evaluation, _feedback = evaluate_department(
        response_start, draft, evaluated_at=NOW
    )
    pending_codes = {item.code for item in draft.pending_commercial_items}

    assert evaluation.overall_pass is expected_pass
    if pending_code is None:
        assert pending_codes == set()
        assert evaluation.terminal_status == "passed"
    else:
        assert pending_code in pending_codes
        assert evaluation.blocks_commercial_completeness is True
        assert evaluation.requires_human_input is True


def test_fixture_producer_reuses_chain_provenance_and_rejects_mismatch() -> None:
    execution_id = uuid4()
    provenance_values = {
        "producer_node": "initialize_approval_run",
        "execution_id": execution_id,
        "source_kind": "part2_import",
        "source_artifact_ids": [uuid4()],
        "source_hashes": ["a" * 64],
        "produced_at": NOW,
    }
    provenance = ApprovalArtifactProvenance(
        **provenance_values,
        provenance_hash=canonical_hash(provenance_values),
    )
    artifact = SimpleNamespace(provenance=provenance)
    chain = SimpleNamespace(
        draft=artifact,
        evaluation=artifact,
        preview=artifact,
        bundle=artifact,
    )
    producer = _producer_from_chain(chain, environment="qa")
    assert producer.producer_node == provenance.producer_node
    assert producer.execution_id == provenance.execution_id
    _assert_producer(provenance, producer)

    mismatched = ArtifactProducerContext._from_verified_orchestrator(
        environment="qa",
        producer_node=provenance.producer_node,
        execution_id=uuid4(),
    )
    with pytest.raises(ApprovalStoreError) as raised:
        _assert_producer(provenance, mismatched)
    assert raised.value.code == "artifact_producer_provenance_mismatch"

    divergent_artifact = SimpleNamespace(
        provenance=provenance.model_copy(update={"execution_id": uuid4()})
    )
    divergent_chain = SimpleNamespace(
        draft=artifact,
        evaluation=artifact,
        preview=artifact,
        bundle=divergent_artifact,
    )
    with pytest.raises(
        RuntimeError, match="fixture artifact provenance identities diverged"
    ):
        _producer_from_chain(divergent_chain, environment="qa")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (AUTHORIZED_ENV, "NO"),
        (EXPECTED_ENVIRONMENT_ENV, "production"),
        (CONTAINER_VERIFIED_ENV, "NO"),
        (SCHEMA_ISOLATION_ENV, "NO"),
        (EXPECTED_DATABASE_ENV, "historical_trackflow"),
        (EXPECTED_USER_ENV, "postgres"),
        (EXPECTED_PORT_ENV, "not-a-port"),
        (VERIFIED_CONTAINER_ID_ENV, "short-id"),
        (EXPECTED_IMAGE_ENV, "postgres:15-alpine"),
    ],
)
def test_each_guardrail_identity_dimension_fails_closed(key, value) -> None:
    values = {
        URL_ENV: (
            "postgresql+psycopg2://trackflow_rfp_approval_correction_user@"
            "127.0.0.1:65478/trackflow_rfp_approval_correction_test_gate1a"
        ),
        AUTHORIZED_ENV: "YES",
        EXPECTED_DATABASE_ENV: "trackflow_rfp_approval_correction_test_gate1a",
        EXPECTED_USER_ENV: "trackflow_rfp_approval_correction_user",
        EXPECTED_PORT_ENV: "65478",
        EXPECTED_ENVIRONMENT_ENV: "qa",
        EXPECTED_CONTAINER_ENV: CONTAINER_PREFIX + "gate1a",
        EXPECTED_IMAGE_ENV: "postgres:16-alpine",
        VERIFIED_CONTAINER_ID_ENV: "a" * 64,
        CONTAINER_VERIFIED_ENV: "YES",
        SCHEMA_ISOLATION_ENV: "YES",
    }
    values[key] = value
    assert _guardrail_reason(values) is not None


def test_last_mile_authorized_correction(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    assert corrected["department"] == "lastmile"
    assert corrected["status"] == "pending_approval"
    assert corrected["remaining_input_codes"] == []


@pytest.mark.parametrize("correction_scenario", ["reverse"], indirect=True)
def test_reverse_authorized_correction(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    assert corrected["department"] == "reverse"
    assert corrected["status"] == "pending_approval"


def test_exact_idempotent_replay(correction_scenario) -> None:
    _, _, body, first = _authorized_correction(correction_scenario)
    counts = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    replay = correction_scenario.correct(body)
    assert replay.status_code == 200
    assert replay.json()["revision_id"] == first["revision_id"]
    assert replay.json()["replayed"] is True
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == counts


def test_stale_cas_has_no_partial_writes(correction_scenario) -> None:
    _, recorded = correction_scenario.record_input()
    assert recorded.status_code == 200
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    response = correction_scenario.correct(correction_scenario.correction_body(
        recorded.json()["input_id"], expected_predecessor_state_version=999
    ))
    assert response.status_code == 409
    assert response.json()["detail"] == "predecessor_revision_stale"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before


def test_wrong_content_hash_has_no_partial_writes(correction_scenario) -> None:
    _, recorded = correction_scenario.record_input()
    assert recorded.status_code == 200
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    response = correction_scenario.correct(correction_scenario.correction_body(
        recorded.json()["input_id"], expected_predecessor_content_hash="f" * 64
    ))
    assert response.status_code == 409
    assert response.json()["detail"] == "predecessor_revision_stale"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before


def test_wrong_department_actor_is_denied_without_writes(correction_scenario) -> None:
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    _, response = correction_scenario.record_input(actor="wrong")
    assert response.status_code == 403
    assert response.json()["detail"] == "Business-input permission is required"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before


def test_admin_without_department_grant_is_denied_without_writes(
    correction_scenario,
) -> None:
    before = _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    )
    _, response = correction_scenario.record_input(actor="admin")
    assert response.status_code == 403
    assert response.json()["detail"] == "RFP approval identity is not authorized"
    assert _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    ) == before


@pytest.mark.parametrize("fault", ["principal", "binding", "issuer", "environment"])
def test_incompatible_authenticated_identity_is_fail_closed(
    correction_scenario, fault
) -> None:
    account = correction_scenario.accounts[correction_scenario.department]
    with Session(correction_scenario.engine) as session, session.begin():
        principal = session.execute(select(rfp_principals).where(
            rfp_principals.c.trackflow_user_id == account.user_id,
            rfp_principals.c.environment == "qa",
        )).mappings().one()
        if fault == "principal":
            session.execute(update(rfp_principals).where(
                rfp_principals.c.principal_id == principal["principal_id"]
            ).values(is_active=False))
        elif fault == "binding":
            session.execute(update(rfp_identity_bindings).where(
                rfp_identity_bindings.c.principal_id == principal["principal_id"]
            ).values(is_active=False))
        elif fault == "issuer":
            session.execute(update(rfp_identity_bindings).where(
                rfp_identity_bindings.c.principal_id == principal["principal_id"]
            ).values(issuer="urn:trackflow:local"))
        else:
            production_principal_id, production_binding_id = uuid4(), uuid4()
            session.execute(update(rfp_principals).where(
                rfp_principals.c.principal_id == principal["principal_id"]
            ).values(trackflow_user_id=f"detached-qa-{uuid4()}"))
            session.execute(insert(rfp_principals).values(
                principal_id=production_principal_id,
                environment="production",
                trackflow_user_id=account.user_id,
                responsible_code=principal["responsible_code"],
                synthetic=False,
            ))
            session.execute(insert(rfp_identity_bindings).values(
                binding_id=production_binding_id,
                principal_id=production_principal_id,
                environment="production",
                auth_source="local",
                issuer="trackflow-local",
                subject=account.user_id,
            ))
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    _, response = correction_scenario.record_input()
    assert response.status_code == 403
    assert response.json()["detail"] == "RFP approval identity is not authorized"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before


def test_foreign_input_kind_is_not_applicable(correction_scenario) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "input_id": str(uuid4()),
        "input_kind": "operational_scope",
        "payload": {
            "input_kind": "operational_scope",
            "department": correction_scenario.department,
            "scope_code": "qa-window",
            "description": "Authorized QA operational window.",
            "included": True,
            "effective_from": now.date().isoformat(),
        },
        "provenance": {
            "evidence_type": "responsible_attestation",
            "evidence_id": str(uuid4()),
            "source_name": "Gate 1A isolated QA attestation",
            "issued_at": now.isoformat(),
            "source_payload_hash": canonical_hash({"scope": "qa-window"}),
        },
        "idempotency_key": str(uuid4()),
        "recorded_at": now.isoformat(),
    }
    recorded = correction_scenario.client.post(
        f"{correction_scenario.base}/business-inputs",
        headers=correction_scenario.headers(correction_scenario.department),
        json=payload,
    )
    assert recorded.status_code == 200
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    rejected = correction_scenario.correct(correction_scenario.correction_body(
        recorded.json()["input_id"]
    ))
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "authorized_input_not_applicable_to_correction"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before


def test_input_owned_by_another_principal_is_rejected(correction_scenario) -> None:
    _, recorded, _other_department = _record_cross_department_input(
        correction_scenario
    )
    with Session(correction_scenario.engine) as session:
        row = session.execute(select(rfp_authorized_business_inputs).where(
            rfp_authorized_business_inputs.c.input_id == UUID(recorded["input_id"])
        )).mappings().one()
        expected_principal = session.execute(select(rfp_principals.c.principal_id).where(
            rfp_principals.c.trackflow_user_id
            == correction_scenario.accounts[correction_scenario.department].user_id
        )).scalar_one()
    assert row["actor_principal_id"] != expected_principal
    before = _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    )
    rejected = correction_scenario.correct(
        correction_scenario.correction_body(recorded["input_id"])
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "authorized_input_not_applicable_to_correction"
    assert _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    ) == before


def test_input_for_another_department_is_rejected(correction_scenario) -> None:
    _, recorded, other_department = _record_cross_department_input(
        correction_scenario
    )
    with Session(correction_scenario.engine) as session:
        row = session.execute(select(rfp_authorized_business_inputs).where(
            rfp_authorized_business_inputs.c.input_id == UUID(recorded["input_id"])
        )).mappings().one()
    assert row["department_scope"] == other_department
    assert row["department_scope"] != correction_scenario.department
    before = _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    )
    rejected = correction_scenario.correct(
        correction_scenario.correction_body(recorded["input_id"])
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "authorized_input_not_applicable_to_correction"
    assert _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    ) == before


def test_input_from_another_ticket_is_rejected(correction_scenario) -> None:
    input_id, foreign_ticket_id, _foreign_run_id = (
        _persist_input_for_another_ticket_and_run(correction_scenario)
    )
    assert foreign_ticket_id != correction_scenario.ticket_id
    before = _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    )
    rejected = correction_scenario.correct(
        correction_scenario.correction_body(str(input_id))
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "authorized_input_not_found_or_scope_mismatch"
    assert _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    ) == before


def test_input_from_another_approval_run_is_rejected(correction_scenario) -> None:
    input_id, _foreign_ticket_id, foreign_run_id = (
        _persist_input_for_another_ticket_and_run(correction_scenario)
    )
    assert foreign_run_id != correction_scenario.approval_run_id
    before = _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    )
    rejected = correction_scenario.correct(
        correction_scenario.correction_body(str(input_id))
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "authorized_input_not_found_or_scope_mismatch"
    assert _snapshot(
        correction_scenario.engine,
        correction_scenario.approval_run_id,
        correction_scenario.ticket_id,
    ) == before


def test_hashes_and_artifacts_are_cryptographically_bound(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    revision_id = UUID(corrected["revision_id"])
    with Session(correction_scenario.engine) as session:
        revision = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == revision_id
        )).mappings().one()
        draft = session.execute(select(rfp_approval_corrected_drafts).where(
            rfp_approval_corrected_drafts.c.draft_artifact_id == revision["draft_id"]
        )).mappings().one()
        bundle = session.execute(select(rfp_approval_validation_bundles).where(
            rfp_approval_validation_bundles.c.validation_bundle_id
            == revision["validation_bundle_id"]
        )).mappings().one()
        artifacts = session.execute(select(rfp_approval_validation_artifacts).where(
            rfp_approval_validation_artifacts.c.artifact_id.in_([
                bundle["evaluation_artifact_id"], bundle["safe_preview_artifact_id"]
            ])
        )).mappings().all()
    assert draft["payload_hash"] == canonical_hash(draft["payload"])
    assert revision["content_hash"] == draft["content_hash"] == bundle["content_hash"]
    assert all(item["evaluated_content_hash"] == revision["content_hash"] for item in artifacts)
    assert all(item["payload_hash"] == canonical_hash(item["payload"]) for item in artifacts)


def test_revision_lineage_a_to_b_has_one_current(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    with correction_scenario.engine.connect() as connection:
        rows = connection.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.approval_run_id
            == correction_scenario.approval_run_id
        ).order_by(rfp_approval_department_revisions.c.review_revision_no)).mappings().all()
    assert [row["review_revision_no"] for row in rows] == [1, 2]
    assert rows[0]["status"] == "superseded" and rows[0]["is_current"] is False
    assert rows[1]["revision_id"] == UUID(corrected["revision_id"])
    assert rows[1]["supersedes_revision_id"] == rows[0]["revision_id"]
    assert rows[1]["is_current"] is True
    assert sum(row["is_current"] for row in rows) == 1


def test_assessment_artifact_targets_corrected_hash(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    assessment_response = correction_scenario.client.post(
        f"{correction_scenario.base}/assess-and-start",
        headers=correction_scenario.headers("manager"),
    )
    assert assessment_response.status_code == 200, assessment_response.text
    with Session(correction_scenario.engine) as session:
        revision = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == UUID(corrected["revision_id"])
        )).mappings().one()
        current_revisions = session.execute(select(
            rfp_approval_department_revisions.c.department_id,
            rfp_approval_department_revisions.c.content_hash,
        ).where(
            rfp_approval_department_revisions.c.approval_run_id
            == correction_scenario.approval_run_id,
            rfp_approval_department_revisions.c.is_current.is_(True),
        )).mappings().all()
        bundle = session.execute(select(rfp_approval_validation_bundles).where(
            rfp_approval_validation_bundles.c.validation_bundle_id
            == revision["validation_bundle_id"]
        )).mappings().one()
        evaluation = session.execute(select(rfp_approval_validation_artifacts).where(
            rfp_approval_validation_artifacts.c.artifact_id
            == bundle["evaluation_artifact_id"]
        )).mappings().one()
        source_payload = session.execute(select(rfp_response_drafts.c.payload).where(
            rfp_response_drafts.c.draft_id == revision["source_part2_draft_id"]
        )).scalar_one()
        global_payload = session.execute(select(rfp_response_drafts.c.payload).where(
            rfp_response_drafts.c.run_id == correction_scenario.response_run_id,
            rfp_response_drafts.c.scope_key == "proposal",
        )).scalar_one()
        assessments = session.execute(select(rfp_conflict_assessments).where(
            rfp_conflict_assessments.c.approval_run_id
            == correction_scenario.approval_run_id
        )).mappings().all()
    assert evaluation["evaluated_content_hash"] == revision["content_hash"]
    assert bundle["content_hash"] == revision["content_hash"]
    assert bundle["revision_validation_hash"] == revision["validation_hash"]
    DepartmentDraft.model_validate(source_payload)
    GlobalCommercialTermsDraft.model_validate(global_payload)
    expected_hashes = [
        {"scope_key": row["department_id"], "content_hash": row["content_hash"]}
        for row in current_revisions
    ]
    expected_hashes.append({
            "scope_key": "proposal",
            "content_hash": canonical_hash(global_payload),
        })
    expected_hashes.sort(key=lambda item: item["scope_key"])
    assert len(assessments) == 3
    assert {item["trigger_id"] for item in assessments} == {
        "volume-vs-capacity", "returns-sla-breach", "currency-mismatch"
    }
    assert all(item["assessed_content_hashes"] == expected_hashes for item in assessments)


@pytest.mark.parametrize("correction_scenario", ["lastmile-extra"], indirect=True)
def test_remaining_information_keeps_blocked_input(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    assert corrected["status"] == "blocked_input"
    assert corrected["remaining_input_codes"] == [
        "AUTHORIZED_OPERATING_WINDOW_REQUIRED"
    ]


def test_correction_never_starts_hitl_prematurely(correction_scenario) -> None:
    _, _, _, corrected = _authorized_correction(correction_scenario)
    assert corrected["ready_for_hitl"] is False
    assert not correction_scenario.checkpoint.exists()
    with Session(correction_scenario.engine) as session:
        row = session.execute(select(rfp_approval_department_revisions).where(
            rfp_approval_department_revisions.c.revision_id == UUID(corrected["revision_id"])
        )).mappings().one()
    assert row["applied_decision_id"] is None


def test_assess_and_start_enforces_reviewable_precondition(correction_scenario) -> None:
    before = _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id)
    response = correction_scenario.client.post(
        f"{correction_scenario.base}/assess-and-start",
        headers=correction_scenario.headers("manager"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "assessment_requires_reviewable_current_revisions"
    assert _snapshot(correction_scenario.engine, correction_scenario.approval_run_id, correction_scenario.ticket_id) == before
    assert not correction_scenario.checkpoint.exists()


def test_committed_correction_is_visible_from_new_connection(correction_scenario) -> None:
    _, recorded, _, corrected = _authorized_correction(correction_scenario)
    independent_engine = create_engine(
        correction_scenario.database_url,
        poolclass=NullPool,
        connect_args={
            "options": f"-csearch_path={correction_scenario.schema}"
        },
    )
    try:
        with correction_scenario.engine.connect() as pooled, independent_engine.connect() as independent:
            pooled_pid = pooled.execute(text("SELECT pg_backend_pid()")).scalar_one()
            independent_pid = independent.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            assert independent_pid != pooled_pid
            assert independent.execute(text("SELECT current_schema()")).scalar_one() == (
                correction_scenario.schema
            )
            input_row = independent.execute(select(rfp_authorized_business_inputs).where(
                rfp_authorized_business_inputs.c.input_id == UUID(recorded["input_id"])
            )).mappings().one()
            revision = independent.execute(select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id == UUID(corrected["revision_id"])
            )).mappings().one()
            run = independent.execute(select(rfp_approval_runs).where(
                rfp_approval_runs.c.approval_run_id == correction_scenario.approval_run_id
            )).mappings().one()
    finally:
        independent_engine.dispose()
    assert input_row["payload_hash"] == recorded["payload_hash"]
    assert revision["status"] == corrected["status"]
    assert revision["content_hash"] == corrected["content_hash"]
    assert run["environment"] == "qa"
