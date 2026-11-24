from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from data.pipelines.rfp_intake.contracts import DepartmentId, SourceReference
from data.pipelines.rfp_response.contracts import (
    DepartmentHandoffResult,
    RFPResponseHandoff,
    RFPResponseStart,
    RequestedAspect,
)
from pipelines.rfp_approval.contracts import (
    ApprovalRunStart,
    AuthenticatedPrincipal,
    BusinessInputProvenance,
    InitialRevisionReference,
    MonthlyStorageVolumeInput,
    RunInitiator,
)
from services.rfp_approval.models import (
    rfp_approval_corrected_drafts,
    rfp_approval_department_revisions,
    rfp_approval_proposal_revisions,
    rfp_approval_runs,
    rfp_approval_validation_bundles,
    rfp_authorizations,
    rfp_authorized_business_inputs,
    rfp_final_documents,
    rfp_human_decisions,
    rfp_identity_bindings,
    rfp_principals,
    rfp_workflow_events,
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
from tests import test_rfp_approval_correction_postgres_integration as closed_gate


# Reuse the reviewed PostgreSQL isolation, ASGI authentication and checkpoint
# lifecycle without changing the already closed Warehouse/Reverse gate.
isolated_engine = closed_gate.isolated_engine
qa_jwt_secret = closed_gate.qa_jwt_secret


@dataclass(frozen=True)
class AutomaticClosureEnvironment:
    engine: Engine
    database_url: str
    schema: str
    client: TestClient
    accounts: dict[str, closed_gate.Account]
    checkpoint: Path

    def headers(self, actor: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.accounts[actor].token}"}


@pytest.fixture
def auto_closure_environment(
    isolated_engine: closed_gate.IsolatedDatabase,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    qa_jwt_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[AutomaticClosureEnvironment]:
    """Reuse the closed gate's isolation while creating no bootstrap business run."""

    monkeypatch.setenv("TRACKFLOW_ENVIRONMENT", "qa")
    monkeypatch.setenv("JWT_SECRET_KEY", qa_jwt_secret)
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    auth_path = tmp_path_factory.getbasetemp() / "auto-finalization-auth.json"
    monkeypatch.setenv("TRACKFLOW_AUTH_DATABASE_PATH", str(auth_path))
    monkeypatch.setenv(
        "TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    checkpoint = tmp_path / "runtime" / "approval.sqlite3"
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH", str(checkpoint))
    application_url = str(
        make_url(isolated_engine.url).update_query_dict(
            {"options": f"-csearch_path={isolated_engine.schema}"}
        ).render_as_string(hide_password=False)
    )
    monkeypatch.setenv("DATABASE_URL", application_url)

    from services.api import auth_database, database as api_database
    from services.api.auth_models import Role, UserCreate, UserInDB, UserUpdate
    from services.api.auth_security import hash_password
    from services.api.auth_services import create_user, update_user
    from services.api.main import app

    previous_application_engine = api_database._engine
    api_database._engine = None
    suffix = uuid4().hex
    password = f"AutoFinal-{suffix}"
    account_specs = {
        "manager": f"auto-final-manager-{suffix}@example.invalid",
        "warehouse": f"auto-final-ana-{suffix}@example.invalid",
        "reverse": f"auto-final-sofia-{suffix}@example.invalid",
        "admin": f"auto-final-admin-{suffix}@example.invalid",
    }
    client_context = TestClient(app)
    entered = False
    try:
        users = {}
        for key, email in account_specs.items():
            if key == "manager":
                user = UserInDB(
                    id="qa-miguel",
                    email=email,
                    hashed_password=hash_password(password),
                    role=Role.manager,
                )
                auth_database.users.insert(user.model_dump(mode="json"))
                users[key] = user
            else:
                users[key] = create_user(UserCreate(email=email, password=password))
        users["admin"] = update_user(
            users["admin"].id, UserUpdate(role=Role.admin)
        )
        client = client_context.__enter__()
        entered = True
        application_engine = api_database._get_engine()
        with application_engine.connect() as connection:
            assert connection.execute(text("SELECT current_schema()" )).scalar_one() == (
                isolated_engine.schema
            )
        accounts = {}
        for key, user in users.items():
            login = client.post(
                "/auth/login",
                data={"username": user.email, "password": password},
            )
            assert login.status_code == 200, login.text
            accounts[key] = closed_gate.Account(
                user.email, user.id, login.json()["access_token"]
            )
        yield AutomaticClosureEnvironment(
            engine=isolated_engine.engine,
            database_url=isolated_engine.url,
            schema=isolated_engine.schema,
            client=client,
            accounts=accounts,
            checkpoint=checkpoint,
        )
    finally:
        if entered:
            client_context.__exit__(None, None, None)
        if api_database._engine is not None:
            api_database._engine.dispose()
        api_database._engine = previous_application_engine
        auth_database.users.truncate()
        auth_database.profiles.truncate()
        auth_database.password_reset_tokens.truncate()


@dataclass(frozen=True)
class AutomaticClosureScenario:
    environment: AutomaticClosureEnvironment
    ticket_id: UUID
    response_run_id: UUID
    approval_run_id: UUID
    warehouse_revision_id: UUID
    reverse: closed_gate.CorrectionScenario


@dataclass(frozen=True)
class IndicativeProposalScenario:
    environment: AutomaticClosureEnvironment
    ticket_id: UUID
    response_run_id: UUID
    approval_run_id: UUID
    warehouse_revision_id: UUID
    reverse_revision_id: UUID


def _build_real_department_history(response_start: RFPResponseStart):
    sources = {}
    evaluations = {}
    previews = {}
    expected_feedback = {
        DepartmentId.warehouse: [],
        DepartmentId.reverse: [
            "Commercial term is pending for indicative human review: "
            "return_processing_rate"
        ],
    }
    for department in (DepartmentId.warehouse, DepartmentId.reverse):
        source = closed_gate.generate_department_draft(
            response_start,
            department=department,
            iteration_count=1,
            generated_at=closed_gate.NOW,
        )
        evaluation, feedback = closed_gate.evaluate_department(
            response_start, source, evaluated_at=closed_gate.NOW
        )
        assert feedback == expected_feedback[department]
        inspection = closed_gate.inspect_department_safety(
            response_start, source, inspected_at=closed_gate.NOW
        )
        preview = closed_gate.build_safe_preview(source, inspection)
        sources[department] = source
        evaluations[department] = evaluation
        previews[department] = preview
    return sources, evaluations, previews


def _reuse_seeded_miguel_identity(
    session: Session, *, account: closed_gate.Account
):
    identity_statement = select(
        rfp_principals.c.principal_id,
        rfp_principals.c.environment,
        rfp_principals.c.trackflow_user_id,
        rfp_principals.c.responsible_code,
        rfp_principals.c.synthetic,
        rfp_principals.c.active.label("principal_active"),
        rfp_identity_bindings.c.binding_id,
        rfp_identity_bindings.c.auth_source,
        rfp_identity_bindings.c.issuer,
        rfp_identity_bindings.c.subject,
        rfp_identity_bindings.c.active.label("binding_active"),
    ).join(
        rfp_identity_bindings,
        rfp_identity_bindings.c.principal_id == rfp_principals.c.principal_id,
    ).where(
        rfp_principals.c.environment == "qa",
        rfp_principals.c.responsible_code == "miguel_torres",
        rfp_identity_bindings.c.environment == "qa",
    )
    row = session.execute(identity_statement).mappings().one()
    assert row["principal_active"] is True
    assert row["binding_active"] is True
    assert row["environment"] == "qa"
    assert row["responsible_code"] == "miguel_torres"
    assert row["trackflow_user_id"] == account.user_id == "qa-miguel"
    assert row["auth_source"] == "local"
    assert row["issuer"] == "urn:trackflow:qa:local"
    assert row["subject"] == account.user_id
    principal_id = row["principal_id"]
    binding_id = row["binding_id"]
    if row["synthetic"] is not True:
        promoted = session.execute(
            update(rfp_principals)
            .where(
                rfp_principals.c.principal_id == row["principal_id"],
                rfp_principals.c.environment == "qa",
                rfp_principals.c.trackflow_user_id == account.user_id,
                rfp_principals.c.responsible_code == "miguel_torres",
                rfp_principals.c.synthetic.is_(False),
            )
            .values(synthetic=True)
        )
        assert promoted.rowcount == 1
        row = session.execute(identity_statement).mappings().one()
    assert row["synthetic"] is True
    assert row["principal_id"] == principal_id
    assert row["binding_id"] == binding_id
    existing_permissions = set(
        session.execute(
            select(rfp_authorizations.c.permission).where(
                rfp_authorizations.c.principal_id == row["principal_id"],
                rfp_authorizations.c.environment == "qa",
            )
        ).scalars()
    )
    assert {"start_approval_run", "arbitrate"}.issubset(existing_permissions)
    return closed_gate.ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="local",
        issuer=row["issuer"],
        subject=row["subject"],
        trackflow_user_id=row["trackflow_user_id"],
        general_role="manager",
        principal_id=row["principal_id"],
        binding_id=row["binding_id"],
        responsible_code="miguel_torres",
        synthetic=bool(row["synthetic"]),
    )


def _persist_real_part2_history(
    environment: AutomaticClosureEnvironment,
    *,
    documented_volume: Decimal | None = None,
    moda_viva: bool = False,
) -> tuple[UUID, UUID]:
    """Persist a real P2 history; no P3 approval or final state is fabricated."""

    engine = environment.engine
    departments = [DepartmentId.warehouse, DepartmentId.reverse]
    ticket_id = uuid4()
    documented_source = SourceReference(line_start=1, line_end=2).model_dump(
        mode="json"
    )
    with Session(engine) as session, session.begin():
        closed_gate._seed_intake_departments(
            session, ticket_id, [item.value for item in departments]
        )
        session.execute(
            update(rfp_metadata)
            .where(rfp_metadata.c.ticket_id == ticket_id)
            .values(
                client_name=("ModaViva" if moda_viva else "Gate 1A QA Client"),
                client_country_code=("ES" if moda_viva else "US"),
                client_country_original=("Spain" if moda_viva else "United States"),
                operation_country_codes=(["ES"] if moda_viva else ["US"]),
                currency=("EUR" if moda_viva else "USD"),
                monthly_volume_value=documented_volume,
                monthly_volume_unit=("m3" if documented_volume is not None else None),
                monthly_volume_source_ref=(
                    documented_source
                    if documented_volume is not None
                    else None
                ),
            )
        )
        if moda_viva:
            source_ref = {"line_start": 1, "line_end": 2}
            session.execute(
                update(rfp_department_sections)
                .where(
                    rfp_department_sections.c.ticket_id == ticket_id,
                    rfp_department_sections.c.department_id == "warehouse",
                )
                .values(
                    key_aspects=[{
                        "fact": "ModaViva requests warehouse storage in Spain.",
                        "requested_action": (
                            "Describe TrackFlow's documented Zaragoza storage service."
                        ),
                        "source_ref": source_ref,
                    }]
                )
            )
            session.execute(
                update(rfp_department_sections)
                .where(
                    rfp_department_sections.c.ticket_id == ticket_id,
                    rfp_department_sections.c.department_id == "reverse",
                )
                .values(
                    key_aspects=[{
                        "fact": "ModaViva requests a documented return inspection process.",
                        "requested_action": (
                            "Describe return inspection and classification in Spain."
                        ),
                        "source_ref": source_ref,
                    }]
                )
            )
            handoff_row = session.execute(
                select(rfp_handoffs).where(rfp_handoffs.c.ticket_id == ticket_id)
            ).mappings().one()
            handoff_payload = dict(handoff_row["payload"])
            handoff_payload["summary_for_sales"] = (
                "ModaViva requests Warehouse and Reverse services in Spain."
            )
            session.execute(
                update(rfp_handoffs)
                .where(rfp_handoffs.c.ticket_id == ticket_id)
                .values(
                    summary_for_sales=handoff_payload["summary_for_sales"],
                    payload=handoff_payload,
                )
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

    sources, evaluations, previews = _build_real_department_history(response_start)

    assert evaluations[DepartmentId.warehouse].overall_pass is True
    assert evaluations[DepartmentId.warehouse].terminal_status == "passed"
    assert sources[DepartmentId.warehouse].pending_commercial_items == []
    assert evaluations[DepartmentId.reverse].overall_pass is False
    assert evaluations[DepartmentId.reverse].terminal_status == "needs_human_input"
    assert {
        item.code
        for item in sources[DepartmentId.reverse].pending_commercial_items
    } == {"RETURN_PROCESSING_RATE_REQUIRED"}

    global_draft = closed_gate.generate_global_terms(
        response_start,
        department_draft_ids=[sources[item].draft_id for item in departments],
        iteration_count=1,
        generated_at=closed_gate.NOW,
    )
    global_inspection = closed_gate.inspect_global_safety(
        response_start, global_draft, inspected_at=closed_gate.NOW
    )
    global_preview = closed_gate.build_global_safe_preview(
        global_draft, global_inspection
    )
    section_currencies = frozenset(
        term.currency
        for source in sources.values()
        for term in source.commercial_terms
        if term.currency is not None
    )
    global_compliance, _global_feedback = closed_gate.evaluate_global(
        response_start,
        global_draft,
        section_currencies=section_currencies,
        consistency_issues=[],
        evaluated_at=closed_gate.NOW,
    )
    global_evaluation = closed_gate.reduce_proposal_evaluations(
        ticket_id=ticket_id,
        run_id=response_run_id,
        section_results=[evaluations[item] for item in departments],
        global_draft_id=global_draft.draft_id,
        global_iteration_count=1,
        global_compliance=global_compliance,
        evaluated_at=closed_gate.NOW,
    )
    assert global_evaluation.outcome == "needs_human_review"
    assert global_evaluation.global_compliance.passed is True
    assert {item.code for item in global_draft.pending_commercial_items} == (
        set() if documented_volume is not None else {"MONTHLY_STORAGE_VOLUME_REQUIRED"}
    )
    assert global_preview.gate_status == "passed"
    assert global_preview.content

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
        ] + list(global_draft.pending_commercial_items),
        created_at=closed_gate.NOW,
    )
    with Session(engine) as session, session.begin():
        manager_identity = _reuse_seeded_miguel_identity(
            session, account=environment.accounts["manager"]
        )
        manage_authorization_id = uuid4()
        session.execute(
            insert(rfp_authorizations).values(
                authorization_id=manage_authorization_id,
                principal_id=manager_identity.principal_id,
                environment="qa",
                permission="manage_approval_run",
                department_scope=None,
                target_policy="run",
                scope_fingerprint=closed_gate.canonical_hash(
                    {
                        "gate": "monthly-storage-volume",
                        "permission": "manage_approval_run",
                        "authorization_id": str(manage_authorization_id),
                    }
                ),
                authorization_version=1,
                active_from=datetime.now(timezone.utc) - timedelta(minutes=1),
                granted_by_principal_id=manager_identity.principal_id,
            )
        )
        closed_gate._insert_additional_authorization(
            session,
            identity=manager_identity,
            permission="provide_business_input",
            department=None,
        )
        closed_gate._insert_principal(
            session,
            account=environment.accounts["warehouse"],
            responsible="ana_whitfield",
            permission="approve_department",
            department="warehouse",
        )
        sofia_identity, _input_reference = closed_gate._insert_principal(
            session,
            account=environment.accounts["reverse"],
            responsible="sofia_ramos",
            permission="provide_business_input",
            department="reverse",
        )
        closed_gate._insert_additional_authorization(
            session,
            identity=sofia_identity,
            permission="approve_department",
            department="reverse",
        )
        for department in departments:
            source = sources[department]
            evaluation = evaluations[department]
            preview = previews[department]
            source_payload = closed_gate.canonical_value(source)
            evaluation_payload = closed_gate.canonical_value(evaluation)
            session.execute(
                insert(rfp_response_drafts).values(
                    draft_id=source.draft_id,
                    run_id=response_run_id,
                    scope_key=department.value,
                    iteration_count=1,
                    contract_version=source.contract_version,
                    payload=source_payload,
                    payload_hash=closed_gate.canonical_hash(source_payload),
                )
            )
            session.execute(
                insert(rfp_response_evaluations).values(
                    evaluation_id=evaluation.evaluation_id,
                    run_id=response_run_id,
                    draft_id=source.draft_id,
                    scope_key=department.value,
                    contract_version=evaluation.contract_version,
                    overall_pass=evaluation.overall_pass,
                    terminal_status=evaluation.terminal_status,
                    payload=evaluation_payload,
                    payload_hash=closed_gate.canonical_hash(evaluation_payload),
                )
            )
            session.execute(
                insert(rfp_response_previews).values(
                    preview_id=preview.preview_id,
                    draft_id=source.draft_id,
                    gate_status=preview.gate_status,
                    safe_content=preview.content,
                    reason_codes=preview.reason_codes,
                    created_at=closed_gate.NOW,
                )
            )

        global_draft_payload = closed_gate.canonical_value(global_draft)
        global_evaluation_payload = closed_gate.canonical_value(global_evaluation)
        session.execute(
            insert(rfp_response_drafts).values(
                draft_id=global_draft.draft_id,
                run_id=response_run_id,
                scope_key="proposal",
                iteration_count=1,
                contract_version=global_draft.contract_version,
                payload=global_draft_payload,
                payload_hash=closed_gate.canonical_hash(global_draft_payload),
            )
        )
        session.execute(
            insert(rfp_response_evaluations).values(
                evaluation_id=global_evaluation.evaluation_id,
                run_id=response_run_id,
                draft_id=global_draft.draft_id,
                scope_key="proposal",
                contract_version=global_evaluation.contract_version,
                overall_pass=global_evaluation.overall_pass,
                terminal_status=global_evaluation.terminal_status,
                payload=global_evaluation_payload,
                payload_hash=closed_gate.canonical_hash(global_evaluation_payload),
            )
        )
        session.execute(
            insert(rfp_response_previews).values(
                preview_id=global_preview.preview_id,
                draft_id=global_draft.draft_id,
                gate_status=global_preview.gate_status,
                safe_content=global_preview.content,
                reason_codes=global_preview.reason_codes,
                created_at=closed_gate.NOW,
            )
        )
        handoff_payload = closed_gate.canonical_value(handoff)
        session.execute(
            insert(rfp_response_handoffs).values(
                run_id=response_run_id,
                ticket_id=ticket_id,
                contract_version=handoff.contract_version,
                outcome=handoff.outcome,
                payload=handoff_payload,
                payload_hash=closed_gate.canonical_hash(handoff_payload),
                created_at=closed_gate.NOW,
            )
        )
        session.execute(
            update(rfp_response_runs)
            .where(rfp_response_runs.c.run_id == response_run_id)
            .values(status="needs_human_review")
        )
        session.execute(
            update(rfp_tickets)
            .where(rfp_tickets.c.ticket_id == ticket_id)
            .values(status="needs_human_review")
        )
    return ticket_id, response_run_id


def _start_delegated_approval_run(
    environment: AutomaticClosureEnvironment,
    *,
    documented_volume: Decimal | None = None,
) -> AutomaticClosureScenario:
    ticket_id, response_run_id = _persist_real_part2_history(
        environment, documented_volume=documented_volume
    )
    initialized = environment.client.post(
        f"/rfps/{ticket_id}/approval",
        headers=environment.headers("manager"),
        json={
            "idempotency_key": str(uuid4()),
            "automate_proposal_materialization": True,
        },
    )
    assert initialized.status_code == 200, initialized.text
    payload = initialized.json()
    assert payload["proposal_automation_delegated"] is True
    assert UUID(payload["response_run_id"]) == response_run_id
    approval_run_id = UUID(payload["approval_run_id"])
    revisions = {item["department"]: item for item in payload["revisions"]}
    assert set(revisions) == {"warehouse", "reverse"}
    assert revisions["warehouse"]["status"] == "pending_assessment"
    assert revisions["reverse"]["status"] == "blocked_input"

    reverse_revision_id = UUID(revisions["reverse"]["revision_id"])
    with Session(environment.engine) as session:
        reverse_row = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.revision_id
                == reverse_revision_id
            )
        ).mappings().one()
        run = session.execute(
            select(rfp_approval_runs).where(
                rfp_approval_runs.c.approval_run_id == approval_run_id
            )
        ).mappings().one()
    assert run["automate_proposal_materialization"] is True
    assert run["active_departments"] == ["warehouse", "reverse"]
    assert run["response_run_id"] == response_run_id
    assert run["start_authorization_id"] is not None
    assert run["started_by_principal_id"] is not None
    assert run["started_by_binding_id"] is not None

    reverse = closed_gate.CorrectionScenario(
        engine=environment.engine,
        database_url=environment.database_url,
        schema=environment.schema,
        client=environment.client,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        approval_run_id=approval_run_id,
        revision_id=reverse_revision_id,
        validation_bundle_id=reverse_row["validation_bundle_id"],
        content_hash=reverse_row["content_hash"],
        state_version=reverse_row["state_version"],
        department="reverse",
        service_code="return_processing_rate",
        pending_code="RETURN_PROCESSING_RATE_REQUIRED",
        accounts=environment.accounts,
        checkpoint=environment.checkpoint,
        initial_hitl=False,
    )
    return AutomaticClosureScenario(
        environment=environment,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        approval_run_id=approval_run_id,
        warehouse_revision_id=UUID(revisions["warehouse"]["revision_id"]),
        reverse=reverse,
    )


