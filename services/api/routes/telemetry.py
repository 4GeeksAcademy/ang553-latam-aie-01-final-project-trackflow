"""Telemetry ingestion endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlmodel import Session

from services.api.database import get_db
from services.api.telemetry_schemas import TelemetryEventsRequest
from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_storage import bulk_insert_telemetry, map_event_to_row

router = APIRouter(prefix="/telemetry", tags=["Telemetry"])


@router.post("/events")
async def receive_events(
    request: TelemetryEventsRequest,
    session: Session = Depends(get_db),
) -> dict[str, int]:
    """Validate events individually and persist valid rows in one bulk call."""
    received = len(request.events)
    rejected = 0
    valid_rows = []

    for raw in request.events:
        try:
            event = TelemetryEvent.model_validate(raw)
        except ValidationError:
            rejected += 1
            continue

        valid_rows.append(map_event_to_row(event))

    if valid_rows:
        bulk_insert_telemetry(session, valid_rows)

    return {
        "received": received,
        "stored": len(valid_rows),
        "rejected": rejected,
    }
