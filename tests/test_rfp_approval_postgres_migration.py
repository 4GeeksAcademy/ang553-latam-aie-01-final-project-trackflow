"""Authorized-only PostgreSQL migration and integrity tests.

Each test receives a fresh schema, seeds Parts 1 and 2, and applies Part 3
twice. No connection is made without all disposable-database guardrails.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TRACKFLOW_RFP_APPROVAL_TEST_DATABASE_URL")
DDL_AUTHORIZED = os.getenv("TRACKFLOW_RFP_APPROVAL_DDL_AUTHORIZED") == "YES"
EXPECTED_DATABASE = "trackflow_rfp_approval_test_2b1"
EXPECTED_USER = "trackflow_gate2b1"
EXPECTED_PORT = 65470
H = {str(index): str(index) * 64 for index in range(1, 10)}
IDS = {
    "ticket_1": "00000000-0000-0000-0000-000000000001",
    "ticket_2": "00000000-0000-0000-0000-000000000002",
    "job_1": "00000000-0000-0000-0000-000000000011",
    "outbox_1": "00000000-0000-0000-0000-000000000012",
    "response_1": "00000000-0000-0000-0000-000000000101",
    "response_2": "00000000-0000-0000-0000-000000000102",
    "draft_1w": "00000000-0000-0000-0000-000000000111",
    "draft_1p": "00000000-0000-0000-0000-000000000112",
    "draft_2w": "00000000-0000-0000-0000-000000000121",
    "draft_2p": "00000000-0000-0000-0000-000000000122",
    "eval_1w": "00000000-0000-0000-0000-000000000131",
    "eval_1p": "00000000-0000-0000-0000-000000000132",
    "eval_2w": "00000000-0000-0000-0000-000000000141",
    "eval_2p": "00000000-0000-0000-0000-000000000142",
    "principal": "00000000-0000-0000-0000-000000000201",
    "binding": "00000000-0000-0000-0000-000000000202",
    "start_auth": "00000000-0000-0000-0000-000000000203",
    "arbitrate_auth": "00000000-0000-0000-0000-000000000204",
    "approval_1": "00000000-0000-0000-0000-000000000301",
    "approval_2": "00000000-0000-0000-0000-000000000302",
    "department_1a": "00000000-0000-0000-0000-000000000311",
    "department_2a": "00000000-0000-0000-0000-000000000312",
    "proposal_1a": "00000000-0000-0000-0000-000000000321",
    "proposal_2a": "00000000-0000-0000-0000-000000000322",
}


def _guardrail_reason(database_url: str | None, ddl_authorized: bool) -> str | None:
    if not database_url:
        return "PostgreSQL integration pending: no dedicated test database identified"
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg2"}:
        return "PostgreSQL integration blocked: URL is not PostgreSQL"
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return "PostgreSQL integration blocked: database is not loopback-isolated"
    try:
        port = parsed.port
    except ValueError:
        return "PostgreSQL integration blocked: URL port is invalid"
    if port != EXPECTED_PORT:
        return "PostgreSQL integration blocked: unexpected test database port"
    if parsed.username != EXPECTED_USER:
        return "PostgreSQL integration blocked: unexpected test database user"
    if (parsed.path or "").lstrip("/") != EXPECTED_DATABASE:
        return "PostgreSQL integration blocked: unexpected disposable database name"
    if not ddl_authorized:
        return "PostgreSQL DDL pending explicit authorization for the identified test database"
    return None


def _authorization_reason() -> str | None:
    return _guardrail_reason(DATABASE_URL, DDL_AUTHORIZED)


def _psycopg2_dsn(url: str) -> str:
    """Strip SQLAlchemy's driver suffix without changing URL credentials."""
    parsed = urlparse(url)
    scheme = "postgresql" if parsed.scheme == "postgresql+psycopg2" else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme))


def test_connection_guardrail_requires_exact_disposable_target() -> None:
    valid = (
        "postgresql+psycopg2://trackflow_gate2b1:test@127.0.0.1:65470/"
        "trackflow_rfp_approval_test_2b1"
    )
    assert _guardrail_reason(valid, True) is None
    assert "loopback" in (_guardrail_reason(valid.replace("127.0.0.1", "db.internal"), True) or "")
    assert "port" in (_guardrail_reason(valid.replace("65470", "65471"), True) or "")
    assert "user" in (_guardrail_reason(valid.replace("trackflow_gate2b1", "other_user"), True) or "")
    assert "database name" in (
        _guardrail_reason(valid.replace("trackflow_rfp_approval_test_2b1", "other_test"), True)
        or ""
    )
    assert "authorization" in (_guardrail_reason(valid, False) or "")