def _start_indicative_modaviva_run(
    environment: AutomaticClosureEnvironment,
) -> IndicativeProposalScenario:
    ticket_id, response_run_id = _persist_real_part2_history(
        environment,
        documented_volume=None,
        moda_viva=True,
    )
    initialized = environment.client.post(
        f"/rfps/{ticket_id}/approval",
        headers=environment.headers("manager"),
        json={
            "idempotency_key": str(uuid4()),
            "automate_proposal_materialization": True,
        },
    )
    assert initialized.status_code == 200, initialized.text
    payload = initialized.json()
    assert payload["proposal_automation_delegated"] is True
    assert UUID(payload["response_run_id"]) == response_run_id
    revisions = {item["department"]: item for item in payload["revisions"]}
    assert set(revisions) == {"warehouse", "reverse"}
    assert {
        department: item["status"] for department, item in revisions.items()
    } == {
        "warehouse": "pending_assessment",
        "reverse": "pending_assessment",
    }
    return IndicativeProposalScenario(
        environment=environment,
        ticket_id=ticket_id,
        response_run_id=response_run_id,
        approval_run_id=UUID(payload["approval_run_id"]),
        warehouse_revision_id=UUID(revisions["warehouse"]["revision_id"]),
        reverse_revision_id=UUID(revisions["reverse"]["revision_id"]),
    )


