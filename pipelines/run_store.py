"""Operational persistence for weekly publication runs.

Phase 2.2A only. This module stores run history and week-control rows inside
the caller's open PostgreSQL transaction. It does not publish KPIs, acquire a
lease, certify a report, or commit.

A later failed run must be written on a connection that will not be rolled back
with the rejected business transaction. Updating the last valid publication is
a separate statement so Phase 2.2B can run it in the same transaction as the
KPI UPSERT.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from pipelines.weekly_performance import POSTGRES_INTEGER_MAX, iso_week_bounds

WEEKLY_PIPELINE_NAME = "weekly_warehouse_client_performance"
RUNS_TABLE = "reporting.pipeline_publication_runs"
CONTROL_TABLE = "reporting.pipeline_publication_control"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_SUCCEEDED_NOOP = "succeeded_noop"
STATUS_FAILED_RETRYABLE = "failed_retryable"
STATUS_BLOCKED_QUALITY = "blocked_quality"
STATUS_SUPERSEDED = "superseded"
STATUS_SKIPPED_IN_PROGRESS = "skipped_in_progress"
RUN_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_SUCCEEDED,
        STATUS_SUCCEEDED_NOOP,
        STATUS_FAILED_RETRYABLE,
        STATUS_BLOCKED_QUALITY,
        STATUS_SUPERSEDED,
        STATUS_SKIPPED_IN_PROGRESS,
    }
)
RUN_TRANSITIONS = {
    STATUS_PENDING: frozenset(
        {
            STATUS_RUNNING,
            STATUS_SKIPPED_IN_PROGRESS,
            STATUS_SUPERSEDED,
            STATUS_BLOCKED_QUALITY,
            STATUS_FAILED_RETRYABLE,
        }
    ),
    STATUS_RUNNING: frozenset(
        {
            STATUS_SUCCEEDED,
            STATUS_SUCCEEDED_NOOP,
            STATUS_FAILED_RETRYABLE,
            STATUS_BLOCKED_QUALITY,
            STATUS_SUPERSEDED,
        }
    ),
}
TERMINAL_STATUSES = frozenset(
    status for status in RUN_STATUSES if status not in RUN_TRANSITIONS
)

COMMIT_NOT_ATTEMPTED = "not_attempted"
COMMIT_UNCOMMITTED = "uncommitted"
COMMIT_UNCERTAIN = "uncertain"
COMMIT_ROLLED_BACK = "rolled_back"
COMMIT_TECHNICALLY_OBSERVED = "technically_observed"
COMMIT_RESULTS = frozenset(
    {
        COMMIT_NOT_ATTEMPTED,
        COMMIT_UNCOMMITTED,
        COMMIT_UNCERTAIN,
        COMMIT_ROLLED_BACK,
        COMMIT_TECHNICALLY_OBSERVED,
    }
)

_SECRET_MARKERS = ("postgres://", "postgresql://", "bearer ", "password=", "api_key=")

_RUN_COLUMNS = """
    run_id, pipeline_name, target_week_start, trigger_type, started_at, finished_at,
    status, stage_reached, records_processed, aggregates_calculated, rows_inserted,
    rows_updated, rows_unchanged, error_code, error_message, block_reason,
    commit_result, prefect_flow_run_id, prefect_task_run_id
