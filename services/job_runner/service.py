"""Transactional state control for TrackFlow background jobs.

The two partial unique indexes in ``infra/sql/job_runs.sql`` are the only
ownership mechanism.  This module does not use advisory locks, leases, expiry,
or automatic takeover.  A caller owns a run only when ``acquire_job_run``
returns ``acquired`` after its transaction commit succeeds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import re
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

_LOGGER = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
JOB_RUN_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_PROCESSING, STATUS_COMPLETED, STATUS_FAILED}
)
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})
VALID_STATUS_TRANSITIONS = frozenset(
    {
        (STATUS_PENDING, STATUS_PROCESSING),
        (STATUS_PROCESSING, STATUS_COMPLETED),
        (STATUS_PROCESSING, STATUS_FAILED),
    }
)

ACQUIRED = "acquired"
SKIPPED_PROCESSING = "skipped_processing"
SKIPPED_COMPLETED = "skipped_completed"
SKIPPED_CONFLICT = "skipped_conflict"
AcquisitionStatus = Literal[
    "acquired", "skipped_processing", "skipped_completed", "skipped_conflict"
]

TABLE_NAME = "job_runs"
PROCESSING_CONSTRAINT = "job_runs_one_processing_per_job_idx"
TARGET_CONSTRAINT = "job_runs_target_claim_idx"
EXPECTED_ACQUISITION_CONSTRAINTS = frozenset(
    {PROCESSING_CONSTRAINT, TARGET_CONSTRAINT}
)
MAX_JOB_NAME_LENGTH = 100
MAX_ERROR_MESSAGE_LENGTH = 500

_SENSITIVE_ERROR_HINT = re.compile(
    r"(?i)(?:"
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)"
    r"(?:\+[a-z0-9_]+)?://|"
    r"\bbearer\s+\S+|"
    r"[\"']?(?:password|passwd|token|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|secret|authorization)[\"']?\s*[:=]"
    r")"
)
_WHITESPACE = re.compile(r"\s+")

_RUN_COLUMNS = """
    id, job_name, target_date, status, started_at, finished_at,
    error_message, created_at