def _execute_file(connection, path: Path) -> None:
    with connection.cursor() as cursor:
        cursor.execute(path.read_text(encoding="utf-8"))


def _seed_part1(connection) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO rfp_tickets(ticket_id,status,raw_pdf_path,uploaded_by_subject) "
            "VALUES (%s,'analisis_completo',%s,'fixture')",
            [(IDS["ticket_1"], "historical-1.pdf"), (IDS["ticket_2"], "historical-2.pdf")],
        )
        cursor.executemany(
            "INSERT INTO rfp_handoffs(ticket_id,contract_version,summary_for_sales,payload) "
            "VALUES (%s,'rfp-intake-v1','historical',%s::jsonb)",
            [(IDS["ticket_1"], '{"source":1}'), (IDS["ticket_2"], '{"source":2}')],
        )
        cursor.execute(
            "INSERT INTO rfp_jobs(job_id,ticket_id,status,attempt_count,max_attempts) "
            "VALUES (%s,%s,'succeeded',1,3)",
            (IDS["job_1"], IDS["ticket_1"]),
        )
        cursor.execute(
            "INSERT INTO rfp_outbox(outbox_id,job_id,attempt_no,status,publish_attempts) "
            "VALUES (%s,%s,1,'published',1)",
            (IDS["outbox_1"], IDS["job_1"]),
        )


def _seed_part2(connection) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO rfp_response_runs("
            "run_id,ticket_id,source_handoff_version,source_handoff_hash,status"
            ") VALUES (%s,%s,'rfp-intake-v1',%s,'ready_for_part3')",
            [
                (IDS["response_1"], IDS["ticket_1"], H["1"]),
                (IDS["response_2"], IDS["ticket_2"], H["2"]),
            ],
        )
        cursor.executemany(
            "INSERT INTO rfp_response_drafts("
            "draft_id,run_id,scope_key,iteration_count,contract_version,payload,payload_hash"
            ") VALUES (%s,%s,%s,1,%s,'{}'::jsonb,%s)",
            [
                (IDS["draft_1w"], IDS["response_1"], "warehouse", "rfp-department-draft-v1", H["3"]),
                (IDS["draft_1p"], IDS["response_1"], "proposal", "rfp-global-commercial-terms-v1", H["4"]),
                (IDS["draft_2w"], IDS["response_2"], "warehouse", "rfp-department-draft-v1", H["5"]),
                (IDS["draft_2p"], IDS["response_2"], "proposal", "rfp-global-commercial-terms-v1", H["6"]),
            ],
        )
        cursor.executemany(
            "INSERT INTO rfp_response_evaluations("
            "evaluation_id,run_id,draft_id,scope_key,contract_version,overall_pass,"
            "terminal_status,payload,payload_hash"
            ") VALUES (%s,%s,%s,%s,%s,true,'ready_for_part3','{}'::jsonb,%s)",
            [
                (IDS["eval_1w"], IDS["response_1"], IDS["draft_1w"], "warehouse", "section-evaluation-v1", H["7"]),
                (IDS["eval_1p"], IDS["response_1"], IDS["draft_1p"], "proposal", "proposal-evaluation-v1", H["8"]),
                (IDS["eval_2w"], IDS["response_2"], IDS["draft_2w"], "warehouse", "section-evaluation-v1", H["7"]),
                (IDS["eval_2p"], IDS["response_2"], IDS["draft_2p"], "proposal", "proposal-evaluation-v1", H["8"]),
            ],
        )
        cursor.executemany(
            "INSERT INTO rfp_response_handoffs("
            "run_id,ticket_id,contract_version,outcome,payload,payload_hash"
            ") VALUES (%s,%s,'rfp-response-v1','ready_for_part3','{}'::jsonb,%s)",
            [
                (IDS["response_1"], IDS["ticket_1"], H["1"]),
                (IDS["response_2"], IDS["ticket_2"], H["2"]),
            ],
        )


