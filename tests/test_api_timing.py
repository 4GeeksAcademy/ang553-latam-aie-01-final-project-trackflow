"""Focused tests for the API request timing middleware."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from services.api.main import app


def test_health_request_logs_timing_without_query_params(caplog) -> None:
    """Health responses remain unchanged and timing logs contain safe fields."""
    timing_logger = logging.getLogger("api.timing")
    timing_logger.addHandler(caplog.handler)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health?secret=should-not-appear")

    try:
        with caplog.at_level("INFO", logger="api.timing"):
            response = asyncio.run(run())
    finally:
        timing_logger.removeHandler(caplog.handler)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    messages = [record.getMessage() for record in caplog.records if record.name == "api.timing"]
    assert len(messages) == 1
    message = messages[0]
    assert "GET /health" in message
    assert "200" in message
    assert "ms" in message
    assert "requestId=" in message
    assert "secret=should-not-appear" not in message


def test_valid_request_id_is_preserved() -> None:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(
                "/health",
                headers={"X-Request-ID": "550e8400-e29b-41d4-a716-446655440000"},
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "550e8400-e29b-41d4-a716-446655440000"


def test_missing_request_id_gets_uuid_v4() -> None:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    response = asyncio.run(run())
    request_id = response.headers["X-Request-ID"]

    assert response.status_code == 200
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        request_id,
    )


def test_invalid_request_id_is_replaced_with_uuid_v4() -> None:
    async def run(value: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health", headers={"X-Request-ID": value})

    for invalid_value in ("not-a-request-id", "550e8400-e29b-11d4-a716-446655440000"):
        response = asyncio.run(run(invalid_value))
        request_id = response.headers["X-Request-ID"]

        assert response.status_code == 200
        assert request_id != invalid_value
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            request_id,
        )