"""

_INSERT_PENDING = text(
    f"""
    INSERT INTO {TABLE_NAME} (id, job_name, target_date, status, created_at)
    VALUES (:id, :job_name, :target_date, 'pending', :created_at)
    RETURNING {_RUN_COLUMNS}
    """
)
_START_PROCESSING = text(
    f"""
    UPDATE {TABLE_NAME}
    SET status = 'processing', started_at = :started_at
    WHERE id = :id AND status = 'pending'
    RETURNING {_RUN_COLUMNS}
    """
)
_SELECT_BY_ID = text(
    f"SELECT {_RUN_COLUMNS} FROM {TABLE_NAME} WHERE id = :id"
)
_SELECT_LATEST_BY_KEY = text(
    f"""
    SELECT {_RUN_COLUMNS}
    FROM {TABLE_NAME}
    WHERE job_name = :job_name AND target_date = :target_date
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    """
)
_SELECT_COMPLETED_BY_KEY = text(
    f"""
    SELECT {_RUN_COLUMNS}
    FROM {TABLE_NAME}
    WHERE job_name = :job_name
      AND target_date = :target_date
      AND status = 'completed'
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    """
)
_SELECT_PROCESSING_BY_JOB = text(
    f"""
    SELECT {_RUN_COLUMNS}
    FROM {TABLE_NAME}
    WHERE job_name = :job_name AND status = 'processing'
    ORDER BY created_at, id
    LIMIT 1
    """
)
_COMPLETE_RUN = text(
    f"""
    UPDATE {TABLE_NAME}
    SET status = 'completed',
        finished_at = GREATEST(:finished_at, started_at),
        error_message = NULL
    WHERE id = :id AND status = 'processing'
    RETURNING {_RUN_COLUMNS}
    """
)
_FAIL_RUN = text(
    f"""
    UPDATE {TABLE_NAME}
    SET status = 'failed',
        finished_at = GREATEST(:finished_at, started_at),
        error_message = :error_message
    WHERE id = :id AND status = 'processing'
    RETURNING {_RUN_COLUMNS}
    """
)


class JobRunError(RuntimeError):
    """Base error for the DEV-53 execution-control service."""


class JobRunValidationError(JobRunError):
    """Caller supplied a value outside the job-run contract."""


class JobRunPersistenceError(JobRunError):
    """A non-routine database failure prevented a trustworthy result."""


@dataclass(frozen=True)
class JobRunRecord:
    id: UUID
    job_name: str
    target_date: date
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime


@dataclass(frozen=True)
class AcquisitionResult:
    status: AcquisitionStatus
    run: JobRunRecord

    @property
    def acquired(self) -> bool:
        return self.status == ACQUIRED


@dataclass(frozen=True)
class TransitionResult:
    changed: bool
    run: JobRunRecord


def acquire_job_run(
    engine: Any,
    job_name: str,
    target_date: date,
    *,
    now: datetime | None = None,
) -> AcquisitionResult:
    """Atomically create ``pending`` and transition it to ``processing``.

    Expected uniqueness conflicts are rolled back before the winning state is
    read on a new connection.  The returned run may be used for work only when
    ``acquired`` is true.
    """
    name = _job_name(job_name)
    target = _target_date(target_date)
    started_at = _utc_time(now)
    run_id = uuid4()
    connection = None
    transaction = None
    commit_confirmed = False
    try:
        connection = engine.connect()
        transaction = connection.begin()
        _one(
            connection.execute(
                _INSERT_PENDING,
                {
                    "id": run_id,
                    "job_name": name,
                    "target_date": target,
                    "created_at": started_at,
                },
            ),
            "pending insert returned no run",
        )
        started = _one(
            connection.execute(
                _START_PROCESSING,
                {"id": run_id, "started_at": started_at},
            ),
            "pending run could not transition to processing",
        )
        transaction.commit()
        commit_confirmed = True
        return AcquisitionResult(ACQUIRED, _record(started))
    except IntegrityError as exc:
        _rollback_after_acquisition_failure(transaction)
        if not _expected_conflict(exc):
            raise JobRunPersistenceError(
                "job-run acquisition violated an unexpected constraint"
            ) from exc
    except JobRunPersistenceError:
        _rollback_after_acquisition_failure(transaction)
        raise
    except SQLAlchemyError as exc:
        _rollback_after_acquisition_failure(transaction)
        raise JobRunPersistenceError("job-run acquisition failed") from exc
    finally:
        if connection is not None:
            _close_acquisition_connection(
                connection,
                commit_confirmed=commit_confirmed,
            )

    return _read_conflict(engine, name, target)


def complete_job_run(
    engine: Any,
    run_id: UUID,
    *,
    now: datetime | None = None,
) -> TransitionResult:
    """Compare-and-set one processing run to completed."""
    return _terminal_transition(
        engine,
        _run_id(run_id),
        STATUS_COMPLETED,
        _utc_time(now),
        None,
    )


def fail_job_run(
    engine: Any,
    run_id: UUID,
    error: BaseException | str,
    *,
    now: datetime | None = None,
) -> TransitionResult:
    """Compare-and-set one processing run to failed with a safe diagnostic."""
    return _terminal_transition(
        engine,
        _run_id(run_id),
        STATUS_FAILED,
        _utc_time(now),
        sanitize_error_message(error),
    )


def get_job_run(engine: Any, run_id: UUID) -> JobRunRecord | None:
    """Read one execution by its stable identifier."""
    identity = _run_id(run_id)
    try:
        with engine.connect() as connection:
            row = connection.execute(_SELECT_BY_ID, {"id": identity}).mappings().first()
    except SQLAlchemyError as exc:
        raise JobRunPersistenceError("job-run lookup failed") from exc
    return None if row is None else _record(row)


def get_latest_job_run(
    engine: Any,
    job_name: str,
    target_date: date,
) -> JobRunRecord | None:
    """Read the most recent attempt for one functional job/date key."""
    parameters = {
        "job_name": _job_name(job_name),
        "target_date": _target_date(target_date),
    }
    try:
        with engine.connect() as connection:
            row = connection.execute(_SELECT_LATEST_BY_KEY, parameters).mappings().first()
    except SQLAlchemyError as exc:
        raise JobRunPersistenceError("job-run key lookup failed") from exc
    return None if row is None else _record(row)


def sanitize_error_message(error: BaseException | str) -> str:
    """Return a conservative diagnostic suitable for persistence.

    Known credential-bearing forms are replaced wholesale rather than trying
    to preserve their surrounding values. This reduces exposure risk but does
    not claim that arbitrary, unlabelled secrets can be detected reliably.
    """
    raw_message = str(error)
    if _SENSITIVE_ERROR_HINT.search(raw_message):
        error_kind = type(error).__name__ if isinstance(error, BaseException) else "job"
        return f"{error_kind} failed; sensitive details redacted"
    message = raw_message
    message = _WHITESPACE.sub(" ", message).strip()
    if not message:
        message = "background job failed"
    return message[:MAX_ERROR_MESSAGE_LENGTH]


def is_valid_status_transition(current_status: str, next_status: str) -> bool:
    """Return whether the explicit DEV-53 state machine permits a transition."""
    if current_status not in JOB_RUN_STATUSES or next_status not in JOB_RUN_STATUSES:
        return False
    return (current_status, next_status) in VALID_STATUS_TRANSITIONS


def _terminal_transition(
    engine: Any,
    run_id: UUID,
    terminal_status: str,
    finished_at: datetime,
    error_message: str | None,
) -> TransitionResult:
    if terminal_status not in TERMINAL_STATUSES:
        raise JobRunValidationError("terminal status must be completed or failed")
    statement = _COMPLETE_RUN if terminal_status == STATUS_COMPLETED else _FAIL_RUN
    parameters = {"id": run_id, "finished_at": finished_at}
    if terminal_status == STATUS_FAILED:
        parameters["error_message"] = error_message
    try:
        with engine.begin() as connection:
            row = connection.execute(statement, parameters).mappings().first()
            if row is not None:
                return TransitionResult(True, _record(row))
            current = connection.execute(_SELECT_BY_ID, {"id": run_id}).mappings().first()
            if current is None:
                raise JobRunValidationError("job run was not found")
            return TransitionResult(False, _record(current))
    except JobRunValidationError:
        raise
    except IntegrityError as exc:
        raise JobRunPersistenceError("terminal transition violated job-run integrity") from exc
    except SQLAlchemyError as exc:
        raise JobRunPersistenceError("terminal transition failed") from exc


def _read_conflict(engine: Any, job_name: str, target_date: date) -> AcquisitionResult:
    parameters = {"job_name": job_name, "target_date": target_date}
    try:
        with engine.begin() as connection:
            completed = connection.execute(
                _SELECT_COMPLETED_BY_KEY, parameters
            ).mappings().first()
            if completed is not None:
                return AcquisitionResult(SKIPPED_COMPLETED, _record(completed))
            processing = connection.execute(
                _SELECT_PROCESSING_BY_JOB, {"job_name": job_name}
            ).mappings().first()
            if processing is not None:
                return AcquisitionResult(SKIPPED_PROCESSING, _record(processing))
            latest = connection.execute(
                _SELECT_LATEST_BY_KEY, parameters
            ).mappings().first()
            if latest is not None:
                return AcquisitionResult(SKIPPED_CONFLICT, _record(latest))
    except SQLAlchemyError as exc:
        raise JobRunPersistenceError(
            "job-run conflict occurred but the winning state could not be read"
        ) from exc
    raise JobRunPersistenceError(
        "job-run conflict cleared before its owner could be identified; retry acquisition"
    )


def _expected_conflict(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None)
    return constraint in EXPECTED_ACQUISITION_CONSTRAINTS


def _rollback_after_acquisition_failure(transaction: Any) -> None:
    """Attempt rollback without replacing the acquisition's primary failure."""
    if transaction is None:
        return
    try:
        if transaction.is_active:
            transaction.rollback()
    except Exception as cleanup_error:
        _log_cleanup_failure("rollback", cleanup_error, commit_confirmed=False)


