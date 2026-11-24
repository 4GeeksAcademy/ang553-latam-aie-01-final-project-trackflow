"""Real PostgreSQL integrity tests for the S1 evidence extension.

The integration scenarios are skipped unless every Gate 2B.3B.1-S1 guardrail
is present.  Each executed test owns one schema, applies Parts 1-3 followed by
the extension twice, and removes only that confirmed schema.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from psycopg2 import Error as PostgreSQLError
from psycopg2 import connect, sql
from psycopg2.extras import Json

from tests.test_rfp_approval_postgres_migration import (
    H,
    IDS,
    _seed_part1,
    _seed_part2,
    _seed_part3_roots,
)


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_TEST_DATABASE_URL")
DDL_DML_AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_DDL_DML_AUTHORIZED") == "YES"
)
EXPECTED_DATABASE = os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_EXPECTED_DATABASE")
EXPECTED_USER = os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_EXPECTED_USER")
EXPECTED_PORT = os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_EXPECTED_PORT")
EXPECTED_ENVIRONMENT = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_EXTENSION_EXPECTED_ENVIRONMENT"
)
VERIFIED_CONTAINER_ID = os.getenv(
    "TRACKFLOW_RFP_APPROVAL_EXTENSION_VERIFIED_CONTAINER_ID"
)
CONTAINER_VERIFIED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_CONTAINER_VERIFIED") == "YES"
)
SCHEMA_ISOLATION_AUTHORIZED = (
    os.getenv("TRACKFLOW_RFP_APPROVAL_EXTENSION_SCHEMA_ISOLATION_AUTHORIZED")
    == "YES"
)

SCHEMA_PREFIX = "gate2b3b1_s1_test_"
MIGRATIONS = (
    ROOT / "infra/sql/rfp_intake.sql",
    ROOT / "infra/sql/rfp_response_generation.sql",
    ROOT / "infra/sql/rfp_approval.sql",
)
EXTENSION = ROOT / "infra/sql/rfp_approval_extension.sql"
SKIP_REASON = (
    "requires a newly authorized isolated Gate 2B.3B.1-S1 PostgreSQL database"
)


def _guardrail_reason(
    database_url: str | None,
    ddl_dml_authorized: bool,
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
            ddl_dml_authorized,
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
    parsed = urlparse(database_url or "")
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg2"}:
        return "extension PostgreSQL URL has an invalid scheme"
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "extension PostgreSQL host is not loopback-isolated"
    try:
        port = parsed.port
        expected_port_number = int(expected_port or "")
    except ValueError:
        return "extension PostgreSQL port is invalid"
    if (
        parsed.path.lstrip("/") != expected_database
        or parsed.username != expected_user
        or port != expected_port_number
    ):
        return "extension PostgreSQL target does not match expected identity"
    if not expected_database.startswith("trackflow_rfp_approval_extension_test_"):
        return "extension database does not use the disposable prefix"
    if not expected_user.startswith("trackflow_gate2b3b1_s1"):
        return "extension database user does not use the Gate S1 prefix"
    if expected_environment != "qa":
        return "extension PostgreSQL environment is not qa"
    if not re.fullmatch(r"[a-f0-9]{64}", verified_container_id or ""):
        return "extension container identity is not verified"
    return None


def _authorization_reason() -> str | None:
    return _guardrail_reason(
        DATABASE_URL,
        DDL_DML_AUTHORIZED,
        EXPECTED_DATABASE,
        EXPECTED_USER,
        EXPECTED_PORT,
        EXPECTED_ENVIRONMENT,
        VERIFIED_CONTAINER_ID,
        CONTAINER_VERIFIED,
        SCHEMA_ISOLATION_AUTHORIZED,
    )


def _dsn(url: str) -> str:
    parsed = urlparse(url)
    scheme = "postgresql" if parsed.scheme == "postgresql+psycopg2" else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme))


def _quoted_schema(schema_name: str):
    assert re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema_name)
    return sql.Identifier(schema_name)


def _schema_exists(dsn: str, schema_name: str) -> bool:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)",
            (schema_name,),
        )
        return bool(cursor.fetchone()[0])


def _create_schema(dsn: str, schema_name: str) -> None:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(_quoted_schema(schema_name)))


def _drop_schema(dsn: str, schema_name: str) -> None:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout TO '10s'")
        cursor.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(_quoted_schema(schema_name))
        )
    if _schema_exists(dsn, schema_name):
        raise RuntimeError(f"schema cleanup was not confirmed for {schema_name}")


def _set_exclusive_search_path(connection, schema_name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SET search_path TO {}").format(_quoted_schema(schema_name))
        )
        cursor.execute("SELECT current_schema()")
        assert cursor.fetchone()[0] == schema_name


def _execute_file(connection, schema_name: str, path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 0
    _set_exclusive_search_path(connection, schema_name)
    with connection.cursor() as cursor:
        cursor.execute(path.read_text(encoding="utf-8"))
    _set_exclusive_search_path(connection, schema_name)


@contextmanager
def _isolated_database(dsn: str, schema_name: str) -> Iterator[object]:
    schema_confirmed = False
    work = None
    try:
        _create_schema(dsn, schema_name)
        if not _schema_exists(dsn, schema_name):
            raise RuntimeError(f"schema creation was not confirmed for {schema_name}")
        schema_confirmed = True
        work = connect(dsn)
        work.autocommit = True
        _set_exclusive_search_path(work, schema_name)
        _execute_file(work, schema_name, MIGRATIONS[0])
        _seed_part1(work)
        _execute_file(work, schema_name, MIGRATIONS[1])
        _seed_part2(work)
        _execute_file(work, schema_name, MIGRATIONS[2])
        _seed_part3_roots(work)
        _execute_file(work, schema_name, EXTENSION)
        _execute_file(work, schema_name, EXTENSION)
        work.autocommit = False
        yield work
    except Exception as primary_error:
        if work is not None:
            work.close()
            work = None
        if schema_confirmed:
            try:
                _drop_schema(dsn, schema_name)
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    "extension scenario and targeted cleanup both failed",
                    [primary_error, cleanup_error],
                ) from None
        raise
    else:
        if work is not None:
            work.rollback()
            work.close()
        if schema_confirmed:
            _drop_schema(dsn, schema_name)


@pytest.fixture
def database():
    reason = _authorization_reason()
    if reason is not None:
        pytest.skip(reason)
    assert DATABASE_URL is not None
    dsn = _dsn(DATABASE_URL)
    with connect(dsn) as preflight, preflight.cursor() as cursor:
        cursor.execute("SELECT current_database(),current_user")
        database_name, user_name = cursor.fetchone()
        assert database_name == EXPECTED_DATABASE
        assert user_name == EXPECTED_USER
        cursor.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
            "('pg_catalog','information_schema')"
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname LIKE %s",
            (f"{SCHEMA_PREFIX}%",),
        )
        assert cursor.fetchone()[0] == 0
    schema_name = f"{SCHEMA_PREFIX}{uuid4().hex}"
    with _isolated_database(dsn, schema_name) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            assert cursor.fetchone()[0] == schema_name
        yield connection


def _expect_rejection(connection, statement: str, parameters, sqlstate: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT expected_rejection")
        try:
            cursor.execute(statement, parameters)
        except PostgreSQLError as error:
            assert error.pgcode == sqlstate
            cursor.execute("ROLLBACK TO SAVEPOINT expected_rejection")
            cursor.execute("RELEASE SAVEPOINT expected_rejection")
        else:
            cursor.execute("ROLLBACK TO SAVEPOINT expected_rejection")
            cursor.execute("RELEASE SAVEPOINT expected_rejection")
            pytest.fail(f"statement unexpectedly succeeded; expected SQLSTATE {sqlstate}")


def _insert_lastmile_source(connection) -> tuple[str, str]:
    draft_id = str(uuid4())
    evaluation_id = str(uuid4())
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_response_drafts("
            "draft_id,run_id,scope_key,iteration_count,contract_version,payload,payload_hash"
            ") VALUES (%s,%s,'lastmile',1,'rfp-department-draft-v1','{}'::jsonb,%s)",
            (draft_id, IDS["response_1"], H["3"]),
        )
        cursor.execute(
            "INSERT INTO rfp_response_evaluations("
            "evaluation_id,run_id,draft_id,scope_key,contract_version,overall_pass,"
            "terminal_status,payload,payload_hash) VALUES ("
            "%s,%s,%s,'lastmile','section-evaluation-v1',true,'ready_for_part3',"
            "'{}'::jsonb,%s)",
            (evaluation_id, IDS["response_1"], draft_id, H["4"]),
        )
    return draft_id, evaluation_id


def _department_insert_sql() -> str:
    return (
        "INSERT INTO rfp_approval_department_revisions("
        "revision_id,approval_run_id,ticket_id,response_run_id,department_id,"
        "review_revision_no,source_part2_draft_id,source_part2_draft_hash,"
        "source_part2_evaluation_id,source_part2_evaluation_hash,source_part2_iteration,"
        "content_payload,content_hash,validation_payload,validation_hash,"
        "evaluated_content_hash,status,created_by_principal_id,created_at) VALUES ("
        "%s,%s,%s,%s,'lastmile',1,%s,%s,%s,%s,1,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,"
        "clock_timestamp())"
    )


def _department_parameters(
    draft_id: str, evaluation_id: str, status: str, validation: dict
) -> tuple:
    return (
        str(uuid4()), IDS["approval_1"], IDS["ticket_1"], IDS["response_1"],
        draft_id, H["3"], evaluation_id, H["4"],
        Json({"pending_commercial_items": []}), H["5"], Json(validation), H["6"],
        H["5"], status, IDS["principal"],
    )


def _valid_conflict_validation() -> dict:
    return {
        "eligible_for_approval": False,
        "mandatory_compliance_passed": True,
        "blocks_commercial_completeness": False,
        "open_conflict_ids": [str(uuid4())],
        "blocking_rule_ids": [],
        "unresolved_input_codes": [],
    }


def _insert_draft(connection, draft_id: str, content_hash: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_approval_corrected_drafts("
            "draft_artifact_id,approval_run_id,ticket_id,scope_key,contract_version,"
            "content_payload,content_hash,payload_hash,provenance,provenance_hash,"
            "producer_node,execution_id,produced_at) VALUES ("
            "%s,%s,%s,'warehouse','rfp-approval-corrected-draft-v1','{}'::jsonb,"
            "%s,%s,'{}'::jsonb,%s,'gate-s1-test',%s,clock_timestamp())",
            (
                draft_id, IDS["approval_1"], IDS["ticket_1"], content_hash,
                H["1"], H["2"], str(uuid4()),
            ),
        )


def _insert_artifact(
    connection, artifact_id: str, artifact_type: str, draft_id: str,
    content_hash: str, payload_hash: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_approval_validation_artifacts("
            "artifact_id,artifact_type,approval_run_id,ticket_id,scope_key,"
            "corrected_draft_id,evaluated_content_hash,contract_version,payload,"
            "payload_hash,provenance,provenance_hash,producer_node,execution_id,produced_at"
            ") VALUES (%s,%s,%s,%s,'warehouse',%s,%s,"
            "'rfp-approval-validation-artifact-v1','{}'::jsonb,%s,'{}'::jsonb,%s,"
            "'gate-s1-test',%s,clock_timestamp())",
            (
                artifact_id, artifact_type, IDS["approval_1"], IDS["ticket_1"],
                draft_id, content_hash, payload_hash, H["2"], str(uuid4()),
            ),
        )


def _bundle_insert_sql() -> str:
    return (
        "INSERT INTO rfp_approval_validation_bundles("
        "validation_bundle_id,contract_version,approval_run_id,ticket_id,scope_key,"
        "corrected_draft_id,content_hash,evaluation_artifact_id,"
        "evaluation_artifact_type,evaluation_contract_version,evaluation_payload_hash,"
        "safe_preview_artifact_id,safe_preview_artifact_type,"
        "safe_preview_contract_version,safe_preview_payload_hash,"
        "revision_validation_hash,mandatory_compliance_passed,"
        "blocks_commercial_completeness,blocking_rule_ids,unresolved_input_codes,"
        "open_conflict_ids,source_departments_approved,eligible,provenance,"
        "provenance_hash,bundle_hash,validated_at) VALUES ("
        "%s,'rfp-approval-validation-bundle-v1',%s,%s,%s,%s,%s,%s,'evaluation',"
        "%s,%s,%s,'safe_preview',%s,%s,%s,true,false,'[]'::jsonb,'[]'::jsonb,"
        "'[]'::jsonb,false,true,'{}'::jsonb,%s,%s,clock_timestamp())"
    )


def _bundle_parameters(
    bundle_id: str, draft_id: str, content_hash: str, evaluation_id: str,
    preview_id: str, validation_hash: str,
) -> list:
    artifact_version = "rfp-approval-validation-artifact-v1"
    return [
        bundle_id, IDS["approval_1"], IDS["ticket_1"], "warehouse", draft_id,
        content_hash, evaluation_id, artifact_version, H["3"], preview_id,
        artifact_version, H["4"], validation_hash, H["2"], H["5"],
    ]


def _insert_evidence_chain(connection, validation_hash: str = H["9"]):
    draft_id, evaluation_id, preview_id, bundle_id = (str(uuid4()) for _ in range(4))
    content_hash = H["3"]
    _insert_draft(connection, draft_id, content_hash)
    _insert_artifact(connection, evaluation_id, "evaluation", draft_id, content_hash, H["3"])
    _insert_artifact(connection, preview_id, "safe_preview", draft_id, content_hash, H["4"])
    with connection.cursor() as cursor:
        cursor.execute(
            _bundle_insert_sql(),
            _bundle_parameters(
                bundle_id, draft_id, content_hash, evaluation_id, preview_id,
                validation_hash,
            ),
        )
    return draft_id, evaluation_id, preview_id, bundle_id


def test_guardrail_requires_every_exact_gate_s1_control() -> None:
    valid_url = (
        "postgresql+psycopg2://trackflow_gate2b3b1_s1:test@127.0.0.1:65473/"
        "trackflow_rfp_approval_extension_test_s1"
    )
    values = (
        valid_url, True, "trackflow_rfp_approval_extension_test_s1",
        "trackflow_gate2b3b1_s1", "65473", "qa", "a" * 64, True, True,
    )
    assert _guardrail_reason(*values) is None
    assert "loopback" in (_guardrail_reason(*((valid_url.replace("127.0.0.1", "db"),) + values[1:])) or "")
    assert _guardrail_reason(*(values[:-1] + (False,))) == SKIP_REASON


def test_double_extension_status_and_incomplete_conflicts(database) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='rfp_approval_department_revisions'::regclass "
            "AND contype='c' AND conname IN ("
            "'ck_rfp_department_revision_status',"
            "'rfp_approval_department_revisions_status_check') "
            "ORDER BY conname"
        )
        status_checks = cursor.fetchall()
    assert len(status_checks) == 1
    assert status_checks[0][0] == "ck_rfp_department_revision_status"
    status_definition = status_checks[0][1]
    for state in (
        "pending_assessment", "blocked_input", "blocked_compliance", "blocked_conflict",
        "pending_approval", "approved", "rejected", "changes_requested",
        "superseded", "limit_reached",
    ):
        assert state in status_definition

    draft_id, evaluation_id = _insert_lastmile_source(database)
    valid = _valid_conflict_validation()
    invalid_payloads = [
        {},
        {key: value for key, value in valid.items() if key != "eligible_for_approval"},
        {key: value for key, value in valid.items() if key != "mandatory_compliance_passed"},
        {key: value for key, value in valid.items() if key != "open_conflict_ids"},
        {key: value for key, value in valid.items() if key != "blocking_rule_ids"},
        {key: value for key, value in valid.items() if key != "unresolved_input_codes"},
    ]
    for payload in invalid_payloads:
        _expect_rejection(
            database, _department_insert_sql(),
            _department_parameters(draft_id, evaluation_id, "blocked_conflict", payload),
            "23514",
        )
    _expect_rejection(
        database, _department_insert_sql(),
        _department_parameters(draft_id, evaluation_id, "invented_status", valid),
        "23514",
    )
    with database.cursor() as cursor:
        cursor.execute(
            _department_insert_sql(),
            _department_parameters(draft_id, evaluation_id, "blocked_conflict", valid),
        )
        cursor.execute(
            "SELECT count(*) FROM rfp_approval_department_revisions "
            "WHERE department_id='lastmile' AND status='blocked_conflict'"
        )
        assert cursor.fetchone()[0] == 1
        for state in (
            "pending_assessment", "blocked_input", "blocked_compliance", "pending_approval", "approved",
            "rejected", "changes_requested",
        ):
            cursor.execute("SAVEPOINT historical_state")
            cursor.execute(
                "UPDATE rfp_approval_department_revisions SET status=%s "
                "WHERE revision_id=%s",
                (state, IDS["department_1a"]),
            )
            cursor.execute("ROLLBACK TO SAVEPOINT historical_state")
            cursor.execute("RELEASE SAVEPOINT historical_state")


def test_bundle_rejects_cross_draft_entity_version_and_hash_references(database) -> None:
    draft_a, draft_b = str(uuid4()), str(uuid4())
    evaluation_a, preview_a, preview_b = str(uuid4()), str(uuid4()), str(uuid4())
    content_hash = H["3"]
    _insert_draft(database, draft_a, content_hash)
    _insert_draft(database, draft_b, content_hash)
    _insert_artifact(database, evaluation_a, "evaluation", draft_a, content_hash, H["3"])
    _insert_artifact(database, preview_a, "safe_preview", draft_a, content_hash, H["4"])
    _insert_artifact(database, preview_b, "safe_preview", draft_b, content_hash, H["4"])
    baseline = _bundle_parameters(
        str(uuid4()), draft_a, content_hash, evaluation_a, preview_a, H["9"]
    )
    mutations = {
        "preview_from_other_draft": {9: preview_b},
        "other_run": {1: IDS["approval_2"]},
        "other_ticket": {2: IDS["ticket_2"]},
        "other_scope": {3: "reverse"},
        "wrong_evaluation_version": {7: "wrong-contract-v1"},
        "wrong_preview_payload_hash": {11: H["8"]},
    }
    for changes in mutations.values():
        parameters = list(baseline)
        parameters[0] = str(uuid4())
        for index, value in changes.items():
            parameters[index] = value
        _expect_rejection(database, _bundle_insert_sql(), parameters, "23503")
    with database.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM rfp_approval_validation_bundles")
        assert cursor.fetchone()[0] == 0


def test_append_only_evidence_and_business_inputs(database) -> None:
    _draft, evaluation, _preview, bundle = _insert_evidence_chain(database)
    input_id = str(uuid4())
    with database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_authorized_business_inputs("
            "input_id,approval_run_id,ticket_id,input_kind,department_scope,payload,"
            "provenance,payload_hash,provenance_hash,evidence_hash,idempotency_key,"
            "actor_principal_id,actor_binding_id,authorization_id,authorization_version,"
            "environment,recorded_at) VALUES ("
            "%s,%s,%s,'warehouse_capacity','warehouse','{}'::jsonb,'{}'::jsonb,"
            "%s,%s,%s,%s,%s,%s,%s,1,'qa',clock_timestamp())",
            (
                input_id, IDS["approval_1"], IDS["ticket_1"], H["1"], H["2"],
                H["3"], str(uuid4()), IDS["principal"], IDS["binding"],
                IDS["start_auth"],
            ),
        )
    targets = (
        ("rfp_authorized_business_inputs", "input_id", input_id),
        ("rfp_approval_corrected_drafts", "draft_artifact_id", _draft),
        ("rfp_approval_validation_artifacts", "artifact_id", evaluation),
        ("rfp_approval_validation_bundles", "validation_bundle_id", bundle),
    )
    for table, key, value in targets:
        _expect_rejection(
            database, f"UPDATE {table} SET created_at=clock_timestamp() WHERE {key}=%s",
            (value,), "P0001",
        )
        _expect_rejection(
            database, f"DELETE FROM {table} WHERE {key}=%s", (value,), "P0001"
        )
        with database.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {table} WHERE {key}=%s", (value,))
            assert cursor.fetchone()[0] == 1


def test_linked_revision_is_immutable_but_operational_and_historical_updates_work(
    database,
) -> None:
    _draft, _evaluation, _preview, bundle = _insert_evidence_chain(database)
    revision_id = IDS["department_1a"]
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE rfp_approval_department_revisions SET validation_bundle_id=%s "
            "WHERE revision_id=%s",
            (bundle, revision_id),
        )
    _expect_rejection(
        database,
        "UPDATE rfp_approval_department_revisions SET validation_bundle_id=NULL "
        "WHERE revision_id=%s",
        (revision_id,), "P0001",
    )
    _expect_rejection(
        database,
        "UPDATE rfp_approval_department_revisions SET validation_bundle_id=%s "
        "WHERE revision_id=%s",
        (str(uuid4()), revision_id), "P0001",
    )
    for assignment, value in (
        ("content_payload=%s::jsonb", Json({"changed": True})),
        ("content_hash=%s", H["8"]),
        ("validation_payload=%s::jsonb", Json({"changed": True})),
        ("validation_hash=%s", H["8"]),
    ):
        _expect_rejection(
            database,
            f"UPDATE rfp_approval_department_revisions SET {assignment} "
            "WHERE revision_id=%s",
            (value, revision_id), "P0001",
        )
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE rfp_approval_department_revisions SET status='approved',"
            "state_version=state_version+1 WHERE revision_id=%s",
            (revision_id,),
        )
        cursor.execute(
            "SELECT status,state_version,validation_bundle_id FROM "
            "rfp_approval_department_revisions WHERE revision_id=%s",
            (revision_id,),
        )
        assert cursor.fetchone() == ("approved", 1, bundle)
        cursor.execute(
            "UPDATE rfp_approval_department_revisions SET status='rejected',"
            "state_version=state_version+1 WHERE revision_id=%s",
            (IDS["department_2a"],),
        )
        cursor.execute(
            "SELECT validation_bundle_id,state_version FROM "
            "rfp_approval_department_revisions WHERE revision_id=%s",
            (IDS["department_2a"],),
        )
        assert cursor.fetchone() == (None, 1)


def test_constraint_violation_rolls_back_without_partial_rows(database) -> None:
    draft_id = str(uuid4())
    artifact_id = str(uuid4())
    try:
        _insert_draft(database, draft_id, H["3"])
        _insert_artifact(
            database, artifact_id, "evaluation", str(uuid4()), H["3"], H["3"]
        )
        pytest.fail("cross-draft artifact unexpectedly succeeded")
    except PostgreSQLError as error:
        assert error.pgcode == "23503"
        database.rollback()
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM rfp_approval_corrected_drafts "
            "WHERE draft_artifact_id=%s",
            (draft_id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM rfp_approval_validation_artifacts "
            "WHERE artifact_id=%s",
            (artifact_id,),
        )
        assert cursor.fetchone()[0] == 0