"""

CREATE_RUN_SQL = text(
    f"""
    /* op:create_run */
    INSERT INTO {RUNS_TABLE} (
        run_id, pipeline_name, target_week_start, trigger_type, status,
        started_at, stage_reached, commit_result, prefect_flow_run_id, prefect_task_run_id
    ) VALUES (
        :run_id, :pipeline_name, :target_week_start, :trigger_type, 'pending',
        COALESCE(:started_at, statement_timestamp()), :stage_reached, 'not_attempted',
        :prefect_flow_run_id, :prefect_task_run_id
    )
    RETURNING {_RUN_COLUMNS}
    """
)
SELECT_RUN_SQL = text(
    f"""
    /* op:select_run */
    SELECT {_RUN_COLUMNS}
    FROM {RUNS_TABLE}
    WHERE run_id = :run_id
    """
)
UPDATE_RUN_SQL = text(
    f"""
    /* op:update_run */
    UPDATE {RUNS_TABLE}
    SET status = :status,
        stage_reached = :stage_reached,
        finished_at = :finished_at,
        records_processed = :records_processed,
        aggregates_calculated = :aggregates_calculated,
        rows_inserted = :rows_inserted,
        rows_updated = :rows_updated,
        rows_unchanged = :rows_unchanged,
        error_code = :error_code,
        error_message = :error_message,
        block_reason = :block_reason,
        commit_result = :commit_result,
        prefect_flow_run_id = :prefect_flow_run_id,
        prefect_task_run_id = :prefect_task_run_id,
        updated_at = statement_timestamp()
    WHERE run_id = :run_id
      AND status = :expected_status
    RETURNING {_RUN_COLUMNS}
    """
)
CORRELATE_PENDING_RUN_SQL = text(
    f"""
    /* op:correlate_pending_run */
    UPDATE {RUNS_TABLE}
    SET prefect_flow_run_id = :prefect_flow_run_id,
        prefect_task_run_id = :prefect_task_run_id,
        stage_reached = 'prefect',
        updated_at = statement_timestamp()
    WHERE run_id = :run_id AND status = 'pending'
    RETURNING {_RUN_COLUMNS}
    """
)
LATEST_RUN_SQL = text(
    f"""
    /* op:latest_run */
    SELECT {_RUN_COLUMNS}
    FROM {RUNS_TABLE}
    WHERE pipeline_name = :pipeline_name
      AND (:target_week_start IS NULL OR target_week_start = :target_week_start)
    ORDER BY started_at DESC, run_id DESC
    LIMIT 1
    """
)
ENSURE_CONTROL_SQL = text(
    f"""
    /* op:ensure_control */
    INSERT INTO {CONTROL_TABLE} (week_start)
    VALUES (:week_start)
    ON CONFLICT (week_start) DO NOTHING
    """
)
SELECT_CONTROL_SQL = text(
    f"""
    /* op:select_control */
    SELECT week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token,
           last_valid_publication_run_id, last_valid_published_at, dependency_fingerprint,
           published_row_count, rules_version
    FROM {CONTROL_TABLE}
    WHERE week_start = :week_start
    """
)
RECORD_VALID_PUBLICATION_SQL = text(
    f"""
    /* op:record_valid_publication */
    UPDATE {CONTROL_TABLE}
    SET last_valid_publication_run_id = :run_id,
        last_valid_published_at = :published_at,
        published_row_count = :published_row_count,
        dependency_fingerprint = :dependency_fingerprint,
        rules_version = :rules_version,
        updated_at = statement_timestamp()
    WHERE week_start = :week_start
    RETURNING week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token,
              last_valid_publication_run_id, last_valid_published_at, dependency_fingerprint,
              published_row_count, rules_version
    """
)
RECORD_COMMIT_OBSERVATION_SQL = text(
    f"""
    /* op:record_commit_observation */
    UPDATE {RUNS_TABLE}
    SET commit_result = :commit_result,
        stage_reached = :stage_reached,
        error_code = :error_code,
        error_message = :error_message,
        updated_at = statement_timestamp()
    WHERE run_id = :run_id
      AND status = :expected_status
    RETURNING {_RUN_COLUMNS}
    """
)
LATEST_VALID_PUBLICATION_SQL = text(
    f"""
    /* op:latest_valid_publication */
    SELECT control.week_start,
           control.last_valid_publication_run_id AS run_id,
           control.last_valid_published_at AS published_at,
           control.published_row_count,
           control.dependency_fingerprint,
           control.rules_version,
           control.fencing_token
    FROM {CONTROL_TABLE} AS control
    WHERE control.week_start = :week_start
      AND control.last_valid_publication_run_id IS NOT NULL
    """
)

_SQL_STATEMENTS = (
    CREATE_RUN_SQL,
    SELECT_RUN_SQL,
    UPDATE_RUN_SQL,
    CORRELATE_PENDING_RUN_SQL,
    LATEST_RUN_SQL,
    ENSURE_CONTROL_SQL,
    SELECT_CONTROL_SQL,
    RECORD_VALID_PUBLICATION_SQL,
    RECORD_COMMIT_OBSERVATION_SQL,
    LATEST_VALID_PUBLICATION_SQL,
)


class RunStoreError(RuntimeError):
    """Identifiable operational-store failure. Not a business publication."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RunRecord:
    """One immutable history row. Status changes update that row only."""

    run_id: UUID
    pipeline_name: str
    target_week_start: date
    trigger_type: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    stage_reached: str | None
    records_processed: int | None
    aggregates_calculated: int | None
    rows_inserted: int | None
    rows_updated: int | None
    rows_unchanged: int | None
    error_code: str | None
    error_message: str | None
    block_reason: str | None
    commit_result: str
    prefect_flow_run_id: str | None = None
    prefect_task_run_id: str | None = None

    @property
    def business_certified(self) -> bool:
        return False