def _historical_snapshot(connection) -> dict[str, list[tuple]]:
    queries = {
        "tickets": "SELECT ticket_id,status,raw_pdf_path FROM rfp_tickets ORDER BY ticket_id",
        "runs": "SELECT run_id,ticket_id,source_handoff_hash FROM rfp_response_runs ORDER BY run_id",
        "drafts": "SELECT draft_id,run_id,payload_hash FROM rfp_response_drafts ORDER BY draft_id",
        "evaluations": "SELECT evaluation_id,run_id,draft_id,payload_hash FROM rfp_response_evaluations ORDER BY evaluation_id",
        "handoffs": "SELECT run_id,ticket_id,payload_hash FROM rfp_response_handoffs ORDER BY run_id",
    }
    result = {}
    with connection.cursor() as cursor:
        for name, query in queries.items():
            cursor.execute(query)
            result[name] = cursor.fetchall()
    return result


def _seed_part3_roots(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_principals("
            "principal_id,environment,trackflow_user_id,responsible_code"
            ") VALUES (%s,'qa','qa-miguel','miguel_torres')",
            (IDS["principal"],),
        )
        cursor.execute(
            "INSERT INTO rfp_identity_bindings("
            "binding_id,principal_id,environment,auth_source,issuer,subject"
            ") VALUES (%s,%s,'qa','local','urn:trackflow:qa:local','qa-miguel')",
            (IDS["binding"], IDS["principal"]),
        )
        cursor.executemany(
            "INSERT INTO rfp_authorizations("
            "authorization_id,principal_id,environment,permission,department_scope,"
            "trigger_scope,target_policy,scope_fingerprint,active_from,granted_by_principal_id"
            ") VALUES (%s,%s,'qa',%s,%s,%s,%s,%s,clock_timestamp(),%s)",
            [
                (IDS["start_auth"], IDS["principal"], "start_approval_run", None, None, "run", H["1"], IDS["principal"]),
                (IDS["arbitrate_auth"], IDS["principal"], "arbitrate", None, "currency-mismatch", "cross_department", H["2"], IDS["principal"]),
            ],
        )
        cursor.executemany(
            "INSERT INTO rfp_approval_runs("
            "approval_run_id,ticket_id,response_run_id,contract_version,response_handoff_version,"
            "response_handoff_hash,active_departments,status,start_idempotency_key,start_request_hash,"
            "started_by_principal_id,started_by_binding_id,start_authorization_id,authorization_version,"
            "environment,started_at"
            ") VALUES (%s,%s,%s,'rfp-approval-start-v1','rfp-response-v1',%s,"
            "'[\"warehouse\"]'::jsonb,'active',%s,%s,%s,%s,%s,1,'qa',clock_timestamp())",
            [
                (IDS["approval_1"], IDS["ticket_1"], IDS["response_1"], H["1"], str(uuid4()), H["3"], IDS["principal"], IDS["binding"], IDS["start_auth"]),
                (IDS["approval_2"], IDS["ticket_2"], IDS["response_2"], H["2"], str(uuid4()), H["4"], IDS["principal"], IDS["binding"], IDS["start_auth"]),
            ],
        )
        revision_rows = [
            (IDS["department_1a"], IDS["approval_1"], IDS["ticket_1"], IDS["response_1"], IDS["draft_1w"], H["3"], IDS["eval_1w"], H["7"], H["3"]),
            (IDS["department_2a"], IDS["approval_2"], IDS["ticket_2"], IDS["response_2"], IDS["draft_2w"], H["5"], IDS["eval_2w"], H["7"], H["5"]),
        ]
        cursor.executemany(
            "INSERT INTO rfp_approval_department_revisions("
            "revision_id,approval_run_id,ticket_id,response_run_id,department_id,review_revision_no,"
            "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
            "source_part2_evaluation_hash,source_part2_iteration,content_payload,content_hash,"
            "validation_payload,validation_hash,evaluated_content_hash,status,created_by_principal_id,created_at"
            ") VALUES (%s,%s,%s,%s,'warehouse',1,%s,%s,%s,%s,1,'{}'::jsonb,%s,"
            "'{}'::jsonb,%s,%s,'pending_approval',%s,clock_timestamp())",
            [row + (H["9"], row[-1], IDS["principal"]) for row in revision_rows],
        )
        proposal_rows = [
            (IDS["proposal_1a"], IDS["approval_1"], IDS["ticket_1"], IDS["response_1"], IDS["draft_1p"], H["4"], IDS["eval_1p"], H["8"], IDS["department_1a"], H["4"]),
            (IDS["proposal_2a"], IDS["approval_2"], IDS["ticket_2"], IDS["response_2"], IDS["draft_2p"], H["6"], IDS["eval_2p"], H["8"], IDS["department_2a"], H["6"]),
        ]
        cursor.executemany(
            "INSERT INTO rfp_approval_proposal_revisions("
            "proposal_revision_id,approval_run_id,ticket_id,response_run_id,revision_no,"
            "source_global_draft_id,source_global_draft_hash,source_global_evaluation_id,"
            "source_global_evaluation_hash,warehouse_revision_id,content_payload,content_hash,"
            "validation_payload,validation_hash,evaluated_content_hash,eligible_for_synthesis,created_at"
            ") VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,%s,'{}'::jsonb,%s,'{}'::jsonb,%s,%s,true,clock_timestamp())",
            [row + (H["9"], row[-1]) for row in proposal_rows],
        )


@pytest.fixture
def migrated_database():
    import psycopg2
    from psycopg2 import sql

    reason = _authorization_reason()
    if reason is not None:
        pytest.skip(reason)
    assert DATABASE_URL is not None
    connection = psycopg2.connect(_psycopg2_dsn(DATABASE_URL))
    connection.autocommit = True
    schema = f"gate2b1_{uuid4().hex}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, inet_server_addr()::text")
        database, user, _server_address = cursor.fetchone()
        assert database == EXPECTED_DATABASE
        assert user == EXPECTED_USER
        cursor.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
            "('pg_catalog','information_schema')"
        )
        assert cursor.fetchone()[0] == 0, "preflight requires an empty disposable database"
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    try:
        _execute_file(connection, ROOT / "tests/fixtures/rfp_response/rfp_intake_part1_baseline.sql")
        _seed_part1(connection)
        _execute_file(connection, ROOT / "infra/sql/rfp_response_generation.sql")
        _seed_part2(connection)
        before = _historical_snapshot(connection)
        _execute_file(connection, ROOT / "infra/sql/rfp_approval.sql")
        _execute_file(connection, ROOT / "infra/sql/rfp_approval.sql")
        _seed_part3_roots(connection)
        yield connection, before
    finally:
        connection.rollback()
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SET search_path TO public")
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        connection.close()


