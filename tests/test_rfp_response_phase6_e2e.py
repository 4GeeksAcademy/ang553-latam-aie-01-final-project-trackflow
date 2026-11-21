"""Opt-in live Phase 6 QA through HTTP, relay, Celery, LangGraph and PostgreSQL."""

from __future__ import annotations

import json
import os
import time
from uuid import UUID

import httpx
import psycopg2
import pytest


API_ENV = "TRACKFLOW_PHASE6_API_URL"
DB_ENV = "TRACKFLOW_PHASE6_DATABASE_URL"
TICKETS = {
    "single": UUID("60000000-0000-4000-8000-000000000001"),
    "multi_pending": UUID("60000000-0000-4000-8000-000000000002"),
    "unknown_carrier": UUID("60000000-0000-4000-8000-000000000003"),
    "three_generations": UUID("60000000-0000-4000-8000-000000000004"),
}

pytestmark = pytest.mark.skipif(
    not os.getenv(API_ENV) or not os.getenv(DB_ENV),
    reason=f"{API_ENV} and {DB_ENV} are required for isolated Phase 6 QA",
)


def _token(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _get_terminal(client: httpx.Client, headers: dict[str, str], ticket_id: UUID) -> dict:
    deadline = time.monotonic() + 120
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/rfps/{ticket_id}/response", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"ready_for_part3", "needs_human_review", "technical_failure"}:
            return last
        time.sleep(0.25)
    raise AssertionError(f"response run did not become terminal: {last}")


def _database_snapshot(database_url: str) -> dict:
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT json_build_object(
              'runs',(SELECT count(*) FROM rfp_response_runs),
              'response_jobs',(SELECT count(*) FROM rfp_jobs WHERE job_kind='response_generation'),
              'response_outbox',(SELECT count(*) FROM rfp_outbox o JOIN rfp_jobs j USING(job_id)
                                  WHERE j.job_kind='response_generation'),
              'published',(SELECT count(*) FROM rfp_outbox o JOIN rfp_jobs j USING(job_id)
                           WHERE j.job_kind='response_generation' AND o.status='published'),
              'succeeded',(SELECT count(*) FROM rfp_jobs WHERE job_kind='response_generation'
                           AND status='succeeded'),
              'drafts',(SELECT count(*) FROM rfp_response_drafts),
              'evaluations',(SELECT count(*) FROM rfp_response_evaluations),
              'handoffs',(SELECT count(*) FROM rfp_response_handoffs),
              'failures',(SELECT count(*) FROM rfp_response_execution_failures)
            )
        """)
        return cursor.fetchone()[0]


def test_live_response_generation_and_security_boundaries() -> None:
    api_url = os.environ[API_ENV]
    database_url = os.environ[DB_ENV]
    with httpx.Client(base_url=api_url, timeout=20) as client:
        admin = {"Authorization": "Bearer " + _token(
            client, "phase6-admin@trackflow.test", "Phase6Admin123!")}
        user = {"Authorization": "Bearer " + _token(
            client, "phase6-user@trackflow.test", "Phase6User123!")}

        assert client.post(f"/rfps/{TICKETS['single']}/response").status_code == 401
        assert client.post(f"/rfps/{TICKETS['single']}/response", headers=user).status_code == 403
        assert client.post(
            "/rfps/60000000-0000-4000-8000-999999999999/response", headers=admin
        ).status_code == 404

        accepted = {}
        for name, ticket_id in TICKETS.items():
            response = client.post(f"/rfps/{ticket_id}/response", headers=admin)
            assert response.status_code == 202, response.text
            accepted[name] = response.json()
        repeated = client.post(f"/rfps/{TICKETS['single']}/response", headers=admin)
        assert repeated.status_code == 202
        assert repeated.json() == accepted["single"]

        results = {name: _get_terminal(client, admin, ticket_id)
                   for name, ticket_id in TICKETS.items()}

        assert results["single"]["status"] == "ready_for_part3"
        assert results["single"]["outcome"] == "ready_for_part3"
        assert results["single"]["technical_failure"] is False
        assert results["single"]["departments"][0]["iteration_count"] == 1
        assert results["single"]["departments"][0]["preview"]["gate_status"] == "passed"
        assert results["single"]["departments"][0]["preview"]["content"]

        multi = results["multi_pending"]
        assert multi["status"] == "needs_human_review"
        assert {item["department"] for item in multi["departments"]} == {"warehouse", "reverse"}
        reverse = next(item for item in multi["departments"] if item["department"] == "reverse")
        assert reverse["iteration_count"] == 1
        assert reverse["evaluation"]["requires_human_input"] is True
        assert reverse["pending_commercial_items"][0]["owner"] == "Sofía Ramos"

        carrier = results["unknown_carrier"]["departments"][0]
        assert carrier["department"] == "lastmile"
        assert carrier["evaluation"]["requires_human_input"] is True
        assert {item["code"] for item in carrier["pending_commercial_items"]} == {
            "CARRIER_MODEL_REQUIRED"
        }
        assert carrier["pending_commercial_items"][0]["owner"] == "Carlos Vega"

        exhausted = results["three_generations"]["departments"][0]
        assert exhausted["iteration_count"] == 3
        assert exhausted["status"] == "iteration_limit_reached"
        assert exhausted["evaluation"]["terminal_status"] == "iteration_limit_reached"
        assert exhausted["evaluation"]["missing_aspect_ids"]
        assert exhausted["evaluation"]["actionable_feedback"]

        for result in results.values():
            encoded = json.dumps(result)
            assert "generated_content" not in encoded
            assert "diagnostic_reference" not in encoded
            assert "payload" not in encoded
            assert result["job"]["status"] == "succeeded"
            assert result["outcome"] in {"ready_for_part3", "needs_human_review"}

        cross_ticket = client.get(
            f"/rfps/{TICKETS['multi_pending']}/response/{accepted['single']['run_id']}",
            headers=admin,
        )
        assert cross_ticket.status_code == 404

    snapshot = _database_snapshot(database_url)
    assert snapshot == {
        "runs": 4, "response_jobs": 4, "response_outbox": 4, "published": 4,
        "succeeded": 4, "drafts": 11, "evaluations": 11, "handoffs": 4,
        "failures": 0,
    }


def test_live_api_fails_closed_for_unverified_preview_and_internal_diagnostic() -> None:
    api_url = os.environ[API_ENV]
    database_url = os.environ[DB_ENV]
    ticket_id = TICKETS["multi_pending"]
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT p.preview_id FROM rfp_response_previews p
            JOIN rfp_response_drafts d USING(draft_id)
            WHERE d.run_id=(SELECT run_id FROM rfp_response_runs WHERE ticket_id=%s)
              AND d.scope_key='reverse'
            ORDER BY d.iteration_count DESC LIMIT 1
        """, (str(ticket_id),))
        preview_id = cursor.fetchone()[0]
        with pytest.raises(psycopg2.errors.CheckViolation):
            cursor.execute("""
                UPDATE rfp_response_previews SET gate_status='indeterminate',
                  safe_content='CONFIDENTIAL UPS RATE 1.00 USD',
                  reason_codes='["UNVERIFIED_CONTENT"]'::jsonb
                WHERE preview_id=%s
            """, (preview_id,))
        connection.rollback()
        cursor.execute("""
            UPDATE rfp_response_previews SET gate_status='indeterminate',
              safe_content=NULL, reason_codes='["UNVERIFIED_CONTENT"]'::jsonb
            WHERE preview_id=%s
        """, (preview_id,))
        cursor.execute("""
            INSERT INTO rfp_response_execution_failures(
              failure_id,run_id,ticket_id,draft_id,evaluator,infrastructure_attempts,
              error_code,diagnostic_reference,terminal,occurred_at)
            SELECT gen_random_uuid(),d.run_id,%s,d.draft_id,'relevance',2,
              'qa_dependency_timeout','private/phase6/diagnostic-secret',false,clock_timestamp()
            FROM rfp_response_drafts d
            JOIN rfp_response_runs r USING(run_id)
            WHERE r.ticket_id=%s AND d.scope_key='reverse'
            ORDER BY d.iteration_count DESC LIMIT 1
        """, (str(ticket_id), str(ticket_id)))
        connection.commit()

    with httpx.Client(base_url=api_url, timeout=20) as client:
        admin = {"Authorization": "Bearer " + _token(
            client, "phase6-admin@trackflow.test", "Phase6Admin123!")}
        response = client.get(f"/rfps/{ticket_id}/response", headers=admin)
        assert response.status_code == 200
        body = response.json()
        reverse = next(item for item in body["departments"] if item["department"] == "reverse")
        assert reverse["preview"] == {
            "gate_status": "indeterminate", "content": None,
            "reason_codes": ["UNVERIFIED_CONTENT"],
        }
        encoded = response.text
        assert "CONFIDENTIAL UPS RATE" not in encoded
        assert "diagnostic-secret" not in encoded
        assert body["operational_failures"][0]["error_code"] == "qa_dependency_timeout"