@dataclass(frozen=True)
class WeekControl:
    """Stored week-control row. A lease column is not a held lease."""

    week_start: date
    owner_run_id: UUID | None
    lease_acquired_at: datetime | None
    lease_expires_at: datetime | None
    fencing_token: int
    last_valid_publication_run_id: UUID | None
    last_valid_published_at: datetime | None
    dependency_fingerprint: str | None
    published_row_count: int | None
    rules_version: str | None

    @property
    def concurrency_guaranteed(self) -> bool:
        return False

    @property
    def business_certified(self) -> bool:
        return False


@dataclass(frozen=True)
class ValidPublication:
    """Pointer to the last stored valid publication. Not a new certification."""

    week_start: date
    run_id: UUID
    published_at: datetime
    published_row_count: int
    dependency_fingerprint: str | None
    rules_version: str | None
    fencing_token: int

    @property
    def business_certified(self) -> bool:
        return False


def create_run(
    connection: Any,
    *,
    target_week_start: date,
    trigger_type: str,
    pipeline_name: str = WEEKLY_PIPELINE_NAME,
    run_id: UUID | None = None,
    started_at: datetime | None = None,
    stage_reached: str | None = None,
    prefect_flow_run_id: str | None = None,
    prefect_task_run_id: str | None = None,
) -> RunRecord:
    """Insert a pending run. Does not create or replace a valid publication."""
    _require_transaction(connection)
    week = _week(target_week_start)
    identity = uuid4() if run_id is None else _uuid(run_id, "run_id")
    parameters = {
        "run_id": identity,
        "pipeline_name": _label(pipeline_name, "pipeline_name"),
        "target_week_start": week,
        "trigger_type": _label(trigger_type, "trigger_type"),
        "started_at": _optional_time(started_at, "started_at"),
        "stage_reached": _optional_label(stage_reached, "stage_reached"),
        "prefect_flow_run_id": _optional_label(prefect_flow_run_id, "prefect_flow_run_id"),
        "prefect_task_run_id": _optional_label(prefect_task_run_id, "prefect_task_run_id"),
    }
    try:
        rows = _rows(connection, CREATE_RUN_SQL, parameters)
    except IntegrityError as exc:
        raise RunStoreError("duplicate_run_id", "run_id already exists") from exc
    return _run(rows[0] if rows else _missing("create_run"))