def _close_acquisition_connection(
    connection: Any,
    *,
    commit_confirmed: bool,
) -> None:
    """Best-effort close that cannot hide a confirmed commit or primary error."""
    try:
        connection.close()
    except Exception as cleanup_error:
        _log_cleanup_failure(
            "close",
            cleanup_error,
            commit_confirmed=commit_confirmed,
        )
        invalidate = getattr(connection, "invalidate", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception as invalidate_error:
                _log_cleanup_failure(
                    "invalidate",
                    invalidate_error,
                    commit_confirmed=commit_confirmed,
                )


def _log_cleanup_failure(
    operation: str,
    cleanup_error: BaseException,
    *,
    commit_confirmed: bool,
) -> None:
    """Record cleanup failure metadata without logging exception contents."""
    commit_state = "confirmed" if commit_confirmed else "not_confirmed"
    _LOGGER.warning(
        "job-run acquisition cleanup failed: operation=%s "
        "commit_state=%s cleanup_error_type=%s",
        operation,
        commit_state,
        type(cleanup_error).__name__,
    )


def _one(result: Any, message: str) -> Mapping[str, Any]:
    row = result.mappings().first()
    if row is None:
        raise JobRunPersistenceError(message)
    return row


def _record(row: Mapping[str, Any]) -> JobRunRecord:
    return JobRunRecord(
        id=_run_id(row["id"]),
        job_name=_job_name(row["job_name"]),
        target_date=_target_date(row["target_date"]),
        status=_status(row["status"]),
        started_at=_optional_utc_time(row["started_at"], "started_at"),
        finished_at=_optional_utc_time(row["finished_at"], "finished_at"),
        error_message=row["error_message"],
        created_at=_required_utc_time(row["created_at"], "created_at"),
    )


def _job_name(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise JobRunValidationError("job_name must be non-empty and trimmed")
    if len(value) > MAX_JOB_NAME_LENGTH:
        raise JobRunValidationError("job_name is too long")
    return value


def _target_date(value: Any) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise JobRunValidationError("target_date must be a date")
    return value


def _run_id(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise JobRunValidationError("run_id must be a UUID") from exc


def _status(value: Any) -> str:
    if value not in JOB_RUN_STATUSES:
        raise JobRunPersistenceError("database returned an invalid job-run status")
    return value


def _utc_time(value: datetime | None) -> datetime:
    moment = datetime.now(timezone.utc) if value is None else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise JobRunValidationError("timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _optional_utc_time(value: Any, field: str) -> datetime | None:
    return None if value is None else _required_utc_time(value, field)


def _required_utc_time(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise JobRunPersistenceError(f"database returned invalid {field}")
    return value.astimezone(timezone.utc)