def _supersede_department(connection, old_id: str, new_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS fk_rfp_department_revision_successor DEFERRED")
        cursor.execute(
            "UPDATE rfp_approval_department_revisions SET status='superseded',is_current=false,"
            "superseded_by_revision_id=%s,state_version=state_version+1 "
            "WHERE revision_id=%s AND is_current AND state_version=0",
            (new_id, old_id),
        )
        assert cursor.rowcount == 1
        cursor.execute(
            "INSERT INTO rfp_approval_department_revisions("
            "revision_id,approval_run_id,ticket_id,response_run_id,department_id,review_revision_no,"
            "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
            "source_part2_evaluation_hash,source_part2_iteration,supersedes_revision_id,"
            "content_payload,content_hash,validation_payload,validation_hash,evaluated_content_hash,"
            "status,created_by_principal_id,created_at"
            ") SELECT %s,approval_run_id,ticket_id,response_run_id,department_id,2,"
            "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
            "source_part2_evaluation_hash,source_part2_iteration,revision_id,'{}'::jsonb,%s,"
            "'{}'::jsonb,%s,%s,'pending_approval',created_by_principal_id,clock_timestamp() "
            "FROM rfp_approval_department_revisions WHERE revision_id=%s",
            (new_id, H["8"], H["9"], H["8"], old_id),
        )


def test_forward_migration_twice_preserves_representative_history(migrated_database) -> None:
    connection, before = migrated_database
    assert _historical_snapshot(connection) == before
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name LIKE 'rfp_%'"
        )
        assert cursor.fetchone()[0] == 29