def update_run(
    connection: Any,
    run_id: UUID,
    *,
    expected_status: str,
    status: str,
    finished_at: datetime | None = None,
    stage_reached: str | None = None,
    records_processed: int | None = None,
    aggregates_calculated: int | None = None,
    rows_inserted: int | None = None,
    rows_updated: int | None = None,
    rows_unchanged: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    block_reason: str | None = None,
    commit_result: str | None = None,
    prefect_flow_run_id: str | None = None,
    prefect_task_run_id: str | None = None,
) -> RunRecord:
    """Update one run row. Terminal history cannot be replaced."""
    _require_transaction(connection)
    identity = _uuid(run_id, "run_id")
    current = _require_run(connection, identity)
    if current.status != expected_status:
        raise RunStoreError("status_conflict", "run status no longer matches expected_status")
    _transition(expected_status, status)
    if status in TERMINAL_STATUSES and finished_at is None:
        raise RunStoreError("finished_at_required", "a terminal run requires finished_at")
    if status == STATUS_RUNNING and finished_at is not None:
        raise RunStoreError("finished_at_forbidden", "running runs cannot be finished")
    if finished_at is not None and finished_at < current.started_at:
        raise RunStoreError("invalid_timestamp", "finished_at cannot precede started_at")
    if status == STATUS_BLOCKED_QUALITY and not block_reason:
        raise RunStoreError("block_reason_required", "blocked_quality requires block_reason")
    if status == STATUS_FAILED_RETRYABLE and not error_code:
        raise RunStoreError("error_code_required", "failed_retryable requires error_code")
    chosen_commit = current.commit_result if commit_result is None else commit_result
    if chosen_commit not in COMMIT_RESULTS:
        raise RunStoreError("invalid_commit_result", "commit_result is not an operational result")
    parameters = {
        "run_id": identity,
        "expected_status": expected_status,
        "status": status,
        "stage_reached": _optional_label(stage_reached, "stage_reached") or current.stage_reached,
        "finished_at": _optional_time(finished_at, "finished_at"),
        "records_processed": _count(records_processed, "records_processed"),
        "aggregates_calculated": _count(aggregates_calculated, "aggregates_calculated"),
        "rows_inserted": _count(rows_inserted, "rows_inserted"),
        "rows_updated": _count(rows_updated, "rows_updated"),
        "rows_unchanged": _count(rows_unchanged, "rows_unchanged"),
        "error_code": _optional_label(error_code, "error_code"),
        "error_message": _optional_text(error_message, "error_message"),
        "block_reason": _optional_text(block_reason, "block_reason"),
        "commit_result": chosen_commit,
        "prefect_flow_run_id": _optional_label(
            prefect_flow_run_id, "prefect_flow_run_id"
        ) or current.prefect_flow_run_id,
        "prefect_task_run_id": _optional_label(
            prefect_task_run_id, "prefect_task_run_id"
        ) or current.prefect_task_run_id,
    }
    rows = _rows(connection, UPDATE_RUN_SQL, parameters)
    if not rows:
        raise RunStoreError("status_conflict", "run status changed before the update")
    return _run(rows[0])


def correlate_pending_run(
    connection: Any,
    run_id: UUID,
    *,
    prefect_flow_run_id: str,
    prefect_task_run_id: str,
) -> RunRecord:
    """Attach Prefect identities to one durably accepted pending run."""
    _require_transaction(connection)
    rows = _rows(
        connection,
        CORRELATE_PENDING_RUN_SQL,
        {
            "run_id": _uuid(run_id, "run_id"),
            "prefect_flow_run_id": _label(prefect_flow_run_id, "prefect_flow_run_id"),
            "prefect_task_run_id": _label(prefect_task_run_id, "prefect_task_run_id"),
        },
    )
    if not rows:
        raise RunStoreError("status_conflict", "accepted run is no longer pending")
    return _run(rows[0])


