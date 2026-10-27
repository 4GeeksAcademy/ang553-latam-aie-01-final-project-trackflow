"""Week-lease coordination for one caller-owned PostgreSQL transaction.

Phase 2.2B-1 only. A granted lease is not durable until the caller commits,
and this module never commits. It does not UPSERT KPIs or certify a report.
Cross-process exclusion is not claimed without an isolated PostgreSQL test.

Lease instants use clock_timestamp(), not the Codespace clock and not a caller
argument. transaction_timestamp() is fixed at transaction start, so a long
transaction could treat an expired lease as valid. statement_timestamp() is
fixed when the command is received, before a row-lock wait. clock_timestamp()
is volatile: PostgreSQL rechecks an UPDATE qualification after the wait, and
the SELECT projection below references the locked row so it runs after FOR UPDATE.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    PendingRollbackError,
    SQLAlchemyError,
)

from pipelines.run_store import (
    CONTROL_TABLE,
    STATUS_SKIPPED_IN_PROGRESS,
    STATUS_SUCCEEDED,
    STATUS_SUCCEEDED_NOOP,
    STATUS_SUPERSEDED,
    RunStoreError,
    _optional_time,
    _require_run,
    _require_transaction,
    _uuid,
    _week,
    ensure_week_control,
    update_run,
)

STATUS_ACQUIRED = "acquired"
STATUS_RECOVERED = "recovered"
STATUS_OCCUPIED = "occupied"
STATUS_ALREADY_HELD = "already_held"
STATUS_VERIFIED = "verified"
STATUS_RENEWED = "renewed"
STATUS_RELEASED = "released"
STATUS_REJECTED = "rejected"
LEASE_STATUSES = frozenset(
    {
        STATUS_ACQUIRED,
        STATUS_RECOVERED,
        STATUS_OCCUPIED,
        STATUS_ALREADY_HELD,
        STATUS_VERIFIED,
        STATUS_RENEWED,
        STATUS_RELEASED,
        STATUS_REJECTED,
    }
)
_GRANT_STATUSES = frozenset({STATUS_ACQUIRED, STATUS_RECOVERED})
_MIN_LEASE_SECONDS = 1
_MAX_LEASE_SECONDS = 24 * 60 * 60
_UNCERTAIN = (DisconnectionError, InterfaceError, PendingRollbackError, OperationalError)

LOCK_CONTROL_SQL = text(
    f"""
    /* op:lock_control */
    /* clock:clock_timestamp_after_row_lock */
    SELECT locked.week_start, locked.owner_run_id, locked.lease_acquired_at,
           locked.lease_expires_at, locked.fencing_token, locked.is_free, locked.is_owner,
           (
               locked.lease_expires_at IS NOT NULL
               AND locked.lease_expires_at <= clock_timestamp()
           ) AS is_expired
    FROM (
        SELECT week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token,
               (owner_run_id IS NULL) AS is_free,
               (owner_run_id = :run_id) AS is_owner
        FROM {CONTROL_TABLE}
        WHERE week_start = :week_start
        FOR UPDATE
    ) AS locked
    """
)
GRANT_LEASE_SQL = text(
    f"""
    /* op:grant_lease */
    /* clock:clock_timestamp_in_lock_recheck */
    UPDATE {CONTROL_TABLE}
    SET owner_run_id = :run_id,
        lease_acquired_at = clock_timestamp(),
        lease_expires_at = clock_timestamp() + (:lease_seconds * INTERVAL '1 second'),
        fencing_token = fencing_token + 1,
        updated_at = clock_timestamp()
    WHERE week_start = :week_start
      AND fencing_token = :expected_fencing_token
      AND (
            owner_run_id IS NULL
            OR (
                lease_expires_at IS NOT NULL
                AND lease_expires_at <= clock_timestamp()
            )
      )
    RETURNING week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token
    """
)
VERIFY_LEASE_SQL = text(
    f"""
    /* op:verify_lease */
    /* clock:clock_timestamp_after_row_lock */
    SELECT locked.week_start, locked.owner_run_id, locked.lease_acquired_at,
           locked.lease_expires_at, locked.fencing_token, locked.is_owner, locked.token_matches,
           (
               locked.lease_expires_at IS NOT NULL
               AND locked.lease_expires_at > clock_timestamp()
           ) AS unexpired
    FROM (
        SELECT week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token,
               (owner_run_id = :run_id) AS is_owner,
               (fencing_token = :fencing_token) AS token_matches
        FROM {CONTROL_TABLE}
        WHERE week_start = :week_start
        FOR UPDATE
    ) AS locked
    """
)
RENEW_LEASE_SQL = text(
    f"""
    /* op:renew_lease */
    /* clock:clock_timestamp_in_lock_recheck */
    UPDATE {CONTROL_TABLE}
    SET lease_expires_at = clock_timestamp() + (:lease_seconds * INTERVAL '1 second'),
        updated_at = clock_timestamp()
    WHERE week_start = :week_start
      AND owner_run_id = :run_id
      AND fencing_token = :fencing_token
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at > clock_timestamp()
    RETURNING week_start, owner_run_id, lease_acquired_at, lease_expires_at, fencing_token
    """
)
RELEASE_LEASE_SQL = text(
    f"""
    /* op:release_lease */
    UPDATE {CONTROL_TABLE}
    SET owner_run_id = NULL,
        lease_acquired_at = NULL,
        lease_expires_at = NULL,
        updated_at = clock_timestamp()
    WHERE week_start = :week_start
      AND owner_run_id = :run_id
      AND fencing_token = :fencing_token
    RETURNING week_start, fencing_token
    """
)
_SQL_STATEMENTS = (
    LOCK_CONTROL_SQL,
    GRANT_LEASE_SQL,
    VERIFY_LEASE_SQL,
    RENEW_LEASE_SQL,
    RELEASE_LEASE_SQL,
)


class ConcurrencyError(RuntimeError):
    """Identifiable lease failure. Not a free lease and not a certification."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LeaseDecision:
    """In-transaction lease observation. durable is always false here."""

    week_start: date
    status: str
    run_id: UUID | None = None
    owner_run_id: UUID | None = None
    fencing_token: int | None = None
    lease_expires_at: datetime | None = None
    lock_held: bool = False
    durable: bool = False

    def __post_init__(self) -> None:
        if self.durable:
            raise ConcurrencyError("durability_forbidden", "this module cannot mark a lease durable")
        if self.status not in LEASE_STATUSES:
            raise ConcurrencyError("invalid_status", "lease status is not recognized")

    @property
    def granted(self) -> bool:
        return self.status in _GRANT_STATUSES

    @property
    def lease_verified(self) -> bool:
        return self.status == STATUS_VERIFIED and self.lock_held and not self.durable

    @property
    def business_certified(self) -> bool:
        return False

    @property
    def concurrency_guaranteed(self) -> bool:
        return False


