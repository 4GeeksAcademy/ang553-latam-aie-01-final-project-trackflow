"""Offline checks for the additive Parte 3 evidence extension.

These tests inspect DDL and SQLAlchemy metadata.  They do not claim that
PostgreSQL has enforced a foreign key, trigger, or transaction.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from services.rfp_approval.models import rfp_metadata_registry


ROOT = Path(__file__).resolve().parents[1]
BASE_DDL = (ROOT / "infra" / "sql" / "rfp_approval.sql").read_text(
    encoding="utf-8"
)
DDL = (ROOT / "infra" / "sql" / "rfp_approval_extension.sql").read_text(
    encoding="utf-8"
)
EXTENSION_TABLES = {
    "rfp_approval_corrected_drafts",
    "rfp_approval_validation_artifacts",
    "rfp_approval_validation_bundles",
}


def _fk(table_name: str, name: str) -> ForeignKeyConstraint:
    return next(
        item
        for item in rfp_metadata_registry.tables[table_name].constraints
        if isinstance(item, ForeignKeyConstraint) and item.name == name
    )


def _signature(
    table_name: str, name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    constraint = _fk(table_name, name)
    return (
        tuple(element.parent.name for element in constraint.elements),
        tuple(element.target_fullname for element in constraint.elements),
    )


def _checks(table_name: str) -> str:
    return " ".join(
        str(item.sqltext)
        for item in rfp_metadata_registry.tables[table_name].constraints
        if isinstance(item, CheckConstraint)
    )


def _function_body(name: str) -> str:
    match = re.search(
        rf"CREATE OR REPLACE FUNCTION {name}\(\).*?AS \$\$(.*?)\$\$;",
        DDL,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_extension_is_separate_additive_and_reentrant_by_definition() -> None:
    assert DDL.startswith("-- Forward-only")
    assert "\nBEGIN;" in DDL[:500]
    assert DDL.rstrip().endswith("COMMIT;")
    assert "trackflow:rfp-approval-extension-v1" in DDL
    assert "DROP TABLE" not in DDL.upper()
    assert "TRUNCATE" not in DDL.upper()
    assert "DELETE FROM" not in DDL.upper()
    assert "ADD COLUMN IF NOT EXISTS validation_bundle_id" in DDL
    assert "DROP CONSTRAINT IF EXISTS" in DDL
    assert "DROP TRIGGER IF EXISTS" in DDL
    for table_name in EXTENSION_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table_name} (" in DDL
        assert table_name not in BASE_DDL


def test_status_check_replaces_the_exact_anonymous_base_constraint() -> None:
    legacy_states = (
        "'pending_assessment','blocked_input','blocked_compliance',"
        "'pending_approval','approved','rejected',"
    )
    assert legacy_states in BASE_DDL
    assert "CONSTRAINT ck_rfp_department_revision_status" not in BASE_DDL
    assert (
        "DROP CONSTRAINT IF EXISTS "
        "rfp_approval_department_revisions_status_check"
    ) in DDL
    assert DDL.count(
        "ADD CONSTRAINT ck_rfp_department_revision_status CHECK(status IN ("
    ) == 1
    status_checks = [
        item
        for item in rfp_metadata_registry.tables[
            "rfp_approval_department_revisions"
        ].constraints
        if isinstance(item, CheckConstraint)
        and item.name == "ck_rfp_department_revision_status"
    ]
    assert len(status_checks) == 1
    status_sql = str(status_checks[0].sqltext)
    for state in (
        "pending_assessment", "blocked_input", "blocked_compliance", "blocked_conflict",
        "pending_approval", "approved", "rejected", "changes_requested",
        "superseded", "limit_reached",
    ):
        assert f"'{state}'" in status_sql
    assert "invented_status" not in status_sql


def test_extension_table_inventory_matches_metadata() -> None:
    sql_tables = set(
        re.findall(r"(?im)^CREATE TABLE IF NOT EXISTS ([a-z0-9_]+)", DDL)
    )
    assert sql_tables == EXTENSION_TABLES
    assert EXTENSION_TABLES <= set(rfp_metadata_registry.tables)
    assert set(
        rfp_metadata_registry.tables["rfp_approval_corrected_drafts"].c.keys()
    ) == {
        "draft_artifact_id", "approval_run_id", "ticket_id", "scope_key",
        "contract_version", "content_payload", "content_hash", "payload_hash",
        "provenance", "provenance_hash", "producer_node", "execution_id",
        "produced_at", "created_at",
    }
    assert {
        "evaluation_artifact_id", "safe_preview_artifact_id", "content_hash",
        "contract_version",
        "evaluation_contract_version", "evaluation_payload_hash",
        "safe_preview_contract_version", "safe_preview_payload_hash",
        "revision_validation_hash", "source_departments_approved", "eligible",
        "bundle_hash",
    } <= set(
        rfp_metadata_registry.tables["rfp_approval_validation_bundles"].c.keys()
    )


def test_validation_artifact_is_bound_to_exact_corrected_draft() -> None:
    local, remote = _signature(
        "rfp_approval_validation_artifacts",
        "fk_rfp_validation_artifact_draft",
    )
    assert local == (
        "corrected_draft_id",
        "approval_run_id",
        "ticket_id",
        "scope_key",
        "evaluated_content_hash",
    )
    assert remote == (
        "rfp_approval_corrected_drafts.draft_artifact_id",
        "rfp_approval_corrected_drafts.approval_run_id",
        "rfp_approval_corrected_drafts.ticket_id",
        "rfp_approval_corrected_drafts.scope_key",
        "rfp_approval_corrected_drafts.content_hash",
    )


def test_bundle_fks_bind_evaluation_preview_scope_and_content() -> None:
    evaluation = _signature(
        "rfp_approval_validation_bundles",
        "fk_rfp_validation_bundle_evaluation",
    )
    preview = _signature(
        "rfp_approval_validation_bundles",
        "fk_rfp_validation_bundle_preview",
    )
    assert evaluation[0] == (
        "evaluation_artifact_id", "approval_run_id", "ticket_id", "scope_key",
        "corrected_draft_id", "content_hash", "evaluation_artifact_type",
        "evaluation_contract_version", "evaluation_payload_hash",
    )
    assert preview[0] == (
        "safe_preview_artifact_id", "approval_run_id", "ticket_id", "scope_key",
        "corrected_draft_id", "content_hash", "safe_preview_artifact_type",
        "safe_preview_contract_version", "safe_preview_payload_hash",
    )
    assert evaluation[1][-5:] == (
        "rfp_approval_validation_artifacts.corrected_draft_id",
        "rfp_approval_validation_artifacts.evaluated_content_hash",
        "rfp_approval_validation_artifacts.artifact_type",
        "rfp_approval_validation_artifacts.contract_version",
        "rfp_approval_validation_artifacts.payload_hash",
    )
    assert preview[1][-2:] == (
        "rfp_approval_validation_artifacts.contract_version",
        "rfp_approval_validation_artifacts.payload_hash",
    )
    checks = _checks("rfp_approval_validation_bundles")
    assert "safe_preview_artifact_type='safe_preview'" in checks
    assert "evaluation_artifact_type='global_evaluation'" in checks
    assert "length(btrim(evaluation_contract_version))>0" in checks
    assert "contract_version='rfp-approval-validation-bundle-v1'" in checks
    artifact_checks = _checks("rfp_approval_validation_artifacts")
    assert "length(btrim(contract_version))>0" in artifact_checks


def test_revisions_reference_bundle_with_run_ticket_scope_and_hash() -> None:
    department = _signature(
        "rfp_approval_department_revisions",
        "fk_rfp_department_revision_validation_bundle",
    )
    proposal = _signature(
        "rfp_approval_proposal_revisions",
        "fk_rfp_proposal_revision_validation_bundle",
    )
    assert department[0] == (
        "validation_bundle_id", "approval_run_id", "ticket_id", "department_id",
        "content_hash", "validation_hash",
    )
    assert proposal[0] == (
        "validation_bundle_id", "approval_run_id", "ticket_id", "source_scope_key",
        "content_hash", "validation_hash",
    )
    assert department[1][-1] == (
        "rfp_approval_validation_bundles.revision_validation_hash"
    )
    assert proposal[1][-3] == "rfp_approval_validation_bundles.scope_key"


def test_new_inserts_require_bundles_without_rewriting_historical_rows() -> None:
    assert "BEFORE INSERT ON rfp_approval_department_revisions" in DDL
    assert "NEW.review_revision_no>1 AND NEW.validation_bundle_id IS NULL" in DDL
    assert "BEFORE INSERT ON rfp_approval_proposal_revisions" in DDL
    assert "IF NEW.validation_bundle_id IS NULL" in DDL
    assert "UPDATE rfp_approval_department_revisions" not in DDL
    assert "UPDATE rfp_approval_proposal_revisions" not in DDL


def test_evidence_and_business_inputs_declare_append_only_triggers() -> None:
    tables = (
        "rfp_authorized_business_inputs",
        "rfp_approval_corrected_drafts",
        "rfp_approval_validation_artifacts",
        "rfp_approval_validation_bundles",
    )
    for table_name in tables:
        assert f"BEFORE UPDATE OR DELETE ON {table_name}" in DDL
    assert "rfp_authorized_business_inputs is append-only" in DDL
    assert "supersedes_input_id" not in DDL


def test_blocked_conflict_and_bundle_eligibility_are_declared_consistently() -> None:
    department_checks = _checks("rfp_approval_department_revisions")
    bundle_checks = _checks("rfp_approval_validation_bundles")
    assert "'blocked_conflict'" in department_checks
    assert "'blocked_conflict'" in DDL
    assert "status<>'blocked_conflict'" in department_checks
    assert ") IS TRUE)" in department_checks
    assert "jsonb_typeof(validation_payload->'eligible_for_approval')='boolean'" in (
        department_checks
    )
    assert "jsonb_array_length(validation_payload->'open_conflict_ids')>0" in (
        department_checks
    )
    assert "jsonb_typeof(validation_payload->'blocking_rule_ids')='array'" in (
        department_checks
    )
    assert "jsonb_typeof(validation_payload->'unresolved_input_codes')='array'" in (
        department_checks
    )
    assert "jsonb_array_length(content_payload->'pending_commercial_items')=0" in (
        department_checks
    )
    assert "jsonb_array_length(open_conflict_ids)=0" in bundle_checks
    assert "scope_key<>'proposal' OR source_departments_approved" in bundle_checks
    for code in (
        "MONTHLY_STORAGE_VOLUME_REQUIRED",
        "LAST_MILE_RATE_REQUIRED",
        "RETURN_PROCESSING_RATE_REQUIRED",
        "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
    ):
        assert code in bundle_checks
        assert code in DDL
    assert "blocks_commercial_completeness=" in bundle_checks
    assert "jsonb_array_length(unresolved_input_codes)>0" in bundle_checks
    assert "unresolved_input_codes <@" in bundle_checks


def test_indicative_bundle_forward_migration_is_transactional_and_scoped() -> None:
    migration = (
        ROOT / "infra" / "sql"
        / "rfp_approval_indicative_validation_bundle.sql"
    ).read_text(encoding="utf-8")
    assert migration.startswith("BEGIN;")
    assert migration.rstrip().endswith("COMMIT;")
    assert migration.count("DROP CONSTRAINT") == 1
    assert migration.count("ADD CONSTRAINT") == 1
    assert "ck_rfp_validation_bundle_eligibility" in migration
    assert "trackflow:indicative-validation-bundle-v1" in migration
    assert "UPDATE " not in migration
    assert "INSERT " not in migration
    assert "DELETE " not in migration
    assert "ALTER TABLE rfp_approval_validation_bundles" in migration


def test_indicative_allowlist_is_exact_across_ddl_metadata_and_migration() -> None:
    expected = {
        "MONTHLY_STORAGE_VOLUME_REQUIRED",
        "LAST_MILE_RATE_REQUIRED",
        "RETURN_PROCESSING_RATE_REQUIRED",
        "VOLUME_DISCOUNT_APPROVAL_REQUIRED",
    }
    metadata_check = next(
        str(item.sqltext)
        for item in rfp_metadata_registry.tables[
            "rfp_approval_validation_bundles"
        ].constraints
        if isinstance(item, CheckConstraint)
        and item.name == "ck_rfp_validation_bundle_eligibility"
    )
    migration = (
        ROOT / "infra" / "sql"
        / "rfp_approval_indicative_validation_bundle.sql"
    ).read_text(encoding="utf-8")

    def codes(source: str) -> set[str]:
        return set(re.findall(r"[\"']([A-Z_]+_REQUIRED)[\"']", source))

    assert codes(DDL) == expected
    assert codes(metadata_check) == expected
    assert codes(migration) == expected


def test_hash_columns_do_not_claim_to_validate_json_cryptographically() -> None:
    assert DDL.count("~ '^[a-f0-9]{64}$'") >= 8
    assert "digest(" not in DDL.lower()
    assert "payload_hash=sha" not in DDL.lower()


def test_revision_update_guards_lock_evidence_but_allow_operational_fields() -> None:
    department = _function_body("protect_rfp_department_revision_evidence")
    proposal = _function_body("protect_rfp_proposal_revision_evidence")
    for field in (
        "content_payload", "content_hash", "validation_payload", "validation_hash",
        "evaluated_content_hash",
    ):
        assert f"NEW.{field} IS DISTINCT FROM OLD.{field}" in department
        assert f"NEW.{field} IS DISTINCT FROM OLD.{field}" in proposal
    assert "OLD.validation_bundle_id IS NOT NULL" in department
    assert "OLD.validation_bundle_id IS NOT NULL" in proposal
    for allowed in (
        "status", "is_current", "state_version", "applied_decision_id",
        "superseded_by_revision_id",
    ):
        assert f"NEW.{allowed} IS DISTINCT FROM OLD.{allowed}" not in department
    for allowed in ("is_current", "state_version", "superseded_by_revision_id"):
        assert f"NEW.{allowed} IS DISTINCT FROM OLD.{allowed}" not in proposal
    assert "BEFORE UPDATE ON rfp_approval_department_revisions" in DDL
    assert "BEFORE UPDATE ON rfp_approval_proposal_revisions" in DDL
