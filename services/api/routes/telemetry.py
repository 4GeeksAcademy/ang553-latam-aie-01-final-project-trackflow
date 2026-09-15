"""Temporary telemetry ingestion endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from services.api.telemetry_schemas import TelemetryEventsRequest

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    telemetry_handler = logging.StreamHandler()
    telemetry_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(telemetry_handler)
logger.propagate = False

router = APIRouter(prefix="/telemetry", tags=["Telemetry"])


@router.post("/events")
async def receive_events(request: TelemetryEventsRequest) -> dict[str, int]:
    """Validate and acknowledge a telemetry batch without persisting it."""
    logger.info("Received telemetry batch with %d events", len(request.events))
    for event in request.events:
        logger.info("Received telemetry event_type=%s", event.event_type)

    return {"received": len(request.events)}
