"""HTTP contract tests for telemetry ingestion."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import httpx
import pytest

from services.api.database import get_db
from services.api.main import app


VALID_EVENT = {
    "eventId": "550e8400-e29b-41d4-a716-446655440000",
    "timestamp": "2026-09-15T12:00:00Z",
    "sessionId": None,
    "userId": "operator-1",
    "event_type": "inbound_order_created",
    "schemaVersion": "1.0",
    "requestId": None,
    "properties": {},
}


async def post_events(body: object) -> tuple[httpx.Response, Mock]:
    session = Mock()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with patch("services.api.routes.telemetry.bulk_insert_telemetry") as bulk_insert:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post("/telemetry/events", json=body)
            return response, bulk_insert
    finally:
        app.dependency_overrides.clear()


def send(body: object) -> tuple[httpx.Response, Mock]:
    return asyncio.run(post_events(body))


def test_valid_telemetry_batch_is_bulk_stored() -> None:
    response, bulk_insert = send({"events": [VALID_EVENT, VALID_EVENT]})

    assert response.status_code == 200
    assert response.json() == {"received": 2, "stored": 2, "rejected": 0}
    bulk_insert.assert_called_once()
    assert len(bulk_insert.call_args.args[1]) == 2


def test_mixed_telemetry_batch_stores_only_valid_events() -> None:
    invalid = {**VALID_EVENT, "schemaVersion": "2.0"}
    response, bulk_insert = send({"events": [VALID_EVENT, invalid, VALID_EVENT]})

    assert response.status_code == 200
    assert response.json() == {"received": 3, "stored": 2, "rejected": 1}
    bulk_insert.assert_called_once()
    assert len(bulk_insert.call_args.args[1]) == 2


def test_all_invalid_events_do_not_call_storage() -> None:
    response, bulk_insert = send({"events": [{"bad": True}, {"bad": False}]})

    assert response.status_code == 200
    assert response.json() == {"received": 2, "stored": 0, "rejected": 2}
    bulk_insert.assert_not_called()


def test_empty_telemetry_batch_does_not_call_storage() -> None:
    response, bulk_insert = send({"events": []})

    assert response.status_code == 200
    assert response.json() == {"received": 0, "stored": 0, "rejected": 0}
    bulk_insert.assert_not_called()


@pytest.mark.parametrize(
    "event",
    [
        {**VALID_EVENT, "eventId": "not-a-uuid"},
        {**VALID_EVENT, "timestamp": "2026-09-15T12:00:00"},
        {**VALID_EVENT, "schemaVersion": "2.0"},
        {**VALID_EVENT, "unexpected": "value"},
        {key: value for key, value in VALID_EVENT.items() if key != "properties"},
    ],
)
def test_invalid_event_items_are_rejected_individually(event: dict[str, object]) -> None:
    response, bulk_insert = send({"events": [event]})

    assert response.status_code == 200
    assert response.json() == {"received": 1, "stored": 0, "rejected": 1}
    bulk_insert.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"events": None},
        {"events": "hello"},
        {"events": {}, "unexpected": "value"},
        {"events": [], "unexpected": "value"},
    ],
)
def test_invalid_telemetry_envelopes_are_rejected(body: object) -> None:
    response, bulk_insert = send(body)

    assert response.status_code == 422
    bulk_insert.assert_not_called()


def test_storage_failure_is_not_reported_as_success() -> None:
    with patch("services.api.routes.telemetry.bulk_insert_telemetry") as bulk_insert:
        bulk_insert.side_effect = RuntimeError("storage failed")
        session = Mock()
        app.dependency_overrides[get_db] = lambda: session
        try:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

            async def request() -> httpx.Response:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    return await client.post(
                        "/telemetry/events", json={"events": [VALID_EVENT]}
                    )

            response = asyncio.run(request())
        finally:
            app.dependency_overrides.clear()

    assert response.status_code >= 500