def _monthly_volume_request(value: str, *, unit: str = "m3") -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "input_id": str(uuid4()),
        "input_kind": "monthly_storage_volume",
        "payload": {
            "input_kind": "monthly_storage_volume",
            "value": value,
            "dimension": "volume",
            "unit": unit,
            "period": "month",
            "operational_scope": {
                "location_code": None,
                "service_code": "warehouse-storage",
                "scope_code": "monthly-storage-volume",
            },
            "effective_from": now.date().isoformat(),
            "effective_until": None,
        },
        "provenance": {
            "evidence_type": "responsible_attestation",
            "evidence_id": str(uuid4()),
            "source_name": "Authorized isolated QA storage forecast",
            "issued_at": now.isoformat(),
            "valid_until": None,
            "source_payload_hash": closed_gate.canonical_hash(
                {"qa_monthly_storage_volume": str(uuid4())}
            ),
            "notes": None,
        },
        "idempotency_key": str(uuid4()),
        "recorded_at": now.isoformat(),
    }


def test_indicative_modaviva_needs_human_review_reaches_final_document(
    auto_closure_environment: AutomaticClosureEnvironment,
) -> None:
    scenario = _start_indicative_modaviva_run(auto_closure_environment)
    base = f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}"
    revision_ids = {
        "warehouse": scenario.warehouse_revision_id,
        "reverse": scenario.reverse_revision_id,
    }
    independent = create_engine(
        scenario.environment.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={scenario.environment.schema}"},
    )
    try:
        with independent.connect() as connection:
            assert connection.execute(text("SELECT current_schema()" )).scalar_one() == (
                scenario.environment.schema
            )
            metadata = connection.execute(
                select(rfp_metadata).where(rfp_metadata.c.ticket_id == scenario.ticket_id)
            ).mappings().one()
            assert metadata["client_name"] == "ModaViva"
            assert metadata["client_country_code"] == "ES"
            assert metadata["operation_country_codes"] == ["ES"]
            assert metadata["currency"] == "EUR"
            assert metadata["monthly_volume_value"] is None
            assert metadata["monthly_volume_unit"] is None
            ticket = connection.execute(
                select(rfp_tickets).where(rfp_tickets.c.ticket_id == scenario.ticket_id)
            ).mappings().one()
            sections = connection.execute(
                select(rfp_department_sections).where(
                    rfp_department_sections.c.ticket_id == scenario.ticket_id
                )
            ).mappings().all()
            assert ticket["raw_pdf_path"].endswith("/source.pdf")
            assert {row["department_id"] for row in sections} == {
                "warehouse",
                "reverse",
            }
            assert all(row["key_aspects"] for row in sections)

            response_run = connection.execute(
                select(rfp_response_runs).where(
                    rfp_response_runs.c.run_id == scenario.response_run_id
                )
            ).mappings().one()
            handoff = connection.execute(
                select(rfp_response_handoffs).where(
                    rfp_response_handoffs.c.run_id == scenario.response_run_id
                )
            ).mappings().one()
            assert response_run["status"] == "needs_human_review"
            assert handoff["outcome"] == "needs_human_review"
            assert handoff["payload"]["all_sections_passed"] is False
            assert {
                item["code"] for item in handoff["payload"]["unresolved_items"]
            } == {
                "MONTHLY_STORAGE_VOLUME_REQUIRED",
                "RETURN_PROCESSING_RATE_REQUIRED",
            }

            drafts = connection.execute(
                select(rfp_response_drafts).where(
                    rfp_response_drafts.c.run_id == scenario.response_run_id
                )
            ).mappings().all()
            evaluations = connection.execute(
                select(rfp_response_evaluations).where(
                    rfp_response_evaluations.c.run_id == scenario.response_run_id
                )
            ).mappings().all()
            assert {row["scope_key"] for row in drafts} == {
                "warehouse",
                "reverse",
                "proposal",
            }
            by_scope = {row["scope_key"]: row for row in evaluations}
            assert by_scope["warehouse"]["overall_pass"] is True
            assert by_scope["warehouse"]["terminal_status"] == "passed"
            assert by_scope["reverse"]["overall_pass"] is False
            assert by_scope["reverse"]["terminal_status"] == "needs_human_input"
            assert by_scope["proposal"]["overall_pass"] is False
            assert by_scope["proposal"]["payload"]["outcome"] == (
                "needs_human_review"
            )
            assert by_scope["proposal"]["terminal_status"] == "needs_human_input"
            assert connection.execute(
                select(func.count()).select_from(rfp_authorized_business_inputs).where(
                    rfp_authorized_business_inputs.c.approval_run_id
                    == scenario.approval_run_id
                )
            ).scalar_one() == 0

        assessed = scenario.environment.client.post(
            f"{base}/assess-and-start",
            headers=scenario.environment.headers("manager"),
        )
        assert assessed.status_code == 200, assessed.text
        ready = {item["department"]: item for item in assessed.json()["ready_revisions"]}
        assert set(ready) == {"warehouse", "reverse"}
        assert all(
            item["status"] == "pending_approval" and item["ready_for_hitl"] is True
            for item in ready.values()
        )

        reviews = {}
        workflows = {}
        for department, revision_id in revision_ids.items():
            headers = scenario.environment.headers(department)
            review = scenario.environment.client.get(
                f"{base}/departments/{department}/revisions/{revision_id}/review",
                headers=headers,
            )
            workflow = scenario.environment.client.get(
                f"{base}/departments/{department}/revisions/{revision_id}",
                headers=headers,
            )
            assert review.status_code == 200, review.text
            assert workflow.status_code == 200, workflow.text
            reviews[department] = review.json()
            workflows[department] = workflow.json()
            assert workflow.json()["status"] == "awaiting_human_decision"
            assert workflow.json()["interrupted"] is True
        assert workflows["warehouse"]["thread_id"] != workflows["reverse"][
            "thread_id"
        ]
        assert reviews["warehouse"]["pending_commercial_items"] == []
        assert {
            item["code"] for item in reviews["reverse"]["pending_commercial_items"]
        } == {"RETURN_PROCESSING_RATE_REQUIRED"}
        assert "RETURN_PROCESSING_RATE_REQUIRED" in reviews["reverse"]["safe_preview"]
        assert scenario.environment.checkpoint.is_file()
        assert scenario.environment.checkpoint.stat().st_size > 0

        ana_inbox = scenario.environment.client.get(
            "/rfps/approval/inbox",
            headers=scenario.environment.headers("warehouse"),
        )
        sofia_inbox = scenario.environment.client.get(
            "/rfps/approval/inbox",
            headers=scenario.environment.headers("reverse"),
        )
        assert [item["revision_id"] for item in ana_inbox.json()["items"]] == [
            str(scenario.warehouse_revision_id)
        ]
        assert [item["revision_id"] for item in sofia_inbox.json()["items"]] == [
            str(scenario.reverse_revision_id)
        ]

        warehouse_url = (
            f"{base}/departments/warehouse/revisions/"
            f"{scenario.warehouse_revision_id}/decision"
        )
        reverse_url = (
            f"{base}/departments/reverse/revisions/"
            f"{scenario.reverse_revision_id}/decision"
        )
        before_rejections = closed_gate._snapshot(
            scenario.environment.engine,
            scenario.approval_run_id,
            scenario.ticket_id,
        )
        missing_ack = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        wrong_actor = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("warehouse"),
            json={
                "action": "approve",
                "acknowledge_pending_commercial_items": True,
                "reason": "Indicative terms reviewed.",
            },
        )
        assert missing_ack.status_code == 422
        assert missing_ack.json()["detail"] == (
            "pending_commercial_items_acknowledgement_required"
        )
        assert wrong_actor.status_code == 403
        assert closed_gate._snapshot(
            scenario.environment.engine,
            scenario.approval_run_id,
            scenario.ticket_id,
        ) == before_rejections

        ana = scenario.environment.client.post(
            warehouse_url,
            headers=scenario.environment.headers("warehouse"),
            json={"action": "approve", "reason": None},
        )
        assert ana.status_code == 200, ana.text
        assert ana.json()["application"]["revision_status"] == "approved"
        assert ana.json()["coordination"]["outcome"] == "waiting"
        assert ana.json()["coordination"]["final_document_id"] is None
        reverse_waiting = scenario.environment.client.get(
            f"{base}/departments/reverse/revisions/{scenario.reverse_revision_id}",
            headers=scenario.environment.headers("reverse"),
        )
        assert reverse_waiting.json()["interrupted"] is True
        with independent.connect() as connection:
            assert connection.execute(
                select(func.count()).select_from(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == scenario.approval_run_id
                )
            ).scalar_one() == 0
            assert connection.execute(
                select(rfp_tickets.c.status).where(
                    rfp_tickets.c.ticket_id == scenario.ticket_id
                )
            ).scalar_one() == "esperando_aprobación"

        sofia = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("reverse"),
            json={
                "action": "approve",
                "acknowledge_pending_commercial_items": True,
                "reason": "Indicative commercial terms reviewed.",
            },
        )
        assert sofia.status_code == 200, sofia.text
        result = sofia.json()
        assert result["application"]["revision_status"] == "approved"
        assert result["coordination"]["outcome"] == "finalized"
        assert result["coordination"]["blockers"] == []
        final_document_id = UUID(result["coordination"]["final_document_id"])

        document_response = scenario.environment.client.get(
            f"{base}/document",
            headers=scenario.environment.headers("manager"),
        )
        assert document_response.status_code == 200, document_response.text
        document = document_response.json()
        assert document["final_document_id"] == str(final_document_id)
        assert document["title"].startswith("TrackFlow indicative logistics proposal")
        assert "Indicative, non-binding proposal" in document["markdown"]
        assert document["currency"] == {"value": "EUR", "status": "documented"}
        assert {item["department"] for item in document["sections"]} == {
            "warehouse",
            "reverse",
        }
        assert "RETURN_PROCESSING_RATE_REQUIRED" in document["markdown"]
        assert document["volume_discounts"] == [{
            "volume_from": 50.0,
            "volume_to": None,
            "discount_percentage": None,
            "condition": (
                "Average monthly storage volume above 50 cubic meters is eligible "
                "for negotiation with Miguel Torres; no percentage is documented."
            ),
            "status": "pending",
        }]
        assert "Pending — no percentage authorized" in document["markdown"]
        assert "None%" not in document["markdown"]
        assert "| 0% |" not in document["markdown"]

        with independent.connect() as connection:
            run = connection.execute(
                select(rfp_approval_runs).where(
                    rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
                )
            ).mappings().one()
            assert run["status"] == "completed"
            assert connection.execute(
                select(rfp_tickets.c.status).where(
                    rfp_tickets.c.ticket_id == scenario.ticket_id
                )
            ).scalar_one() == "terminado"
            revisions = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id.in_(
                        list(revision_ids.values())
                    )
                )
            ).mappings().all()
            assert {row["status"] for row in revisions} == {"approved"}
            decisions = connection.execute(
                select(rfp_human_decisions).where(
                    rfp_human_decisions.c.revision_id.in_(list(revision_ids.values()))
                )
            ).mappings().all()
            assert len(decisions) == 2
            assert {row["processing_status"] for row in decisions} == {"applied"}
            reverse_decision = next(
                row for row in decisions if row["department_id"] == "reverse"
            )
            assert reverse_decision["reason"] == (
                "[indicative-commercial-ack:v1] RETURN_PROCESSING_RATE_REQUIRED | "
                "Indicative commercial terms reviewed."
            )
            proposal = connection.execute(
                select(rfp_approval_proposal_revisions).where(
                    rfp_approval_proposal_revisions.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_approval_proposal_revisions.c.is_current.is_(True),
                )
            ).mappings().one()
            assert proposal["eligible_for_synthesis"] is True
            assert proposal["warehouse_revision_id"] == scenario.warehouse_revision_id
            assert proposal["reverse_revision_id"] == scenario.reverse_revision_id
            assert proposal["lastmile_revision_id"] is None
            assert proposal["validation_payload"]["unresolved_input_codes"] == [
                "MONTHLY_STORAGE_VOLUME_REQUIRED"
            ]
            assert proposal["content_payload"]["authorized_input_ids"] == []
            final_row = connection.execute(
                select(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == scenario.approval_run_id
                )
            ).mappings().one()
            assert connection.execute(
                select(func.count()).select_from(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == scenario.approval_run_id
                )
            ).scalar_one() == 1
            assert final_row["payload_hash"] == closed_gate.canonical_hash(
                final_row["payload"]
            )
            assert final_row["markdown_hash"] == hashlib.sha256(
                final_row["markdown"].encode("utf-8")
            ).hexdigest()
            manifest = final_row["payload"]["manifest"]
            assert manifest["department_revision_ids"] == [
                str(scenario.warehouse_revision_id),
                str(scenario.reverse_revision_id),
            ]
            assert manifest["approval_decision_ids"] == [
                str(next(row["decision_id"] for row in decisions if row["department_id"] == "warehouse")),
                str(reverse_decision["decision_id"]),
            ]
            assert manifest["authorized_input_ids"] == []

        snapshot = closed_gate._snapshot(
            scenario.environment.engine,
            scenario.approval_run_id,
            scenario.ticket_id,
        )
        replay = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("reverse"),
            json={
                "action": "approve",
                "acknowledge_pending_commercial_items": True,
                "reason": "Indicative commercial terms reviewed.",
            },
        )
        assert replay.status_code == 409
        assert replay.json()["detail"] == "workflow_not_interrupted"
        reconcile = scenario.environment.client.post(
            f"{base}/reconcile",
            headers=scenario.environment.headers("manager"),
            json={
                "revision_id": str(scenario.warehouse_revision_id),
                "department": "warehouse",
            },
        )
        assert reconcile.status_code == 200, reconcile.text
        assert reconcile.json()["coordination"]["outcome"] == "finalized"
        assert UUID(reconcile.json()["coordination"]["final_document_id"]) == (
            final_document_id
        )
        assert closed_gate._snapshot(
            scenario.environment.engine,
            scenario.approval_run_id,
            scenario.ticket_id,
        ) == snapshot
    finally:
        independent.dispose()


