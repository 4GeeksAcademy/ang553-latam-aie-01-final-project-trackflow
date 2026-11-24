"""Offline contract tests for the Part 3 PostgreSQL schema.

These tests inspect SQL and SQLAlchemy metadata only.  They deliberately do
not connect to PostgreSQL and therefore do not claim runtime DDL evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index

from services.rfp_approval.models import rfp_metadata_registry
from services.rfp_intake.models import rfp_tickets
from tests.test_rfp_approval_postgres_migration import _psycopg2_dsn


ROOT = Path(__file__).resolve().parents[1]
DDL = (ROOT / "infra" / "sql" / "rfp_approval.sql").read_text(encoding="utf-8")
EXTENSION_DDL = (
    ROOT / "infra" / "sql" / "rfp_approval_extension.sql"
).read_text(encoding="utf-8")

HISTORICAL_TABLES = {
    "rfp_tickets", "rfp_metadata", "rfp_department_sections", "rfp_handoffs",
    "rfp_jobs", "rfp_outbox", "rfp_response_runs", "rfp_response_sections",
    "rfp_response_drafts", "rfp_response_previews", "rfp_response_evaluator_results",
    "rfp_response_evaluations", "rfp_response_execution_failures",
    "rfp_response_llm_invocations", "rfp_response_handoffs",
}
APPROVAL_TABLES = {
    "rfp_principals", "rfp_identity_bindings", "rfp_authorizations",
    "rfp_approval_runs", "rfp_approval_department_revisions",
    "rfp_approval_proposal_revisions", "rfp_human_decisions",
    "rfp_authorized_business_inputs", "rfp_conflict_assessments",
    "rfp_arbitration_conflicts", "rfp_arbitration_resolutions",
    "rfp_arbitration_resolution_evidence", "rfp_final_documents",
    "rfp_workflow_events",
}
EXTENSION_TABLES = {
    "rfp_approval_corrected_drafts",
    "rfp_approval_validation_artifacts",
    "rfp_approval_validation_bundles",
}


def _fk(table_name: str, constraint_name: str) -> ForeignKeyConstraint:
    table = rfp_metadata_registry.tables[table_name]
    return next(
        item for item in table.constraints
        if isinstance(item, ForeignKeyConstraint) and item.name == constraint_name
    )

def _fk_signature(table_name: str, constraint_name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    constraint = _fk(table_name, constraint_name)
    local = tuple(element.parent.name for element in constraint.elements)
    remote = tuple(element.target_fullname for element in constraint.elements)
    return local, remote


def _check_sql(table_name: str) -> str:
    return " ".join(
        str(item.sqltext)
        for item in rfp_metadata_registry.tables[table_name].constraints
        if isinstance(item, CheckConstraint)
    )


def test_run_automation_delegation_is_explicit_and_role_bound() -> None:
    table = rfp_metadata_registry.tables["rfp_approval_runs"]
    assert "started_by_general_role" in table.c
    assert "automate_proposal_materialization" in table.c
    checks = _check_sql("rfp_approval_runs")
    assert "NOT automate_proposal_materialization" in checks
    assert "started_by_general_role IS NOT NULL" in checks
    normalized = " ".join(DDL.split())
    assert "automate_proposal_materialization boolean NOT NULL DEFAULT false" in normalized
    assert "CONSTRAINT ck_rfp_approval_automation_delegation" in normalized


def test_metadata_preserves_history_and_adds_exact_business_inventory() -> None:
    assert set(rfp_metadata_registry.tables) == (
        HISTORICAL_TABLES | APPROVAL_TABLES | EXTENSION_TABLES
    )
    assert len(APPROVAL_TABLES) == 14  # The brief says 13 but enumerates fourteen.


def test_ddl_and_orm_declare_the_same_new_tables() -> None:
    sql_tables = set(re.findall(r"(?im)^CREATE TABLE IF NOT EXISTS ([a-z0-9_]+)", DDL))
    assert sql_tables == APPROVAL_TABLES
    for table_name in APPROVAL_TABLES:
        assert f'CREATE TABLE IF NOT EXISTS {table_name} (' in DDL
    extension_tables = set(
        re.findall(
            r"(?im)^CREATE TABLE IF NOT EXISTS ([a-z0-9_]+)", EXTENSION_DDL
        )
    )
    assert extension_tables == EXTENSION_TABLES


def test_ticket_states_keep_history_and_add_official_part3_states() -> None:
    status_check = next(
        item for item in rfp_tickets.constraints
        if isinstance(item, CheckConstraint) and item.name == "ck_rfp_ticket_status"
    )
    expression = str(status_check.sqltext)
    expected = {
        "analizando", "descartado", "analisis_completo", "generando_borrador",
        "en_evaluación", "needs_human_review", "esperando_aprobación", "terminado",
    }
    assert all(f"'{state}'" in expression for state in expected)
    assert all(f"'{state}'" in DDL for state in expected)
    assert "rfp-intake-v1" not in DDL
    assert "rfp-response-v1'" in DDL


def test_migration_is_forward_only_and_reentrant_by_definition() -> None:
    normalized = DDL.upper()
    assert "\nBEGIN;" in DDL[:500] and DDL.rstrip().endswith("COMMIT;")
    assert "PG_ADVISORY_XACT_LOCK" in normalized
    assert "DROP TABLE" not in normalized
    assert "TRUNCATE" not in normalized
    assert "DELETE FROM" not in normalized
    assert normalized.count("CREATE TABLE IF NOT EXISTS") == len(APPROVAL_TABLES)
    assert "DROP CONSTRAINT IF EXISTS CK_RFP_TICKET_STATUS" in normalized
    assert "DROP CONSTRAINT IF EXISTS FK_RFP_DEPARTMENT_APPLIED_DECISION" in normalized
    assert "DROP CONSTRAINT IF EXISTS FK_RFP_DEPARTMENT_REVISION_SUCCESSOR" in normalized
    assert "DROP CONSTRAINT IF EXISTS FK_RFP_PROPOSAL_SUCCESSOR" in normalized
    assert "DROP CONSTRAINT IF EXISTS UQ_RFP_RESOLUTION_CONFLICT_HASH" in normalized
    assert "DROP TRIGGER IF EXISTS TRG_RFP_ARBITRATION_RESOLUTIONS_APPEND_ONLY" in normalized
    assert "DROP TRIGGER IF EXISTS TRG_RFP_WORKFLOW_EVENTS_APPEND_ONLY" in normalized


def test_no_langgraph_checkpointer_tables_are_declared() -> None:
    lowered = DDL.lower()
    forbidden = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
    assert not any(re.search(rf"create table(?: if not exists)?\s+{name}\b", lowered) for name in forbidden)
    assert not any(name in APPROVAL_TABLES for name in forbidden)


def test_handoff_and_approval_run_are_bound_to_ticket_run_and_hash() -> None:
    local, remote = _fk_signature("rfp_approval_runs", "fk_rfp_approval_source_handoff")
    assert local == ("response_run_id", "ticket_id", "response_handoff_hash")
    assert remote == (
        "rfp_response_handoffs.run_id", "rfp_response_handoffs.ticket_id",
        "rfp_response_handoffs.payload_hash",
    )


def test_department_revision_sources_are_bound_to_response_run_and_scope() -> None:
    draft = _fk_signature("rfp_approval_department_revisions", "fk_rfp_department_revision_source_draft")
    evaluation = _fk_signature(
        "rfp_approval_department_revisions", "fk_rfp_department_revision_source_evaluation"
    )
    assert draft[0] == ("source_part2_draft_id", "response_run_id", "department_id")
    assert draft[1][-1] == "rfp_response_drafts.scope_key"
    assert evaluation[0] == (
        "source_part2_evaluation_id", "response_run_id", "source_part2_draft_id", "department_id"
    )
    assert evaluation[1][-1] == "rfp_response_evaluations.scope_key"


def test_decision_is_bound_to_revision_run_ticket_department_and_actor_grant() -> None:
    revision = _fk_signature("rfp_human_decisions", "fk_rfp_decision_revision")
    authorization = _fk_signature("rfp_human_decisions", "fk_rfp_decision_authorization")
    assert revision[0] == ("revision_id", "approval_run_id", "ticket_id", "department_id")
    assert authorization[0] == (
        "authorization_id", "actor_principal_id", "environment", "authorization_version"
    )


def test_resolution_is_bound_to_same_conflict_and_rechecked_assessment() -> None:
    conflict = _fk_signature("rfp_arbitration_resolutions", "fk_rfp_resolution_conflict")
    post = _fk_signature("rfp_arbitration_resolutions", "fk_rfp_resolution_post_assessment")
    assert conflict[0] == (
        "conflict_id", "approval_run_id", "ticket_id", "trigger_id", "conflict_fingerprint"
    )
    assert post[0] == (
        "post_assessment_id", "approval_run_id", "ticket_id", "trigger_id",
        "post_assessment_outcome",
    )
    assert post[1][-1] == "rfp_conflict_assessments.outcome"
    assert "post_assessment_outcome='no_conflict'" in _check_sql("rfp_arbitration_resolutions")


def test_final_document_revisions_cannot_cross_ticket_or_department() -> None:
    proposal = _fk_signature("rfp_final_documents", "fk_rfp_final_proposal")
    warehouse = _fk_signature("rfp_final_documents", "fk_rfp_final_warehouse")
    assert proposal[0] == ("proposal_revision_id", "approval_run_id", "ticket_id")
    assert warehouse[0] == (
        "warehouse_revision_id", "approval_run_id", "ticket_id", "warehouse_department_id"
    )
    assert warehouse[1][-1] == "rfp_approval_department_revisions.department_id"


def test_hash_json_and_review_state_guards_are_present() -> None:
    assert "evaluated_content_hash=content_hash" in _check_sql("rfp_approval_department_revisions")
    assert "evaluated_content_hash=content_hash" in _check_sql("rfp_approval_proposal_revisions")
    assert "review_revision_no BETWEEN 1 AND 3" in _check_sql("rfp_approval_department_revisions")
    assert "status<>'limit_reached' OR review_revision_no=3" in _check_sql("rfp_approval_department_revisions")
    assert "'blocked_conflict'" in _check_sql("rfp_approval_department_revisions")
    assert "'pending_assessment'" in _check_sql("rfp_approval_department_revisions")
    assert "'blocked_conflict'" in EXTENSION_DDL
    assert (
        "DROP CONSTRAINT IF EXISTS "
        "rfp_approval_department_revisions_status_check"
    ) in EXTENSION_DDL
    assert ") IS TRUE)" in _check_sql("rfp_approval_department_revisions")
    assert "jsonb_typeof(payload)='object'" in _check_sql("rfp_final_documents")
    assert DDL.count("~ '^[a-f0-9]{64}$'") >= 20


def test_supersession_successor_fks_are_deferred_but_current_indexes_remain_unique() -> None:
    department = _fk(
        "rfp_approval_department_revisions", "fk_rfp_department_revision_successor"
    )
    proposal = _fk("rfp_approval_proposal_revisions", "fk_rfp_proposal_successor")
    assert department.deferrable is True and department.initially == "DEFERRED"
    assert proposal.deferrable is True and proposal.initially == "DEFERRED"
    assert "DEFERRABLE INITIALLY DEFERRED" in DDL
    assert "uq_rfp_department_current" in DDL
    assert "uq_rfp_proposal_current" in DDL
    assert "uq_rfp_department_revision_successor" in DDL
    assert "uq_rfp_proposal_successor" in DDL


def test_partial_uniqueness_covers_current_rows_and_nullable_evidence_targets() -> None:
    expected = {
        "uq_rfp_active_authorization_scope", "uq_rfp_approval_active_ticket",
        "uq_rfp_department_current", "uq_rfp_proposal_current",
        "uq_rfp_resolution_evidence_department", "uq_rfp_resolution_evidence_proposal",
        "uq_rfp_resolution_accepted_conflict",
    }
    indexes = {
        item.name: item
        for table in rfp_metadata_registry.tables.values()
        for item in table.indexes
        if isinstance(item, Index)
    }
    assert expected <= indexes.keys()
    assert all(indexes[name].unique for name in expected)
    assert all(indexes[name].dialect_options["postgresql"]["where"] is not None for name in expected)


def test_arbitration_history_is_append_only_idempotent_and_allows_request_then_accept() -> None:
    table = rfp_metadata_registry.tables["rfp_arbitration_resolutions"]
    uniques = {item.name for item in table.constraints if item.name}
    assert "uq_rfp_resolution_conflict_hash" not in uniques
    assert "uq_rfp_resolution_idempotency" in uniques
    assert "uq_rfp_resolution_material" in uniques
    assert "BEFORE UPDATE OR DELETE ON rfp_arbitration_resolutions" in DDL
    assert "uq_rfp_resolution_accepted_conflict" in DDL
    assert "post_assessment_outcome='no_conflict'" in _check_sql(
        "rfp_arbitration_resolutions"
    )


def test_workflow_events_are_append_only_with_only_a_shallow_sql_denylist() -> None:
    assert "BEFORE UPDATE OR DELETE ON rfp_workflow_events" in DDL
    assert "RAISE EXCEPTION 'rfp_workflow_events is append-only'" in DDL
    checks = _check_sql("rfp_workflow_events")
    assert "jsonb_typeof(safe_input_ref)='object'" in checks
    assert "jsonb_typeof(safe_output_ref)='object'" in checks
    for key in ("token", "access_token", "password", "prompt", "rate", "tariff"):
        assert f"'{key}'" in checks
    event_auth = _fk_signature("rfp_workflow_events", "fk_rfp_event_authorization")
    assert event_auth[0] == (
        "authorization_id", "actor_principal_id", "environment", "authorization_version"
    )
    assert "shallow, top-level denylist only" in DDL
    assert "future store must build these references from a strict allowlist" in DDL
    assert "jsonb_path_exists" not in DDL


def test_qa_identities_cannot_bind_to_production_principals() -> None:
    principal_checks = _check_sql("rfp_principals")
    binding_checks = _check_sql("rfp_identity_bindings")
    assert "NOT synthetic OR environment='qa'" in principal_checks
    assert "auth_source<>'local' OR issuer IN ('trackflow-local','urn:trackflow:qa:local')" in binding_checks
    identity_fk = _fk_signature("rfp_identity_bindings", "fk_rfp_identity_principal_environment")
    assert identity_fk[0] == ("principal_id", "environment")


def test_schema_module_contains_no_runtime_provisioning() -> None:
    module = (ROOT / "services" / "rfp_approval" / "models.py").read_text(encoding="utf-8")
    assert "create_all(" not in module
    assert "create_engine(" not in module
    assert "DATABASE_URL" not in module


def test_sqlalchemy_postgres_url_is_normalized_for_psycopg2() -> None:
    assert _psycopg2_dsn(
        "postgresql+psycopg2://user:encoded%40pass@127.0.0.1/db?sslmode=disable"
    ) == "postgresql://user:encoded%40pass@127.0.0.1/db?sslmode=disable"
    assert _psycopg2_dsn("postgresql://user@localhost/db") == (
        "postgresql://user@localhost/db"
    )
