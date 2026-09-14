"""Focused tests for the API request timing middleware."""

from __future__ import annotations

import asyncio

import httpx

from services.api.main import app


def test_health_request_logs_timing_without_query_params(caplog) -> None:
    """Health responses remain unchanged and timing logs contain safe fields."""
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health?secret=should-not-appear")

    with caplog.at_level("INFO", logger="api.timing"):
        response = asyncio.run(run())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    messages = [record.getMessage() for record in caplog.records if record.name == "api.timing"]
    assert len(messages) == 1
    message = messages[0]
    assert "GET /health" in message
    assert "200" in message
    assert "ms" in message
    assert "secret=should-not-appear" not in message