def test_last_human_approval_automatically_persists_final_document(
    auto_closure_environment: AutomaticClosureEnvironment,
) -> None:
    scenario = _start_delegated_approval_run(auto_closure_environment)
    monthly_request = _monthly_volume_request("20.0000")
    monthly_url = (
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/"
        "business-inputs"
    )
    denied = scenario.environment.client.post(
        monthly_url,
        headers=scenario.environment.headers("admin"),
        json=monthly_request,
    )
    assert denied.status_code == 403
    incompatible = json.loads(json.dumps(monthly_request))
    incompatible["input_id"] = str(uuid4())
    incompatible["idempotency_key"] = str(uuid4())
    incompatible["payload"]["unit"] = "ft3"
    rejected = scenario.environment.client.post(
        monthly_url,
        headers=scenario.environment.headers("manager"),
        json=incompatible,
    )
    assert rejected.status_code == 422
    with Session(scenario.environment.engine) as session:
        assert session.execute(
            select(func.count()).select_from(rfp_authorized_business_inputs).where(
                rfp_authorized_business_inputs.c.input_kind
                == "monthly_storage_volume"
            )
        ).scalar_one() == 0
    monthly_response = scenario.environment.client.post(
        monthly_url,
        headers=scenario.environment.headers("manager"),
        json=monthly_request,
    )
    assert monthly_response.status_code == 200, monthly_response.text
    monthly_input_id = UUID(monthly_response.json()["input_id"])
    _request, recorded, _body, corrected = closed_gate._authorized_correction(
        scenario.reverse
    )
    reverse_b_id = UUID(corrected["revision_id"])
    assert corrected["status"] == "pending_approval"
    assert corrected["remaining_input_codes"] == []

    with Session(scenario.environment.engine) as session:
        lineage = session.execute(
            select(rfp_approval_department_revisions)
            .where(
                rfp_approval_department_revisions.c.approval_run_id
                == scenario.approval_run_id
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
        for row in lineage
    } == {
        ("reverse", 1): ("superseded", False),
        ("reverse", 2): ("pending_approval", True),
        ("warehouse", 1): ("pending_assessment", True),
    }
    reverse_b = next(row for row in lineage if row["revision_id"] == reverse_b_id)
    assert reverse_b["supersedes_revision_id"] == scenario.reverse.revision_id

    assessed = scenario.environment.client.post(
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/assess-and-start",
        headers=scenario.environment.headers("manager"),
    )
    assert assessed.status_code == 200, assessed.text
    ready = {item["department"]: item for item in assessed.json()["ready_revisions"]}
    assert set(ready) == {"warehouse", "reverse"}
    assert UUID(ready["warehouse"]["revision_id"]) == scenario.warehouse_revision_id
    assert UUID(ready["reverse"]["revision_id"]) == reverse_b_id
    assert all(
        item["status"] == "pending_approval" and item["ready_for_hitl"] is True
        for item in ready.values()
    )

    workflow_states = {}
    for department, revision_id in (
        ("warehouse", scenario.warehouse_revision_id),
        ("reverse", reverse_b_id),
    ):
        workflow = scenario.environment.client.get(
            f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/departments/"
            f"{department}/revisions/{revision_id}",
            headers=scenario.environment.headers(department),
        )
        assert workflow.status_code == 200, workflow.text
        workflow_states[department] = workflow.json()
        assert workflow.json()["status"] == "awaiting_human_decision"
        assert workflow.json()["interrupted"] is True
    assert workflow_states["warehouse"]["thread_id"] != workflow_states["reverse"][
        "thread_id"
    ]
    assert scenario.environment.checkpoint.is_file()
    assert scenario.environment.checkpoint.stat().st_size > 0

    warehouse_url = (
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/departments/"
        f"warehouse/revisions/{scenario.warehouse_revision_id}/decision"
    )
    reverse_url = (
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/departments/"
        f"reverse/revisions/{reverse_b_id}/decision"
    )
    independent = create_engine(
        scenario.reverse.database_url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={scenario.reverse.schema}"},
    )
    try:
        before_document = scenario.environment.client.get(
            f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/document",
            headers=scenario.environment.headers("manager"),
        )
        assert before_document.status_code == 404

        ana = scenario.environment.client.post(
            warehouse_url,
            headers=scenario.environment.headers("warehouse"),
            json={"action": "approve", "reason": None},
        )
        assert ana.status_code == 200, ana.text
        assert ana.json()["application"]["revision_status"] == "approved"
        assert ana.json()["coordination"]["outcome"] == "waiting"
        assert ana.json()["coordination"]["final_document_id"] is None

        reverse_state = scenario.environment.client.get(
            f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/departments/"
            f"reverse/revisions/{reverse_b_id}",
            headers=scenario.environment.headers("reverse"),
        )
        assert reverse_state.status_code == 200, reverse_state.text
        assert reverse_state.json()["interrupted"] is True
        assert reverse_state.json()["decision"] is None
        with independent.connect() as connection:
            assert connection.execute(
                select(func.count()).select_from(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == scenario.approval_run_id
                )
            ).scalar_one() == 0
            assert connection.execute(
                select(rfp_tickets.c.status).where(
                    rfp_tickets.c.ticket_id == scenario.ticket_id
                )
            ).scalar_one() == "esperando_aprobación"

        sofia = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert sofia.status_code == 200, sofia.text
        result = sofia.json()
        assert result["application"]["revision_status"] == "approved"
        assert result["coordination"]["outcome"] == "finalized"
        assert result["coordination"]["blockers"] == []
        final_document_id = UUID(result["coordination"]["final_document_id"])

        locator = scenario.environment.client.get(
            f"/rfps/{scenario.ticket_id}/approval/document-locator",
            headers=scenario.environment.headers("manager"),
        )
        assert locator.status_code == 200, locator.text
        assert UUID(locator.json()["approval_run_id"]) == scenario.approval_run_id
        assert UUID(locator.json()["final_document_id"]) == final_document_id
        document_response = scenario.environment.client.get(
            f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/document",
            headers=scenario.environment.headers("manager"),
        )
        assert document_response.status_code == 200, document_response.text
        document_public = document_response.json()
        assert document_public["currency"] == {"value": "USD", "status": "documented"}
        assert {item["department"] for item in document_public["sections"]} == {
            "warehouse",
            "reverse",
        }
        assert document_public["markdown"].startswith("# ")
        assert document_public["volume_discounts"] == [
            {
                "volume_from": 50.0,
                "volume_to": None,
                "discount_percentage": None,
                "condition": (
                    "Average monthly storage volume above 50 cubic meters is "
                    "eligible for negotiation with Miguel Torres; no percentage "
                    "is documented."
                ),
                "status": "pending",
            }
        ]
        assert "Pending — no percentage authorized" in document_public["markdown"]
        assert "None%" not in document_public["markdown"]
        assert "| 0% |" not in document_public["markdown"]

        with independent.connect() as connection:
            run = connection.execute(
                select(rfp_approval_runs).where(
                    rfp_approval_runs.c.approval_run_id == scenario.approval_run_id
                )
            ).mappings().one()
            assert run["status"] == "completed"
            assert run["completed_at"] is not None
            assert run["automate_proposal_materialization"] is True
            delegation = connection.execute(
                select(rfp_authorizations).where(
                    rfp_authorizations.c.authorization_id
                    == run["start_authorization_id"]
                )
            ).mappings().one()
            assert delegation["authorization_id"] == run["start_authorization_id"]
            assert delegation["authorization_version"] == run["authorization_version"]
            assert delegation["principal_id"] == run["started_by_principal_id"]
            assert delegation["environment"] == run["environment"] == "qa"
            assert delegation["permission"] == "manage_approval_run"
            assert delegation["department_scope"] is None
            assert delegation["trigger_scope"] is None
            assert delegation["target_policy"] == "run"
            assert delegation["revoked_at"] is None
            assert delegation["active_from"] <= run["completed_at"]
            assert (
                delegation["expires_at"] is None
                or delegation["expires_at"] > run["completed_at"]
            )
            binding = connection.execute(
                select(rfp_identity_bindings).where(
                    rfp_identity_bindings.c.binding_id
                    == run["started_by_binding_id"],
                    rfp_identity_bindings.c.principal_id
                    == run["started_by_principal_id"],
                    rfp_identity_bindings.c.environment == run["environment"],
                )
            ).mappings().one()
            assert binding["active"] is True
            assert binding["disabled_at"] is None
            assert connection.execute(
                select(rfp_tickets.c.status).where(
                    rfp_tickets.c.ticket_id == scenario.ticket_id
                )
            ).scalar_one() == "terminado"
            proposal = connection.execute(
                select(rfp_approval_proposal_revisions).where(
                    rfp_approval_proposal_revisions.c.approval_run_id
                    == scenario.approval_run_id,
                    rfp_approval_proposal_revisions.c.is_current.is_(True),
                )
            ).mappings().one()
            assert proposal["eligible_for_synthesis"] is True
            assert proposal["warehouse_revision_id"] == scenario.warehouse_revision_id
            assert proposal["reverse_revision_id"] == reverse_b_id
            assert proposal["lastmile_revision_id"] is None
            assert proposal["content_payload"]["source_department_revision_ids"] == [
                str(scenario.warehouse_revision_id),
                str(reverse_b_id),
            ]
            assert proposal["content_payload"]["authorized_input_ids"] == [
                str(monthly_input_id)
            ]
            persisted_volume = connection.execute(
                select(rfp_authorized_business_inputs).where(
                    rfp_authorized_business_inputs.c.input_id == monthly_input_id
                )
            ).mappings().one()
            volume = MonthlyStorageVolumeInput.model_validate_json(
                json.dumps(persisted_volume["payload"])
            )
            provenance = BusinessInputProvenance.model_validate_json(
                json.dumps(persisted_volume["provenance"])
            )
            assert str(volume.value) == "20.0000"
            assert (volume.unit, volume.period) == ("m3", "month")
            assert volume.operational_scope.scope_code == "monthly-storage-volume"
            assert (
                closed_gate.canonical_hash(provenance)
                == persisted_volume["provenance_hash"]
            )
            assert (
                provenance.source_payload_hash == persisted_volume["evidence_hash"]
            )
            assert connection.execute(
                select(func.count())
                .select_from(rfp_approval_proposal_revisions)
                .where(
                    rfp_approval_proposal_revisions.c.approval_run_id
                    == scenario.approval_run_id
                )
            ).scalar_one() == 1
            final_row = connection.execute(
                select(rfp_final_documents).where(
                    rfp_final_documents.c.approval_run_id == scenario.approval_run_id
                )
            ).mappings().one()
            assert connection.execute(
                select(func.count())
                .select_from(rfp_final_documents)
                .where(
                    rfp_final_documents.c.approval_run_id
                    == scenario.approval_run_id
                )
            ).scalar_one() == 1
            assert final_row["final_document_id"] == final_document_id
            assert final_row["proposal_revision_id"] == proposal["proposal_revision_id"]
            assert final_row["warehouse_revision_id"] == scenario.warehouse_revision_id
            assert final_row["reverse_revision_id"] == reverse_b_id
            assert final_row["lastmile_revision_id"] is None
            assert final_row["payload_hash"] == closed_gate.canonical_hash(
                final_row["payload"]
            )
            assert final_row["markdown_hash"] == hashlib.sha256(
                final_row["markdown"].encode("utf-8")
            ).hexdigest()
            manifest = final_row["payload"]["manifest"]
            assert manifest["ticket_id"] == str(scenario.ticket_id)
            assert manifest["approval_run_id"] == str(scenario.approval_run_id)
            assert manifest["response_run_id"] == str(scenario.response_run_id)
            assert manifest["proposal_revision_id"] == str(
                proposal["proposal_revision_id"]
            )
            assert manifest["department_revision_ids"] == [
                str(scenario.warehouse_revision_id),
                str(reverse_b_id),
            ]
            assert manifest["authorized_input_ids"] == [str(monthly_input_id)]
            assert final_row["payload"]["global_terms"][
                "proposal_content_hash"
            ] == proposal["content_hash"]
            assert final_row["payload"]["global_terms"][
                "volume_discounts"
            ] == proposal["content_payload"]["volume_discounts"]
            authorized_input = connection.execute(
                select(rfp_authorized_business_inputs).where(
                    rfp_authorized_business_inputs.c.input_id
                    == UUID(recorded["input_id"])
                )
            ).mappings().one()
            assert authorized_input["payload_hash"] == recorded["payload_hash"]
            reverse_b = connection.execute(
                select(rfp_approval_department_revisions).where(
                    rfp_approval_department_revisions.c.revision_id == reverse_b_id
                )
            ).mappings().one()
            assert (
                reverse_b["content_payload"]["creation_reason"] == "authorized_input"
            )
            reverse_bundle = connection.execute(
                select(rfp_approval_validation_bundles).where(
                    rfp_approval_validation_bundles.c.validation_bundle_id
                    == reverse_b["validation_bundle_id"]
                )
            ).mappings().one()
            reverse_draft = connection.execute(
                select(rfp_approval_corrected_drafts).where(
                    rfp_approval_corrected_drafts.c.draft_artifact_id
                    == reverse_bundle["corrected_draft_id"]
                )
            ).mappings().one()
            assert reverse_bundle["content_hash"] == reverse_b["content_hash"]
            assert reverse_draft["content_hash"] == reverse_b["content_hash"]
            assert reverse_draft["provenance"]["source_kind"] == "human_correction"
            assert reverse_draft["provenance"]["source_artifact_ids"] == [
                recorded["input_id"]
            ]
            assert reverse_draft["provenance"]["source_hashes"] == [
                recorded["payload_hash"]
            ]
            revision_hashes = dict(
                connection.execute(
                    select(
                        rfp_approval_department_revisions.c.revision_id,
                        rfp_approval_department_revisions.c.content_hash,
                    ).where(
                        rfp_approval_department_revisions.c.revision_id.in_(
                            [scenario.warehouse_revision_id, reverse_b_id]
                        )
                    )
                ).all()
            )
            decisions = connection.execute(
                select(rfp_human_decisions).where(
                    rfp_human_decisions.c.revision_id.in_(
                        [scenario.warehouse_revision_id, reverse_b_id]
                    )
                )
            ).mappings().all()
            assert len(decisions) == 2
            assert {row["processing_status"] for row in decisions} == {"applied"}
            assert {
                row["revision_id"]: row["expected_content_hash"]
                for row in decisions
            } == revision_hashes
            decision_ids = {
                row["department_id"]: str(row["decision_id"])
                for row in decisions
            }
            assert manifest["approval_decision_ids"] == [
                decision_ids["warehouse"],
                decision_ids["reverse"],
            ]
            finalization_events = dict(
                connection.execute(
                    select(
                        rfp_workflow_events.c.event_type,
                        func.count(),
                    )
                    .where(
                        rfp_workflow_events.c.approval_run_id
                        == scenario.approval_run_id,
                        rfp_workflow_events.c.event_type.in_(
                            [
                                "fan_in_evaluated",
                                "final_document_synthesized",
                                "final_document_persisted_ticket_closed",
                            ]
                        ),
                    )
                    .group_by(rfp_workflow_events.c.event_type)
                ).all()
            )
            assert finalization_events == {
                "fan_in_evaluated": 1,
                "final_document_synthesized": 1,
                "final_document_persisted_ticket_closed": 1,
            }

        snapshot = closed_gate._snapshot(
            scenario.environment.engine, scenario.approval_run_id, scenario.ticket_id
        )
        with Session(scenario.environment.engine) as session:
            closure_snapshot = {
                "proposals": closed_gate.canonical_hash(
                    [
                        dict(row)
                        for row in session.execute(
                            select(rfp_approval_proposal_revisions).where(
                                rfp_approval_proposal_revisions.c.approval_run_id
                                == scenario.approval_run_id
                            )
                        ).mappings().all()
                    ]
                ),
                "documents": closed_gate.canonical_hash(
                    [
                        dict(row)
                        for row in session.execute(
                            select(rfp_final_documents).where(
                                rfp_final_documents.c.approval_run_id
                                == scenario.approval_run_id
                            )
                        ).mappings().all()
                    ]
                ),
            }
        decision_replay = scenario.environment.client.post(
            reverse_url,
            headers=scenario.environment.headers("reverse"),
            json={"action": "approve", "reason": None},
        )
        assert decision_replay.status_code == 409
        assert decision_replay.json()["detail"] == "workflow_not_interrupted"
        reconcile = scenario.environment.client.post(
            f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/reconcile",
            headers=scenario.environment.headers("manager"),
            json={
                "revision_id": str(scenario.warehouse_revision_id),
                "department": "warehouse",
            },
        )
        assert reconcile.status_code == 200, reconcile.text
        assert reconcile.json()["coordination"]["outcome"] == "finalized"
        assert UUID(reconcile.json()["coordination"]["final_document_id"]) == (
            final_document_id
        )
        assert closed_gate._snapshot(
            scenario.environment.engine, scenario.approval_run_id, scenario.ticket_id
        ) == snapshot
        with Session(scenario.environment.engine) as session:
            assert {
                "proposals": closed_gate.canonical_hash(
                    [
                        dict(row)
                        for row in session.execute(
                            select(rfp_approval_proposal_revisions).where(
                                rfp_approval_proposal_revisions.c.approval_run_id
                                == scenario.approval_run_id
                            )
                        ).mappings().all()
                    ]
                ),
                "documents": closed_gate.canonical_hash(
                    [
                        dict(row)
                        for row in session.execute(
                            select(rfp_final_documents).where(
                                rfp_final_documents.c.approval_run_id
                                == scenario.approval_run_id
                            )
                        ).mappings().all()
                    ]
                ),
            } == closure_snapshot
    finally:
        independent.dispose()


def test_documented_monthly_volume_rejects_contradictory_authorized_input(
    auto_closure_environment: AutomaticClosureEnvironment,
) -> None:
    scenario = _start_delegated_approval_run(
        auto_closure_environment, documented_volume=Decimal("20.0000")
    )
    _request, _recorded, _body, corrected = closed_gate._authorized_correction(
        scenario.reverse
    )
    assert corrected["status"] == "pending_approval"

    monthly_url = (
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/"
        "business-inputs"
    )
    contradictory = scenario.environment.client.post(
        monthly_url,
        headers=scenario.environment.headers("manager"),
        json=_monthly_volume_request("21.0000"),
    )
    assert contradictory.status_code == 200, contradictory.text

    assessed = scenario.environment.client.post(
        f"/rfps/{scenario.ticket_id}/approval/{scenario.approval_run_id}/assess-and-start",
        headers=scenario.environment.headers("manager"),
    )
    assert assessed.status_code == 409, assessed.text
    assert assessed.json()["detail"] == "monthly_storage_volume_source_contradiction"

    with Session(scenario.environment.engine) as session:
        current = session.execute(
            select(rfp_approval_department_revisions).where(
                rfp_approval_department_revisions.c.approval_run_id
                == scenario.approval_run_id,
                rfp_approval_department_revisions.c.is_current.is_(True),
            )
        ).mappings().all()
        assert {row["department_id"]: row["status"] for row in current} == {
            "warehouse": "pending_assessment",
            "reverse": "pending_approval",
        }
        assert session.execute(
            select(func.count()).select_from(rfp_approval_proposal_revisions).where(
                rfp_approval_proposal_revisions.c.approval_run_id
                == scenario.approval_run_id
            )
        ).scalar_one() == 0
        assert session.execute(
            select(func.count()).select_from(rfp_final_documents).where(
                rfp_final_documents.c.approval_run_id == scenario.approval_run_id
            )
        ).scalar_one() == 0


def test_automation_delegation_contract_requires_run_manager() -> None:
    principal = AuthenticatedPrincipal(
        issuer="urn:trackflow:qa:local",
        subject="manager-qa",
        trackflow_user_id="manager-qa",
        auth_source="local",
        general_role="user",
        synthetic=True,
    )
    reference = InitialRevisionReference(
        department=DepartmentId.warehouse,
        source_draft_id=uuid4(),
        source_draft_hash="a" * 64,
        source_evaluation_id=uuid4(),
        source_evaluation_hash="b" * 64,
        source_part2_iteration=1,
        terminal_status="passed",
    )
    values = {
        "approval_run_id": uuid4(),
        "ticket_id": uuid4(),
        "response_run_id": uuid4(),
        "response_handoff_hash": "c" * 64,
        "active_departments": [DepartmentId.warehouse],
        "initial_revisions": [reference],
        "started_by": RunInitiator(
            principal=principal,
            authorization_id=uuid4(),
            permission="manage_approval_run",
        ),
        "automate_proposal_materialization": True,
        "started_at": closed_gate.NOW.astimezone(timezone.utc),
    }
    delegated = ApprovalRunStart(**values)
    assert delegated.automate_proposal_materialization is True
    invalid = {
        **values,
        "started_by": RunInitiator(
            principal=principal,
            authorization_id=uuid4(),
            permission="start_approval_run",
        ),
    }
    with pytest.raises(ValidationError, match="requires run management"):
        ApprovalRunStart(**invalid)


def test_auto_finalization_history_preserves_real_reverse_feedback() -> None:
    response_start = RFPResponseStart(
        ticket_id=uuid4(),
        run_id=uuid4(),
        source_handoff_hash="d" * 64,
        summary_for_sales="Automatic closure QA history.",
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
    sources, evaluations, _previews = _build_real_department_history(response_start)
    assert sources[DepartmentId.warehouse].pending_commercial_items == []
    assert evaluations[DepartmentId.warehouse].overall_pass is True
    assert evaluations[DepartmentId.warehouse].terminal_status == "passed"
    assert {
        item.code
        for item in sources[DepartmentId.reverse].pending_commercial_items
    } == {"RETURN_PROCESSING_RATE_REQUIRED"}
    assert evaluations[DepartmentId.reverse].overall_pass is False
    assert evaluations[DepartmentId.reverse].terminal_status == "needs_human_input"


def test_documented_volume_fixture_uses_structured_pdf_source_reference() -> None:
    reference = SourceReference(line_start=1, line_end=2)
    persisted = reference.model_dump(mode="json")
    assert SourceReference.model_validate(persisted) == reference
    assert persisted == {"line_start": 1, "line_end": 2}


def test_auto_finalization_reuses_seeded_miguel_without_principal_insert() -> None:
    helper_source = inspect.getsource(_reuse_seeded_miguel_identity)
    fixture_source = inspect.getsource(_persist_real_part2_history)
    assert 'responsible_code == "miguel_torres"' in helper_source
    assert "trackflow_user_id" in helper_source
    assert "binding_id" in helper_source
    assert "insert(rfp_principals)" not in helper_source
    assert ".values(synthetic=True)" in helper_source
    assert 'assert row["synthetic"] is True' in helper_source
    assert "_reuse_seeded_miguel_identity" in fixture_source
    assert 'responsible="miguel_torres"' not in fixture_source