def get_run(connection: Any, run_id: UUID) -> RunRecord | None:
    """Read one run. Absence is not a publication and does not create history."""
    _require_transaction(connection)
    rows = _rows(connection, SELECT_RUN_SQL, {"run_id": _uuid(run_id, "run_id")})
    if not rows:
        return None
    return _run(rows[0])


def latest_run(
    connection: Any,
    *,
    pipeline_name: str = WEEKLY_PIPELINE_NAME,
    target_week_start: date | None = None,
) -> RunRecord | None:
    """Return the newest stored run, not the last valid publication."""
    _require_transaction(connection)
    week = None if target_week_start is None else _week(target_week_start)
    rows = _rows(
        connection,
        LATEST_RUN_SQL,
        {
            "pipeline_name": _label(pipeline_name, "pipeline_name"),
            "target_week_start": week,
        },
    )
    if not rows:
        return None
    return _run(rows[0])


def latest_valid_publication(connection: Any, target_week_start: date) -> ValidPublication | None:
    """Read the stored valid publication pointer. Does not inspect the latest run."""
    _require_transaction(connection)
    rows = _rows(
        connection,
        LATEST_VALID_PUBLICATION_SQL,
        {"week_start": _week(target_week_start)},
    )
    if not rows:
        return None
    row = rows[0]
    return ValidPublication(
        week_start=row["week_start"],
        run_id=_uuid(row["run_id"], "run_id"),
        published_at=_required_time(row["published_at"], "published_at"),
        published_row_count=_count(row["published_row_count"], "published_row_count") or 0,
        dependency_fingerprint=row["dependency_fingerprint"],
        rules_version=row["rules_version"],
        fencing_token=_token(row["fencing_token"]),
    )


def get_week_control(connection: Any, week_start: date) -> WeekControl | None:
    """Read one week-control row. Absence is not an empty certified week."""
    _require_transaction(connection)
    rows = _rows(connection, SELECT_CONTROL_SQL, {"week_start": _week(week_start)})
    if not rows:
        return None
    return _control(rows[0])


def ensure_week_control(connection: Any, week_start: date) -> WeekControl:
    """Insert the week-control row when absent. Does not acquire a lease."""
    _require_transaction(connection)
    week = _week(week_start)
    _rows(connection, ENSURE_CONTROL_SQL, {"week_start": week})
    control = get_week_control(connection, week)
    if control is None:
        raise RunStoreError("control_not_found", "week control could not be read after insert")
    return control


def record_valid_publication(
    connection: Any,
    week_start: date,
    run_id: UUID,
    *,
    published_at: datetime,
    published_row_count: int,
    dependency_fingerprint: str | None = None,
    rules_version: str | None = None,
) -> WeekControl:
    """Point the week at a valid publication without changing run history or committing."""
    _require_transaction(connection)
    week = _week(week_start)
    identity = _uuid(run_id, "run_id")
    run = _require_run(connection, identity)
    if run.target_week_start != week:
        raise RunStoreError("week_mismatch", "run target week does not match the control week")
    ensure_week_control(connection, week)
    rows = _rows(
        connection,
        RECORD_VALID_PUBLICATION_SQL,
        {
            "week_start": week,
            "run_id": identity,
            "published_at": _required_time(published_at, "published_at"),
            "published_row_count": _count(published_row_count, "published_row_count"),
            "dependency_fingerprint": _optional_text(dependency_fingerprint, "dependency_fingerprint"),
            "rules_version": _optional_label(rules_version, "rules_version"),
        },
    )
    if not rows:
        raise RunStoreError("control_not_found", "week control was not updated")
    return _control(rows[0])