def acquire_week_lease(
    connection: Any,
    week_start: date,
    run_id: UUID,
    *,
    lease_seconds: int,
) -> LeaseDecision:
    """Grant one uncommitted lease, or report that the week is still held.

    A valid lease is left unchanged. An expired lease can be recovered and its
    token advances. The caller must commit before the grant is durable.
    """
    identity, week, seconds = _prepared(
        connection, week_start, run_id, lease_seconds=lease_seconds
    )
    _run_for_week(connection, identity, week)
    try:
        ensure_week_control(connection, week)
        locked = _one(
            connection,
            LOCK_CONTROL_SQL,
            {"week_start": week, "run_id": identity},
        )
    except ConcurrencyError:
        raise
    except RunStoreError as exc:
        raise _from_store(exc) from exc
    if locked is None:
        raise ConcurrencyError("query_failed", "week control disappeared after ensure")
    if not locked["is_free"] and not locked["is_expired"]:
        status = STATUS_ALREADY_HELD if locked["is_owner"] else STATUS_OCCUPIED
        return _decision(week, status, identity, locked, lock_held=True)
    granted = _one(
        connection,
        GRANT_LEASE_SQL,
        {
            "week_start": week,
            "run_id": identity,
            "lease_seconds": seconds,
            "expected_fencing_token": locked["fencing_token"],
        },
    )
    if granted is None:
        raise ConcurrencyError(
            "transaction_uncertain",
            "locked lease row was not updated; not treating the week as free",
        )
    status = STATUS_ACQUIRED if locked["is_free"] else STATUS_RECOVERED
    return _decision(week, status, identity, granted, lock_held=True)


