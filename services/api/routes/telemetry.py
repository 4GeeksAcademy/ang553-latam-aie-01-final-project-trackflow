"""Telemetry ingestion and cached operational reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlmodel import Session

from services.api.cache import TTLCache
from services.api.database import get_db
from services.api.telemetry_schemas import TelemetryEventsRequest
from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_storage import bulk_insert_telemetry, map_event_to_row
from services.telemetry.analysis import build_telemetry_report

router = APIRouter(prefix="/telemetry", tags=["Telemetry"])

report_cache: TTLCache[dict[str, Any]] = TTLCache(ttl_seconds=60)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/report")
def get_telemetry_report(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Report over [start_date, end_date), cached for 60 seconds per process.

    Omit both dates for the last seven days; the default period remains fixed
    on cache hits. Explicit dates must both have offsets and are keyed in UTC.
    """
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=422,
            detail="start_date and end_date must be provided together",
        )

    if start_date is not None and end_date is not None:
        if start_date.utcoffset() is None or end_date.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail="start_date and end_date must be timezone-aware",
            )
        start_date = start_date.astimezone(timezone.utc)
        end_date = end_date.astimezone(timezone.utc)
        if start_date >= end_date:
            raise HTTPException(
                status_code=422,
                detail="start_date must be earlier than end_date",
            )
        cache_key = (
            f"telemetry_report:v1:{start_date.isoformat()}:{end_date.isoformat()}"
        )
    else:
        cache_key = "telemetry_report:v1:default"

    cached = report_cache.get(cache_key)
    if cached is not None:
        return cached

    if start_date is None or end_date is None:
        end_date = _utc_now()
        start_date = end_date - timedelta(days=7)

    report = build_telemetry_report(session, start_date, end_date)
    report_cache.set(cache_key, report)
    return report


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