def record_commit_observation(
    connection: Any,
    run_id: UUID,
    *,
    commit_result: str,
    stage_reached: str,
    error_code: str,
    error_message: str,
) -> RunRecord:
    """Record an unresolved commit without declaring success or failure.

    The run stays ``running``. This does not change a lease, a fencing token,
    or the last valid publication, and it does not commit.
    """
    _require_transaction(connection)
    if commit_result not in {COMMIT_UNCERTAIN, COMMIT_UNCOMMITTED, COMMIT_ROLLED_BACK}:
        raise RunStoreError(
            "invalid_commit_result",
            "an unresolved observation cannot be recorded as a successful commit",
        )
    identity = _uuid(run_id, "run_id")
    current = _require_run(connection, identity)
    if current.status != STATUS_RUNNING:
        raise RunStoreError("status_conflict", "only a running run can record an unresolved commit")
    rows = _rows(
        connection,
        RECORD_COMMIT_OBSERVATION_SQL,
        {
            "run_id": identity,
            "expected_status": STATUS_RUNNING,
            "commit_result": commit_result,
            "stage_reached": _label(stage_reached, "stage_reached"),
            "error_code": _label(error_code, "error_code"),
            "error_message": _optional_text(error_message, "error_message"),
        },
    )
    if not rows:
        raise RunStoreError("status_conflict", "run status changed before the commit observation")
    observed = _run(rows[0])
    if observed.status != STATUS_RUNNING or observed.commit_result != commit_result:
        raise RunStoreError("write_error", "commit observation changed the run outcome")
    return observed


