"""Temporary telemetry ingestion endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from services.api.telemetry_capture import capture_telemetry_events
from services.api.telemetry_schemas import TelemetryEventsRequest

router = APIRouter(prefix="/telemetry", tags=["Telemetry"])


@router.post("/events")
async def receive_events(request: TelemetryEventsRequest) -> dict[str, int]:
    """Validate and acknowledge a telemetry batch without persisting it."""
    capture_telemetry_events(request.events)

    return {"received": len(request.events)}