def test_department_and_proposal_supersession_is_atomic(migrated_database) -> None:
    import psycopg2

    connection, _ = migrated_database
    department_b = "00000000-0000-0000-0000-000000000313"
    proposal_b = "00000000-0000-0000-0000-000000000323"
    connection.autocommit = False
    try:
        _supersede_department(connection, IDS["department_1a"], department_b)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS fk_rfp_proposal_successor DEFERRED")
            cursor.execute(
                "UPDATE rfp_approval_proposal_revisions SET is_current=false,"
                "superseded_by_revision_id=%s,state_version=state_version+1 "
                "WHERE proposal_revision_id=%s AND is_current AND state_version=0",
                (proposal_b, IDS["proposal_1a"]),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                "INSERT INTO rfp_approval_proposal_revisions("
                "proposal_revision_id,approval_run_id,ticket_id,response_run_id,revision_no,"
                "source_global_draft_id,source_global_draft_hash,source_global_evaluation_id,"
                "source_global_evaluation_hash,supersedes_revision_id,warehouse_revision_id,"
                "content_payload,content_hash,validation_payload,validation_hash,"
                "evaluated_content_hash,eligible_for_synthesis,created_at"
                ") SELECT %s,approval_run_id,ticket_id,response_run_id,2,source_global_draft_id,"
                "source_global_draft_hash,source_global_evaluation_id,source_global_evaluation_hash,"
                "proposal_revision_id,%s,'{}'::jsonb,%s,'{}'::jsonb,%s,%s,true,clock_timestamp() "
                "FROM rfp_approval_proposal_revisions WHERE proposal_revision_id=%s",
                (proposal_b, department_b, H["8"], H["9"], H["8"], IDS["proposal_1a"]),
            )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT revision_id FROM rfp_approval_department_revisions "
                "WHERE approval_run_id=%s AND department_id='warehouse' AND is_current",
                (IDS["approval_1"],),
            )
            assert cursor.fetchall() == [(department_b,)]
            with pytest.raises(psycopg2.IntegrityError):
                cursor.execute(
                    "INSERT INTO rfp_approval_department_revisions("
                    "revision_id,approval_run_id,ticket_id,response_run_id,department_id,review_revision_no,"
                    "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
                    "source_part2_evaluation_hash,source_part2_iteration,supersedes_revision_id,"
                    "content_payload,content_hash,validation_payload,validation_hash,evaluated_content_hash,"
                    "status,created_by_principal_id,created_at"
                    ") SELECT %s,approval_run_id,ticket_id,response_run_id,department_id,3,"
                    "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
                    "source_part2_evaluation_hash,source_part2_iteration,revision_id,'{}'::jsonb,%s,"
                    "'{}'::jsonb,%s,%s,'pending_approval',created_by_principal_id,clock_timestamp() "
                    "FROM rfp_approval_department_revisions WHERE revision_id=%s",
                    ("00000000-0000-0000-0000-000000000314", H["7"], H["9"], H["7"], department_b),
                )
        connection.rollback()
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.IntegrityError):
                cursor.execute(
                    "INSERT INTO rfp_approval_proposal_revisions("
                    "proposal_revision_id,approval_run_id,ticket_id,response_run_id,revision_no,"
                    "source_global_draft_id,source_global_draft_hash,source_global_evaluation_id,"
                    "source_global_evaluation_hash,supersedes_revision_id,warehouse_revision_id,"
                    "content_payload,content_hash,validation_payload,validation_hash,"
                    "evaluated_content_hash,eligible_for_synthesis,created_at"
                    ") SELECT %s,approval_run_id,ticket_id,response_run_id,3,source_global_draft_id,"
                    "source_global_draft_hash,source_global_evaluation_id,source_global_evaluation_hash,"
                    "proposal_revision_id,%s,'{}'::jsonb,%s,'{}'::jsonb,%s,%s,true,clock_timestamp() "
                    "FROM rfp_approval_proposal_revisions WHERE proposal_revision_id=%s",
                    (str(uuid4()), department_b, H["7"], H["9"], H["7"], proposal_b),
                )
        connection.rollback()
    finally:
        connection.rollback()
        connection.autocommit = True


