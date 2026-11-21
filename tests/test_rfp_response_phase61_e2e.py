"""Opt-in Phase 6.1 carrier-model E2E against isolated live services."""

from __future__ import annotations

import json
import os
import time
from uuid import UUID

import httpx
import psycopg2
import pytest


API_ENV = "TRACKFLOW_PHASE61_API_URL"
DB_ENV = "TRACKFLOW_PHASE61_DATABASE_URL"
TICKETS = {
    "unknown": UUID("61000000-0000-4000-8000-000000000001"),
    "customer_managed": UUID("61000000-0000-4000-8000-000000000002"),
    "trackflow_managed": UUID("61000000-0000-4000-8000-000000000003"),
}

pytestmark = pytest.mark.skipif(
    not os.getenv(API_ENV) or not os.getenv(DB_ENV),
    reason=f"{API_ENV} and {DB_ENV} are required for isolated Phase 6.1 QA",
)


def _token(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _terminal(client: httpx.Client, headers: dict[str, str], ticket_id: UUID) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = client.get(f"/rfps/{ticket_id}/response", headers=headers)
        # Reads fail closed if concurrent persistence yields an unverifiable snapshot.
        # A persistent 503 still fails this test at the deadline.
        if response.status_code == 503:
            time.sleep(0.25)
            continue
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] in {
            "ready_for_part3", "needs_human_review", "technical_failure",
        }:
            return value
        time.sleep(0.25)
    raise AssertionError("response run did not become terminal")


def test_carrier_models_cross_http_celery_langgraph_and_postgres() -> None:
    with httpx.Client(base_url=os.environ[API_ENV], timeout=20) as client:
        admin = {"Authorization": "Bearer " + _token(
            client, "phase61-admin@trackflow.test", "Phase61Admin123!")}
        user = {"Authorization": "Bearer " + _token(
            client, "phase61-user@trackflow.test", "Phase61User123!")}

        assert client.post(
            f"/rfps/{TICKETS['customer_managed']}/response",
            json={"carrier_model": "customer_managed"},
        ).status_code == 401
        assert client.post(
            f"/rfps/{TICKETS['customer_managed']}/response", headers=user,
            json={"carrier_model": "customer_managed"},
        ).status_code == 403

        accepted = {}
        for model, ticket_id in TICKETS.items():
            response = client.post(
                f"/rfps/{ticket_id}/response", headers=admin,
                json={"carrier_model": model},
            )
            assert response.status_code == 202, response.text
            accepted[model] = response.json()

        repeated = client.post(
            f"/rfps/{TICKETS['customer_managed']}/response", headers=admin,
            json={"carrier_model": "customer_managed"},
        )
        assert repeated.status_code == 202
        assert repeated.json() == accepted["customer_managed"]
        conflict = client.post(
            f"/rfps/{TICKETS['customer_managed']}/response", headers=admin,
            json={"carrier_model": "trackflow_managed"},
        )
        assert conflict.status_code == 409

        results = {
            model: _terminal(client, admin, ticket_id)
            for model, ticket_id in TICKETS.items()
        }

    customer = results["customer_managed"]
    assert customer["carrier_model"] == "customer_managed"
    assert customer["status"] == "ready_for_part3"
    customer_section = customer["departments"][0]
    assert customer_section["status"] == "passed"
    assert customer_section["iteration_count"] == 2
    assert customer_section["evaluation"]["blocks_commercial_completeness"] is False
    assert customer_section["evaluation"]["requires_human_input"] is False
    assert customer_section["pending_commercial_items"] == []
    assert "The customer manages the carrier." in customer_section["preview"]["content"]
    assert "does not set its last-mile price" in customer_section["preview"]["content"]
    global_terms = json.loads(customer["global_terms"]["preview"]["content"])
    assert global_terms["sla"]["applicability"] == "not_applicable"
    assert global_terms["sla"]["percentage"] is None

    unknown = results["unknown"]
    assert unknown["carrier_model"] == "unknown"
    assert unknown["status"] == "needs_human_review"
    assert {item["code"] for item in unknown["departments"][0][
        "pending_commercial_items"]} == {"CARRIER_MODEL_REQUIRED"}

    trackflow = results["trackflow_managed"]
    assert trackflow["carrier_model"] == "trackflow_managed"
    assert trackflow["status"] == "needs_human_review"
    assert {item["code"] for item in trackflow["departments"][0][
        "pending_commercial_items"]} == {"LAST_MILE_RATE_REQUIRED"}
    trackflow_global = json.loads(trackflow["global_terms"]["preview"]["content"])
    assert trackflow_global["sla"]["applicability"] == "applicable"
    assert trackflow_global["sla"]["percentage"] == 90

    for result in results.values():
        encoded = json.dumps(result)
        assert "carrier_confirmation_actor" not in encoded
        assert "carrier_confirmation_source" not in encoded
        assert "carrier_confirmed_at" not in encoded
        assert "generated_content" not in encoded

    with psycopg2.connect(os.environ[DB_ENV]) as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT ticket_id,carrier_model,carrier_confirmation_actor,
                   carrier_confirmation_source,carrier_confirmed_at
            FROM rfp_response_runs WHERE ticket_id=ANY(%s::uuid[]) ORDER BY ticket_id
        """, ([str(value) for value in TICKETS.values()],))
        rows = cursor.fetchall()
        cursor.execute("""
            SELECT d.payload
            FROM rfp_response_drafts AS d
            JOIN rfp_response_runs AS r ON r.run_id=d.run_id
            WHERE r.ticket_id=%s AND d.scope_key='lastmile'
            ORDER BY d.iteration_count DESC LIMIT 1
        """, (str(TICKETS["customer_managed"]),))
        customer_draft = cursor.fetchone()[0]
    assert len(rows) == 3
    assert {row[1] for row in rows} == set(TICKETS)
    assert all(row[2] and row[3] == "admin_response_start" and row[4] for row in rows)
    carrier_claims = [
        claim for claim in customer_draft["supported_claims"]
        if claim["statement"] in {
            "The customer manages the carrier.",
            "TrackFlow does not set its last-mile price or delivery promise.",
        }
    ]
    assert len(carrier_claims) == 2
    for carrier_claim in carrier_claims:
        carrier_evidence = carrier_claim["evidence"][0]
        assert carrier_evidence["document_id"] == "admin-confirmation"
        assert carrier_evidence["section_id"] == "carrier_model"
        assert carrier_evidence["document_id"] != "rfp-intake-v1"
