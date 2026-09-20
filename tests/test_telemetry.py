"""Focused tests for the integrated telemetry stub."""

from __future__ import annotations

import asyncio

import httpx

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


async def post_events(body: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/telemetry/events", json=body)


def test_valid_telemetry_batch() -> None:
    response = asyncio.run(post_events({"events": [VALID_EVENT]}))

    assert response.status_code == 200
    assert response.json() == {"received": 1}


def test_empty_telemetry_batch() -> None:
    response = asyncio.run(post_events({"events": []}))

    assert response.status_code == 200
    assert response.json() == {"received": 0}


def test_telemetry_root_array_is_rejected() -> None:
    response = asyncio.run(post_events([VALID_EVENT]))

    assert response.status_code == 422


def test_invalid_telemetry_envelope_is_rejected() -> None:
    event = {**VALID_EVENT, "schemaVersion": "2.0"}
    response = asyncio.run(post_events({"events": [event]}))

    assert response.status_code == 422


def test_extra_telemetry_envelope_field_is_rejected() -> None:
    event = {**VALID_EVENT, "unexpected": "value"}
    response = asyncio.run(post_events({"events": [event]}))

    assert response.status_code == 422


def test_extra_telemetry_batch_field_is_rejected() -> None:
    response = asyncio.run(
        post_events({"events": [VALID_EVENT], "unexpected": "value"})
    )

    assert response.status_code == 422