def test_arbitration_keeps_request_and_later_acceptance(migrated_database) -> None:
    import psycopg2

    connection, _ = migrated_database
    assessment_bad = "00000000-0000-0000-0000-000000000401"
    conflict = "00000000-0000-0000-0000-000000000402"
    request = "00000000-0000-0000-0000-000000000403"
    corrected = "00000000-0000-0000-0000-000000000404"
    assessment_ok = "00000000-0000-0000-0000-000000000405"
    accepted = "00000000-0000-0000-0000-000000000406"
    fingerprint = H["7"]
    request_key = str(uuid4())
    accept_key = str(uuid4())
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rfp_conflict_assessments("
                "assessment_id,approval_run_id,ticket_id,trigger_id,outcome,facts_payload,"
                "missing_input_codes,assessed_content_hashes,assessment_hash,assessed_at"
                ") VALUES (%s,%s,%s,'currency-mismatch','demonstrated','{}'::jsonb,'[]'::jsonb,"
                "'[]'::jsonb,%s,clock_timestamp())",
                (assessment_bad, IDS["approval_1"], IDS["ticket_1"], H["3"]),
            )
            cursor.execute(
                "INSERT INTO rfp_arbitration_conflicts("
                "conflict_id,approval_run_id,ticket_id,assessment_id,assessment_outcome,trigger_id,"
                "affected_targets,fingerprint,status,detected_at"
                ") VALUES (%s,%s,%s,%s,'demonstrated','currency-mismatch','[\"proposal\"]'::jsonb,"
                "%s,'open',clock_timestamp())",
                (conflict, IDS["approval_1"], IDS["ticket_1"], assessment_bad, fingerprint),
            )
            cursor.execute(
                "INSERT INTO rfp_arbitration_resolutions("
                "resolution_id,approval_run_id,ticket_id,conflict_id,trigger_id,conflict_fingerprint,"
                "idempotency_key,action,targets,authorized_input_ids,reason,actor_principal_id,"
                "actor_binding_id,authorization_id,authorization_version,environment,resolution_hash,decided_at"
                ") VALUES (%s,%s,%s,%s,'currency-mismatch',%s,%s,'request_changes',"
                "'[\"proposal\"]'::jsonb,'[]'::jsonb,'correct currency',%s,%s,%s,1,'qa',%s,clock_timestamp())",
                (request, IDS["approval_1"], IDS["ticket_1"], conflict, fingerprint, request_key, IDS["principal"], IDS["binding"], IDS["arbitrate_auth"], H["4"]),
            )
        _supersede_department(connection, IDS["department_1a"], corrected)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rfp_conflict_assessments("
                "assessment_id,approval_run_id,ticket_id,trigger_id,outcome,facts_payload,"
                "missing_input_codes,assessed_content_hashes,assessment_hash,assessed_at"
                ") VALUES (%s,%s,%s,'currency-mismatch','no_conflict','{}'::jsonb,'[]'::jsonb,"
                "'[]'::jsonb,%s,clock_timestamp())",
                (assessment_ok, IDS["approval_1"], IDS["ticket_1"], H["5"]),
            )
            cursor.execute(
                "INSERT INTO rfp_arbitration_resolutions("
                "resolution_id,approval_run_id,ticket_id,conflict_id,trigger_id,conflict_fingerprint,"
                "idempotency_key,action,targets,authorized_input_ids,reason,actor_principal_id,"
                "actor_binding_id,authorization_id,authorization_version,environment,post_assessment_id,"
                "post_assessment_outcome,resolution_hash,decided_at"
                ") VALUES (%s,%s,%s,%s,'currency-mismatch',%s,%s,'accept_resolution',"
                "'[\"proposal\"]'::jsonb,'[]'::jsonb,'verified correction',%s,%s,%s,1,'qa',%s,"
                "'no_conflict',%s,clock_timestamp())",
                (accepted, IDS["approval_1"], IDS["ticket_1"], conflict, fingerprint, accept_key, IDS["principal"], IDS["binding"], IDS["arbitrate_auth"], assessment_ok, H["6"]),
            )
            cursor.execute(
                "INSERT INTO rfp_arbitration_resolution_evidence("
                "evidence_id,resolution_id,approval_run_id,ticket_id,original_department_revision_id,"
                "corrected_department_revision_id,corrected_content_hash,revision_validation_artifact_id,"
                "revision_validation_hash,conflict_recheck_artifact_id,conflict_recheck_hash"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (str(uuid4()), accepted, IDS["approval_1"], IDS["ticket_1"], IDS["department_1a"], corrected, H["8"], str(uuid4()), H["9"], assessment_ok, H["5"]),
            )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT action FROM rfp_arbitration_resolutions "
                "WHERE conflict_id=%s ORDER BY decided_at,resolution_id",
                (conflict,),
            )
            assert [row[0] for row in cursor.fetchall()] == ["request_changes", "accept_resolution"]
            with pytest.raises(psycopg2.IntegrityError):
                cursor.execute(
                    "INSERT INTO rfp_arbitration_resolutions("
                    "resolution_id,approval_run_id,ticket_id,conflict_id,trigger_id,conflict_fingerprint,"
                    "idempotency_key,action,targets,authorized_input_ids,reason,actor_principal_id,"
                    "actor_binding_id,authorization_id,authorization_version,environment,resolution_hash,decided_at"
                    ") VALUES (%s,%s,%s,%s,'currency-mismatch',%s,%s,'request_changes',"
                    "'[\"proposal\"]'::jsonb,'[]'::jsonb,'retry',%s,%s,%s,1,'qa',%s,clock_timestamp())",
                    (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], conflict, fingerprint, request_key, IDS["principal"], IDS["binding"], IDS["arbitrate_auth"], H["8"]),
                )
        connection.rollback()
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.IntegrityError):
                cursor.execute(
                    "INSERT INTO rfp_arbitration_resolutions("
                    "resolution_id,approval_run_id,ticket_id,conflict_id,trigger_id,conflict_fingerprint,"
                    "idempotency_key,action,targets,authorized_input_ids,reason,actor_principal_id,"
                    "actor_binding_id,authorization_id,authorization_version,environment,post_assessment_id,"
                    "post_assessment_outcome,resolution_hash,decided_at"
                    ") VALUES (%s,%s,%s,%s,'currency-mismatch',%s,%s,'accept_resolution',"
                    "'[\"proposal\"]'::jsonb,'[]'::jsonb,'duplicate acceptance',%s,%s,%s,1,'qa',%s,"
                    "'no_conflict',%s,clock_timestamp())",
                    (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], conflict, fingerprint, str(uuid4()), IDS["principal"], IDS["binding"], IDS["arbitrate_auth"], assessment_ok, H["8"]),
                )
        connection.rollback()
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.RaiseException):
                cursor.execute(
                    "UPDATE rfp_arbitration_resolutions SET reason='changed' WHERE resolution_id=%s",
                    (request,),
                )
        connection.rollback()
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.RaiseException):
                cursor.execute(
                    "DELETE FROM rfp_arbitration_resolutions WHERE resolution_id=%s",
                    (request,),
                )
        connection.rollback()
    finally:
        connection.rollback()
        connection.autocommit = True


