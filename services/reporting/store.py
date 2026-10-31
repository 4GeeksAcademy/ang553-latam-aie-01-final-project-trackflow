from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from pipelines.run_store import create_run, update_run

LATEST_VALID_WEEK_SQL = text(
    """SELECT week_start FROM reporting.pipeline_publication_control
       WHERE last_valid_publication_run_id IS NOT NULL
       ORDER BY week_start DESC LIMIT 1"""
)
VALID_WEEK_SQL = text(
    """SELECT week_start FROM reporting.pipeline_publication_control
       WHERE week_start = :week_start AND last_valid_publication_run_id IS NOT NULL"""
)
WEEKLY_ROWS_SQL = text(
    """SELECT warehouse, client_id, inbound_units_count, outbound_orders_count,
              stockout_events_count, discrepancy_events_count, discrepancy_rate
       FROM reporting.weekly_warehouse_client_performance
       WHERE week_start = :week_start ORDER BY warehouse, client_id"""
)
LATEST_RUN_SQL = text(
    """SELECT run_id, target_week_start AS week_start, trigger_type, status,
              started_at, finished_at, records_processed, aggregates_calculated,
              rows_inserted, rows_updated, rows_unchanged, error_code, error_message,
              block_reason, commit_result, prefect_flow_run_id, prefect_task_run_id
       FROM reporting.pipeline_publication_runs
       ORDER BY started_at DESC, run_id DESC LIMIT 1"""
)
ACTIVE_RUN_SQL = text(
    """SELECT run_id FROM reporting.pipeline_publication_runs
       WHERE target_week_start = :week_start AND status IN ('pending', 'running')
       ORDER BY started_at DESC LIMIT 1"""
)
WEEK_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))")


def weekly_report(connection: Any, week_start: date | None) -> tuple[date, list[dict]] | None:
    selected = week_start
    if selected is None:
        row = connection.execute(LATEST_VALID_WEEK_SQL).mappings().first()
        if row is None:
            return None
        selected = row["week_start"]
    elif connection.execute(VALID_WEEK_SQL, {"week_start": selected}).first() is None:
        return None
    rows = connection.execute(WEEKLY_ROWS_SQL, {"week_start": selected}).mappings().all()
    return selected, [dict(row) for row in rows]


def latest_pipeline_run(connection: Any) -> dict | None:
    row = connection.execute(LATEST_RUN_SQL).mappings().first()
    return None if row is None else dict(row)


def accept_pipeline_run(connection: Any, week_start: date) -> tuple[UUID, bool]:
    connection.execute(WEEK_LOCK_SQL, {"lock_key": f"trackflow-reporting:{week_start.isoformat()}"})
    existing = connection.execute(ACTIVE_RUN_SQL, {"week_start": week_start}).scalar_one_or_none()
    if existing is not None:
        return existing, False
    record = create_run(
        connection,
        target_week_start=week_start,
        trigger_type="http",
        stage_reached="accepted",
    )
    return record.run_id, True


def mark_scheduling_failed(connection: Any, run_id: UUID, now: datetime) -> None:
    update_run(
        connection,
        run_id,
        expected_status="pending",
        status="failed_retryable",
        finished_at=now,
        stage_reached="scheduling",
        error_code="scheduling_failed",
        error_message="scheduler rejected the request before enqueue",
    )
