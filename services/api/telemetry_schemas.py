"""Pydantic schemas for the telemetry ingestion endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelemetryEvent(BaseModel):
    """The eight-field telemetry event envelope.

    Detailed validation of ``properties`` by ``event_type`` belongs to a later
    phase.  This model intentionally validates only the shared envelope.
    """

    model_config = ConfigDict(extra="forbid")

    eventId: UUID = Field(..., description="UUID version 4 identifying the event")
    timestamp: datetime
    sessionId: str | None = Field(..., min_length=1)
    userId: str | None = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    schemaVersion: str = Field(..., pattern=r"^1\.0$")
    requestId: str | None = Field(..., min_length=1)
    properties: dict[str, Any]

    @field_validator("eventId")
    @classmethod
    def validate_event_id_version(cls, value: UUID) -> UUID:
        """Require the UUID version specified by the telemetry contract."""
        if value.version != 4:
            raise ValueError("eventId must be a UUID v4")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_utc_z_timestamp(cls, value: Any) -> Any:
        """Require ISO 8601 timestamps represented in UTC with a trailing Z."""
        if isinstance(value, str) and not value.endswith("Z"):
            raise ValueError("timestamp must be represented in UTC with a trailing Z")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp_utc(cls, value: datetime) -> datetime:
        """Reject naive or non-UTC datetimes."""
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("timestamp must be UTC")
        return value

    @field_validator("event_type")
    @classmethod
    def validate_event_type_not_blank(cls, value: str) -> str:
        """Reject whitespace-only event types without enumerating event names."""
        if not value.strip():
            raise ValueError("event_type must not be blank")
        return value


class TelemetryEventsRequest(BaseModel):
    """Batch body whose event items are validated by the route individually."""

    model_config = ConfigDict(extra="forbid")

    events: list[Any]