def verify_week_lease(
    connection: Any,
    week_start: date,
    run_id: UUID,
    fencing_token: int,
) -> LeaseDecision:
    """Lock the week row and check owner, token, and expiry.

    The row lock lasts until the caller commits or rolls back. A later publish
    must use this same open transaction. This function does not authorize a
    business publication and does not commit.
    """
    identity, week, _seconds = _prepared(connection, week_start, run_id, lease_seconds=None)
    token = _token(fencing_token)
    locked = _one(
        connection,
        VERIFY_LEASE_SQL,
        {"week_start": week, "run_id": identity, "fencing_token": token},
    )
    if locked is None:
        return LeaseDecision(week_start=week, status=STATUS_REJECTED, run_id=identity, lock_held=False)
    verified = bool(locked["is_owner"] and locked["token_matches"] and locked["unexpired"])
    return _decision(
        week,
        STATUS_VERIFIED if verified else STATUS_REJECTED,
        identity,
        locked,
        lock_held=True,
    )


def renew_week_lease(
    connection: Any,
    week_start: date,
    run_id: UUID,
    fencing_token: int,
    *,
    lease_seconds: int,
) -> LeaseDecision:
    """Extend expiry when week, owner, and token still match. Does not bump the token."""
    identity, week, seconds = _prepared(
        connection, week_start, run_id, lease_seconds=lease_seconds
    )
    renewed = _one(
        connection,
        RENEW_LEASE_SQL,
        {
            "week_start": week,
            "run_id": identity,
            "fencing_token": _token(fencing_token),
            "lease_seconds": seconds,
        },
    )
    if renewed is None:
        return LeaseDecision(week_start=week, status=STATUS_REJECTED, run_id=identity, lock_held=False)
    return _decision(week, STATUS_RENEWED, identity, renewed, lock_held=True)


def release_week_lease(
    connection: Any,
    week_start: date,
    run_id: UUID,
    fencing_token: int,
) -> LeaseDecision:
    """Clear owner and expiry only for the matching token. The token is not reused."""
    identity, week, _seconds = _prepared(connection, week_start, run_id, lease_seconds=None)
    released = _one(
        connection,
        RELEASE_LEASE_SQL,
        {"week_start": week, "run_id": identity, "fencing_token": _token(fencing_token)},
    )
    if released is None:
        return LeaseDecision(week_start=week, status=STATUS_REJECTED, run_id=identity, lock_held=False)
    return LeaseDecision(
        week_start=week,
        status=STATUS_RELEASED,
        run_id=identity,
        owner_run_id=None,
        fencing_token=int(released["fencing_token"]),
        lock_held=True,
    )


def record_lease_run_status(
    connection: Any,
    run_id: UUID,
    *,
    status: str,
    finished_at: datetime,
    reason: str,
) -> Any:
    """Record skipped_in_progress or superseded. Never records success."""
    _require_transaction(connection)
    if status in {STATUS_SUCCEEDED, STATUS_SUCCEEDED_NOOP} or status not in {
        STATUS_SKIPPED_IN_PROGRESS,
        STATUS_SUPERSEDED,
    }:
        raise ConcurrencyError("success_forbidden", "a lease conflict is not a successful publication")
    identity = _uuid(run_id, "run_id")
    try:
        current = _require_run(connection, identity)
    except RunStoreError as exc:
        raise _from_store(exc) from exc
    try:
        updated = update_run(
            connection,
            identity,
            expected_status=current.status,
            status=status,
            finished_at=finished_at,
            block_reason=reason,
            stage_reached="lease",
        )
    except RunStoreError as exc:
        raise _from_store(exc) from exc
    if updated.status in {STATUS_SUCCEEDED, STATUS_SUCCEEDED_NOOP}:
        raise ConcurrencyError("success_forbidden", "lease handling must not mark a run succeeded")
    return updated


def _prepared(
    connection: Any,
    week_start: date,
    run_id: UUID,
    *,
    lease_seconds: int | None,
) -> tuple[UUID, date, int | None]:
    _require_transaction(connection)
    week = _week(week_start)
    identity = _uuid(run_id, "run_id")
    seconds = None if lease_seconds is None else _lease_seconds(lease_seconds)
    return identity, week, seconds


def _run_for_week(connection: Any, run_id: UUID, week: date) -> None:
    try:
        run = _require_run(connection, run_id)
    except RunStoreError as exc:
        raise _from_store(exc) from exc
    if run.target_week_start != week:
        raise ConcurrencyError("week_mismatch", "run target week does not match the lease week")


def _one(connection: Any, statement: Any, parameters: Mapping[str, Any]) -> Mapping[str, Any] | None:
    rows = _rows(connection, statement, parameters)
    if not rows:
        return None
    if len(rows) != 1:
        raise ConcurrencyError("query_failed", "lease statement returned more than one week")
    return rows[0]