def record_fencing_advance(
    connection: Any,
    week_start: date,
    *,
    expected_fencing_token: int,
    owner_run_id: UUID | None = None,
    lease_acquired_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> None:
    """Reject every lease or fencing mutation from the run store.

    Ownership and token changes have one protected path: acquire_week_lease.
    Matching an expected token is not authorization to replace the owner.
    """
    del connection, week_start, expected_fencing_token, owner_run_id, lease_acquired_at, lease_expires_at
    raise RunStoreError(
        "lease_authority_forbidden",
        "lease ownership and fencing changes must go through acquire_week_lease",
    )


def _require_transaction(connection: Any) -> None:
    if connection is None or not hasattr(connection, "execute"):
        raise RunStoreError("transaction_required", "an open PostgreSQL connection is required")
    options = {}
    getter = getattr(connection, "get_execution_options", None)
    if callable(getter):
        loaded = getter() or {}
        if isinstance(loaded, Mapping):
            options = loaded
    if str(options.get("isolation_level", "")).upper() == "AUTOCOMMIT":
        raise RunStoreError("autocommit_forbidden", "autocommit cannot store a weekly control atomically")
    dialect_name = getattr(getattr(connection, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        raise RunStoreError("postgresql_required", "operational persistence requires PostgreSQL")
    in_transaction = getattr(connection, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        raise RunStoreError("transaction_required", "an open transaction is required")


def _rows(connection: Any, statement: Any, parameters: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    _assert_statement(statement)
    try:
        result = connection.execute(statement, dict(parameters))
    except IntegrityError:
        raise
    except SQLAlchemyError as exc:
        raise RunStoreError("write_error", str(exc)) from exc
    if result is None:
        return []
    if getattr(result, "returns_rows", True) is False:
        return []
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return [dict(row) for row in mappings().all()]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return [dict(row) for row in result]
    return []


def _require_run(connection: Any, run_id: UUID) -> RunRecord:
    rows = _rows(connection, SELECT_RUN_SQL, {"run_id": run_id})
    if not rows:
        raise RunStoreError("run_not_found", "run_id was not found")
    return _run(rows[0])


def _transition(current: str, proposed: str) -> None:
    if proposed not in RUN_STATUSES:
        raise RunStoreError("invalid_status", "status is not a publication-run status")
    allowed = RUN_TRANSITIONS.get(current, frozenset())
    if proposed not in allowed:
        raise RunStoreError("invalid_transition", f"{current} cannot transition to {proposed}")


def _run(row: Mapping[str, Any]) -> RunRecord:
    return RunRecord(
        run_id=_uuid(row["run_id"], "run_id"),
        pipeline_name=row["pipeline_name"],
        target_week_start=row["target_week_start"],
        trigger_type=row["trigger_type"],
        status=row["status"],
        started_at=_required_time(row["started_at"], "started_at"),
        finished_at=_optional_time(row["finished_at"], "finished_at"),
        stage_reached=row["stage_reached"],
        records_processed=row["records_processed"],
        aggregates_calculated=row["aggregates_calculated"],
        rows_inserted=row["rows_inserted"],
        rows_updated=row["rows_updated"],
        rows_unchanged=row["rows_unchanged"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        block_reason=row["block_reason"],
        commit_result=row["commit_result"],
        prefect_flow_run_id=row["prefect_flow_run_id"],
        prefect_task_run_id=row["prefect_task_run_id"],
    )


def _control(row: Mapping[str, Any]) -> WeekControl:
    return WeekControl(
        week_start=row["week_start"],
        owner_run_id=None if row["owner_run_id"] is None else _uuid(row["owner_run_id"], "owner_run_id"),
        lease_acquired_at=_optional_time(row["lease_acquired_at"], "lease_acquired_at"),
        lease_expires_at=_optional_time(row["lease_expires_at"], "lease_expires_at"),
        fencing_token=_token(row["fencing_token"]),
        last_valid_publication_run_id=(
            None
            if row["last_valid_publication_run_id"] is None
            else _uuid(row["last_valid_publication_run_id"], "last_valid_publication_run_id")
        ),
        last_valid_published_at=_optional_time(row["last_valid_published_at"], "last_valid_published_at"),
        dependency_fingerprint=row["dependency_fingerprint"],
        published_row_count=row["published_row_count"],
        rules_version=row["rules_version"],
    )


def _week(value: date) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise RunStoreError("invalid_week_start", "week_start must be an ISO Monday date")
    try:
        iso_week_bounds(value)
    except ValueError as exc:
        raise RunStoreError("invalid_week_start", str(exc)) from exc
    return value


def _uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RunStoreError("invalid_identifier", f"{field} must be a UUID") from exc


def _label(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RunStoreError("invalid_text", f"{field} must be a short non-empty string")
    _reject_secret(value, field)
    return value


def _optional_label(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _label(value, field)


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise RunStoreError("invalid_text", f"{field} must be short descriptive text")
    _reject_secret(value, field)
    return value


def _reject_secret(value: str, field: str) -> None:
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise RunStoreError("secret_rejected", f"{field} must not contain credentials")


def _optional_time(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    return _required_time(value, field)


def _required_time(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RunStoreError("invalid_timestamp", f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _count(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > POSTGRES_INTEGER_MAX:
        raise RunStoreError("invalid_count", f"{field} must be a non-negative PostgreSQL integer")
    return value


def _token(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise RunStoreError("invalid_fencing_token", "fencing_token must be a non-negative bigint")
    return value


def _missing(operation: str) -> Mapping[str, Any]:
    raise RunStoreError("write_error", f"{operation} did not return a row")


def _assert_statement(statement: Any) -> None:
    sql = str(statement).upper()
    if "TELEMETRY_EVENTS" in sql or "DELETE " in sql or " DROP " in sql:
        raise RunStoreError("forbidden_sql", "operational SQL must not delete rows or write telemetry")
    if "WEEKLY_WAREHOUSE_CLIENT_PERFORMANCE" in sql:
        raise RunStoreError("forbidden_sql", "operational SQL must not write the business KPI table")


def _assert_module_sql() -> None:
    for statement in _SQL_STATEMENTS:
        sql = str(statement)
        _assert_statement(statement)
        if "'" in sql.split("/*", 1)[0]:
            raise RunStoreError("forbidden_sql", "values must be bound parameters, not SQL literals")
        if sql.count(":") < 1 and "ENSURE" not in sql.upper():
            raise RunStoreError("forbidden_sql", "operational statements must be parameterized")


_assert_module_sql()