def _expect_fk_rejection(connection, statement: str, parameters: tuple) -> None:
    import psycopg2

    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT cross_entity")
        with pytest.raises(psycopg2.IntegrityError):
            cursor.execute(statement, parameters)
        cursor.execute("ROLLBACK TO SAVEPOINT cross_entity")


def test_cross_run_and_ticket_associations_are_rejected(migrated_database) -> None:
    connection, _ = migrated_database
    connection.autocommit = False
    try:
        _expect_fk_rejection(
            connection,
            "INSERT INTO rfp_approval_department_revisions("
            "revision_id,approval_run_id,ticket_id,response_run_id,department_id,review_revision_no,"
            "source_part2_draft_id,source_part2_draft_hash,source_part2_evaluation_id,"
            "source_part2_evaluation_hash,source_part2_iteration,content_payload,content_hash,"
            "validation_payload,validation_hash,evaluated_content_hash,status,created_at"
            ") VALUES (%s,%s,%s,%s,'lastmile',1,%s,%s,%s,%s,1,'{}'::jsonb,%s,'{}'::jsonb,%s,%s,"
            "'pending_approval',clock_timestamp())",
            (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], IDS["response_1"], IDS["draft_2w"], H["5"], IDS["eval_2w"], H["7"], H["5"], H["9"], H["5"]),
        )
        _expect_fk_rejection(
            connection,
            "INSERT INTO rfp_human_decisions("
            "decision_id,approval_run_id,ticket_id,revision_id,department_id,idempotency_key,"
            "application_key,expected_content_hash,action,processing_status,actor_principal_id,"
            "actor_binding_id,authorization_id,authorization_version,environment,decided_at"
            ") VALUES (%s,%s,%s,%s,'warehouse',%s,%s,%s,'approve','queued',%s,%s,%s,1,'qa',clock_timestamp())",
            (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], IDS["department_2a"], str(uuid4()), f"decision-apply:{H['1']}", H["5"], IDS["principal"], IDS["binding"], IDS["start_auth"]),
        )
        _expect_fk_rejection(
            connection,
            "INSERT INTO rfp_final_documents("
            "final_document_id,approval_run_id,ticket_id,proposal_revision_id,warehouse_revision_id,"
            "contract_version,payload,payload_hash,markdown,markdown_hash,storage_reference,generated_at"
            ") VALUES (%s,%s,%s,%s,%s,'rfp-persisted-final-document-v1','{}'::jsonb,%s,'x',%s,'test',clock_timestamp())",
            (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], IDS["proposal_2a"], IDS["department_1a"], H["1"], H["2"]),
        )
        foreign_assessment = str(uuid4())
        foreign_conflict = str(uuid4())
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO rfp_conflict_assessments("
                "assessment_id,approval_run_id,ticket_id,trigger_id,outcome,facts_payload,"
                "missing_input_codes,assessed_content_hashes,assessment_hash,assessed_at"
                ") VALUES (%s,%s,%s,'currency-mismatch','demonstrated','{}'::jsonb,'[]'::jsonb,"
                "'[]'::jsonb,%s,clock_timestamp())",
                (foreign_assessment, IDS["approval_2"], IDS["ticket_2"], H["6"]),
            )
            cursor.execute(
                "INSERT INTO rfp_arbitration_conflicts("
                "conflict_id,approval_run_id,ticket_id,assessment_id,assessment_outcome,trigger_id,"
                "affected_targets,fingerprint,status,detected_at"
                ") VALUES (%s,%s,%s,%s,'demonstrated','currency-mismatch','[\"proposal\"]'::jsonb,"
                "%s,'open',clock_timestamp())",
                (foreign_conflict, IDS["approval_2"], IDS["ticket_2"], foreign_assessment, H["8"]),
            )
        _expect_fk_rejection(
            connection,
            "INSERT INTO rfp_arbitration_resolutions("
            "resolution_id,approval_run_id,ticket_id,conflict_id,trigger_id,conflict_fingerprint,"
            "idempotency_key,action,targets,authorized_input_ids,reason,actor_principal_id,"
            "actor_binding_id,authorization_id,authorization_version,environment,resolution_hash,decided_at"
            ") VALUES (%s,%s,%s,%s,'currency-mismatch',%s,%s,'request_changes',"
            "'[\"proposal\"]'::jsonb,'[]'::jsonb,'cross run',%s,%s,%s,1,'qa',%s,clock_timestamp())",
            (str(uuid4()), IDS["approval_1"], IDS["ticket_1"], foreign_conflict, H["8"], str(uuid4()), IDS["principal"], IDS["binding"], IDS["arbitrate_auth"], H["9"]),
        )
        connection.rollback()
    finally:
        connection.rollback()
        connection.autocommit = True


