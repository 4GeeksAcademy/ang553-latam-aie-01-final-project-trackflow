from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class WeeklyPerformanceEntry(BaseModel):
    warehouse: str
    client_id: str
    inbound_units_count: int
    outbound_orders_count: int
    stockout_events_count: int
    discrepancy_events_count: int
    discrepancy_rate: Decimal

    @field_serializer("discrepancy_rate", when_used="json")
    def serialize_rate(self, value: Decimal) -> int | float:
        return int(value) if value == value.to_integral_value() else float(value)


class WeeklyPerformanceResponse(BaseModel):
    week_start: date
    entries: list[WeeklyPerformanceEntry]


class PipelineRunResponse(BaseModel):
    run_id: UUID
    week_start: date
    trigger_type: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    records_processed: int | None
    aggregates_calculated: int | None
    rows_inserted: int | None
    rows_updated: int | None
    rows_unchanged: int | None
    error_code: str | None
    error_message: str | None
    block_reason: str | None
    commit_result: str
    prefect_flow_run_id: str | None
    prefect_task_run_id: str | None


class PipelineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    week_start: date | None = Field(
        default=None,
        description="ISO Monday for a fully closed UTC week; defaults to the latest closed week.",
    )


class PipelineRunAcceptedResponse(BaseModel):
    run_id: UUID
    week_start: date
    status: str = "accepted"
    published: bool = False