def _rows(connection: Any, statement: Any, parameters: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    _assert_statement(statement)
    try:
        result = connection.execute(statement, dict(parameters))
    except _UNCERTAIN as exc:
        raise ConcurrencyError(
            "transaction_uncertain",
            "lease statement outcome is uncertain; the week is not free",
        ) from exc
    except IntegrityError as exc:
        raise ConcurrencyError("integrity_error", "lease write violated a constraint") from exc
    except SQLAlchemyError as exc:
        raise ConcurrencyError("query_failed", "lease statement failed") from exc
    if result is None:
        return []
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return [dict(row) for row in mappings().all()]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return [dict(row) for row in result]
    raise ConcurrencyError("query_failed", "lease statement returned no row mapping")


def _decision(
    week: date,
    status: str,
    run_id: UUID,
    row: Mapping[str, Any],
    *,
    lock_held: bool,
) -> LeaseDecision:
    owner = row.get("owner_run_id")
    expires = row.get("lease_expires_at")
    return LeaseDecision(
        week_start=week,
        status=status,
        run_id=run_id,
        owner_run_id=None if owner is None else _uuid(owner, "owner_run_id"),
        fencing_token=None if row.get("fencing_token") is None else int(row["fencing_token"]),
        lease_expires_at=_optional_time(expires, "lease_expires_at"),
        lock_held=lock_held,
    )


def _lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConcurrencyError("invalid_lease_ttl", "lease_seconds must be an integer")
    if value < _MIN_LEASE_SECONDS or value > _MAX_LEASE_SECONDS:
        raise ConcurrencyError("invalid_lease_ttl", "lease_seconds is outside the allowed window")
    return value


def _token(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConcurrencyError("invalid_fencing_token", "fencing_token must be a non-negative integer")
    return value


def _from_store(exc: RunStoreError) -> ConcurrencyError:
    cause = exc.__cause__
    if isinstance(cause, _UNCERTAIN):
        return ConcurrencyError("transaction_uncertain", "lease transaction outcome is uncertain")
    return ConcurrencyError(exc.code, str(exc))


def _assert_statement(statement: Any) -> None:
    sql = str(statement).upper()
    if "TELEMETRY_EVENTS" in sql or "DELETE " in sql or " DROP " in sql:
        raise ConcurrencyError("forbidden_sql", "lease SQL must not delete rows or write telemetry")
    if "WEEKLY_WAREHOUSE_CLIENT_PERFORMANCE" in sql:
        raise ConcurrencyError("forbidden_sql", "lease SQL must not write the business KPI table")


def _assert_module_sql() -> None:
    for statement in _SQL_STATEMENTS:
        sql = str(statement)
        _assert_statement(statement)
        if "'" in sql.split("/*", 1)[0]:
            raise ConcurrencyError("forbidden_sql", "lease values must be bound parameters")
        if sql.count(":") < 1:
            raise ConcurrencyError("forbidden_sql", "lease statements must be parameterized")
    if "FOR UPDATE" not in str(LOCK_CONTROL_SQL).upper():
        raise ConcurrencyError("forbidden_sql", "acquisition must lock the control row")
    if "FOR UPDATE" not in str(VERIFY_LEASE_SQL).upper():
        raise ConcurrencyError("forbidden_sql", "verification must hold the row lock")
    if "FENCING_TOKEN + 1" not in str(GRANT_LEASE_SQL).upper():
        raise ConcurrencyError("forbidden_sql", "a new lease must advance the fencing token")
    if "FENCING_TOKEN + 1" in str(RELEASE_LEASE_SQL).upper() or "FENCING_TOKEN -" in str(RELEASE_LEASE_SQL).upper():
        raise ConcurrencyError("forbidden_sql", "release must not change the fencing token")
    combined = "\n".join(str(statement) for statement in _SQL_STATEMENTS)
    if "transaction_timestamp()" in combined or "statement_timestamp()" in combined:
        raise ConcurrencyError("forbidden_sql", "lease expiry must not use a frozen transaction or statement clock")
    if ":observed_at" in combined or "OBSERVED_AT" in combined.upper():
        raise ConcurrencyError("forbidden_sql", "callers cannot inject a lease clock")
    for statement in (LOCK_CONTROL_SQL, GRANT_LEASE_SQL, VERIFY_LEASE_SQL, RENEW_LEASE_SQL):
        if "clock_timestamp()" not in str(statement):
            raise ConcurrencyError("forbidden_sql", "lease expiry must use clock_timestamp()")


_assert_module_sql()