def test_workflow_event_trigger_rejects_update_and_delete(migrated_database) -> None:
    import psycopg2

    connection, _ = migrated_database
    event = str(uuid4())
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO rfp_workflow_events("
            "event_id,event_key,approval_run_id,ticket_id,thread_id,execution_id,sequence_no,node_name,"
            "event_type,safe_input_ref,safe_output_ref,result,started_at,finished_at"
            ") VALUES (%s,%s,%s,%s,'warehouse',%s,1,'prepare','node',"
            "'{\"artifact_id\":\"input\"}'::jsonb,'{\"artifact_id\":\"output\"}'::jsonb,"
            "'ok',clock_timestamp(),clock_timestamp())",
            (event, f"event:{event}", IDS["approval_1"], IDS["ticket_1"], str(uuid4())),
        )
        with pytest.raises(psycopg2.errors.RaiseException):
            cursor.execute("UPDATE rfp_workflow_events SET result='changed' WHERE event_id=%s", (event,))
        connection.rollback()
        with pytest.raises(psycopg2.errors.RaiseException):
            cursor.execute("DELETE FROM rfp_workflow_events WHERE event_id=%s", (event,))
        connection.rollback()


def test_new_ticket_states_are_accepted_and_invalid_state_is_rejected(migrated_database) -> None:
    import psycopg2

    connection, _ = migrated_database
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE rfp_tickets SET status='esperando_aprobación' WHERE ticket_id=%s",
            (IDS["ticket_1"],),
        )
        cursor.execute(
            "UPDATE rfp_tickets SET status='terminado' WHERE ticket_id=%s",
            (IDS["ticket_1"],),
        )
        with pytest.raises(psycopg2.IntegrityError):
            cursor.execute(
                "UPDATE rfp_tickets SET status='internal_worker_state' WHERE ticket_id=%s",
                (IDS["ticket_1"],),
            )
        connection.rollback()
