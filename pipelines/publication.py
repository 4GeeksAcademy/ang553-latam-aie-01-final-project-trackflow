"""Transactional staging for weekly warehouse/client aggregates.

Phase 2.1 only. The public entry point refuses to write unless a QualityReport
is present and free of blocking diagnostics. It still does not certify a
business publication. The internal stager can be used later by the Phase 2.2
coordinator inside the same open transaction. This module never commits.

Nothing returned here is a certified publication. ``business_certified`` is
always false. A ``staged`` status means uncommitted statements only.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import (
    DataError,
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    PendingRollbackError,
    ProgrammingError,
    SQLAlchemyError,
)

from pipelines.weekly_performance import (
    POSTGRES_INTEGER_MAX,
    AggregateKey,
    QualityReport,
    WeeklyAggregate,
    _valid_client_id,
    iso_week_bounds,
)

DESTINATION_TABLE = "reporting.weekly_warehouse_client_performance"
CANONICAL_WAREHOUSES = frozenset({"los_angeles", "zaragoza"})
KPI_COUNT_FIELDS = (
    "inbound_units_count",
    "outbound_orders_count",
    "stockout_events_count",
    "discrepancy_events_count",
)
KPI_VALUE_FIELDS = KPI_COUNT_FIELDS + ("discrepancy_rate",)
BLOCKING_QUALITY_CODES = frozenset(
    {
        "aggregate_integer_overflow",
        "stockexit_identity_conflict",
        "stockexit_identity_unverified",
        "stockexit_movement_missing",
        "stockexit_type_unknown",
        "outbound_classification_unavailable",
    }
)

STATUS_BLOCKED = "blocked"
STATUS_STAGED = "staged"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_CONNECTION_ERROR = "connection_error"
STATUS_WRITE_FAILED = "write_failed"
STATUS_COMMIT_UNCERTAIN = "commit_uncertain"
STATUS_TECHNICALLY_COMMITTED = "technically_committed"
STATUS_COMMIT_FAILED = "commit_failed"
STATUS_COMMIT_UNAUTHORIZED = "commit_unauthorized"
STATUS_ROLLBACK_FAILED = "rollback_failed"
_UNUSABLE_FLAG = "trackflow_publication_unusable"
_UNUSABLE_CONNECTIONS: weakref.WeakSet[Any] = weakref.WeakSet()

_UNCERTAIN_SQLSTATES = frozenset(
    {
        "08000",
        "08001",
        "08003",
        "08004",
        "08006",
        "08007",
        "57P01",
        "57P02",
        "57P03",
    }
)
_CONNECTION_MARKERS = (
    "could not connect",
    "connection refused",
    "server closed the connection",
    "connection already closed",
    "consuming input failed",
    "could not receive data",
    "ssl syscall error",
    "connection reset",
    "broken pipe",
)

PUBLISHED_KEYS_SQL_TEXT = f"""
SELECT warehouse,
       client_id,
       inbound_units_count,
       outbound_orders_count,
       stockout_events_count,
       discrepancy_events_count,
       discrepancy_rate
FROM {DESTINATION_TABLE}
WHERE week_start = :week_start
FOR UPDATE
"""
PUBLISHED_KEYS_SQL = text(PUBLISHED_KEYS_SQL_TEXT)

_INSERT_COLUMNS = """
    warehouse,
    client_id,
    week_start,
    inbound_units_count,
    outbound_orders_count,
    stockout_events_count,
    discrepancy_events_count,
    discrepancy_rate
"""
_UPDATE_CLAUSE = f"""
ON CONFLICT (warehouse, client_id, week_start)
DO UPDATE SET
    inbound_units_count = EXCLUDED.inbound_units_count,
    outbound_orders_count = EXCLUDED.outbound_orders_count,
    stockout_events_count = EXCLUDED.stockout_events_count,
    discrepancy_events_count = EXCLUDED.discrepancy_events_count,
    discrepancy_rate = EXCLUDED.discrepancy_rate,
    computed_at = statement_timestamp()
WHERE {DESTINATION_TABLE}.inbound_units_count
        IS DISTINCT FROM EXCLUDED.inbound_units_count
    OR {DESTINATION_TABLE}.outbound_orders_count
        IS DISTINCT FROM EXCLUDED.outbound_orders_count
    OR {DESTINATION_TABLE}.stockout_events_count
        IS DISTINCT FROM EXCLUDED.stockout_events_count
    OR {DESTINATION_TABLE}.discrepancy_events_count
        IS DISTINCT FROM EXCLUDED.discrepancy_events_count
    OR {DESTINATION_TABLE}.discrepancy_rate
        IS DISTINCT FROM EXCLUDED.discrepancy_rate
"""
WEEKLY_UPSERT_SQL = f"""
INSERT INTO {DESTINATION_TABLE} ({_INSERT_COLUMNS})
VALUES (
    :warehouse_0,
    :client_id_0,
    :week_start_0,
    :inbound_units_count_0,
    :outbound_orders_count_0,
    :stockout_events_count_0,
    :discrepancy_events_count_0,
    :discrepancy_rate_0
)
{_UPDATE_CLAUSE}
RETURNING warehouse, client_id
"""


class _StatementFailure(Exception):
    """Internal carrier for a classified database statement failure."""

    def __init__(self, exc: BaseException, phase: str):
        self.exc = exc
        self.phase = phase
        super().__init__(str(exc))


@dataclass(frozen=True)
class PublicationIssue:
    """An identifiable validation, reconciliation, or database error."""

    code: str
    message: str
    sqlstate: str | None = None


@dataclass(frozen=True)
class PublicationOutcome:
    """Uncommitted staging result. Never a business-certified publication."""

    week_start: date
    status: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: tuple[PublicationIssue, ...] = ()
    disappeared_keys: tuple[AggregateKey, ...] = ()
    committed: bool = False
    writes_attempted: bool = False
    rollback_observed: bool = False
    staged_uncommitted: bool = False
    quality_gate: str = "not_evaluated"

    @property
    def business_certified(self) -> bool:
        return False


@dataclass(frozen=True)
class ReconciliationResult:
    """P(W) - C(W) observed inside the caller's transaction."""

    week_start: date
    disappeared_keys: tuple[AggregateKey, ...] = ()
    errors: tuple[PublicationIssue, ...] = ()
    read_completed: bool = False
    published_kpis: tuple[tuple[AggregateKey, tuple[int, int, int, int, Decimal]], ...] = ()


@dataclass(frozen=True)
class TransactionObservation:
    """Technical commit or rollback observation. Not a business certification."""

    status: str
    error: PublicationIssue | None = None
    rollback_observed: bool = False

    @property
    def business_certified(self) -> bool:
        return False


def official_discrepancy_rate(
    outbound_orders_count: int,
    discrepancy_events_count: int,
) -> Decimal:
    """Return the Phase 1 rate for counters already on a weekly aggregate.

    This does not recompute KPIs from raw telemetry. It mirrors the precision
    used by ``transform_weekly_performance`` so a batch can be rejected when
    its rate does not match that contract.
    """
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        if outbound_orders_count == 0:
            return Decimal(0)
        return Decimal(discrepancy_events_count) / Decimal(outbound_orders_count)


def validate_weekly_publication_batch(
    week_start: date,
    aggregates: Sequence[WeeklyAggregate],
    *,
    now: datetime | None = None,
) -> tuple[PublicationIssue, ...]:
    """Reject a batch that must not be written. Aggregates are not modified."""
    issues: list[PublicationIssue] = []
    week_issue = _week_issue(week_start)
    if week_issue is not None:
        issues.append(week_issue)
    else:
        clock = datetime.now(timezone.utc) if now is None else now
        complete_issue = _complete_week_issue(week_start, clock)
        if complete_issue is not None:
            issues.append(complete_issue)

    if isinstance(aggregates, (str, bytes)) or not isinstance(aggregates, Sequence):
        issues.append(
            PublicationIssue(
                "schema_incompatible",
                "aggregates must be a sequence of WeeklyAggregate instances",
            )
        )
        return tuple(issues)
    if len(aggregates) == 0:
        issues.append(
            PublicationIssue(
                "empty_batch_not_certified",
                "An empty batch is not a certified week without activity",
            )
        )
        return tuple(issues)

    seen: set[tuple[str, str, date]] = set()
    for aggregate in aggregates:
        issues.extend(_aggregate_issues(week_start, aggregate))
        if not isinstance(aggregate, WeeklyAggregate):
            continue
        try:
            identity = (aggregate.warehouse, aggregate.client_id, aggregate.week_start)
        except TypeError:
            issues.append(
                PublicationIssue(
                    "schema_incompatible",
                    "aggregate identity is not comparable",
                )
            )
            continue
        if identity in seen:
            issues.append(
                PublicationIssue(
                    "duplicate_aggregate_key",
                    "duplicate publication key "
                    f"warehouse={aggregate.warehouse}, client_id={aggregate.client_id}, "
                    f"week_start={aggregate.week_start}",
                )
            )
        seen.add(identity)
    return tuple(issues)


def blocking_quality_issues(quality: QualityReport) -> tuple[PublicationIssue, ...]:
    """Block known quality diagnostics. This is not the Phase 2.2 authorizer."""
    if not isinstance(quality, QualityReport):
        return (
            PublicationIssue(
                "schema_incompatible",
                "quality must be a QualityReport",
            ),
        )
    issues: list[PublicationIssue] = []
    for item in quality.issues:
        if item.code in BLOCKING_QUALITY_CODES:
            issues.append(
                PublicationIssue(
                    "quality_blocked",
                    f"{item.code}: {item.message}",
                )
            )
    if quality.coverage_pending:
        issues.append(
            PublicationIssue(
                "coverage_insufficient",
                "Coverage diagnostics are unresolved; the batch cannot be certified",
            )
        )
    if quality.conflict_movement_keys:
        issues.append(
            PublicationIssue(
                "identity_conflict",
                "Conflicting identities are unresolved; the batch cannot be written",
            )
        )
    if quality.cross_week_verification_pending:
        issues.append(
            PublicationIssue(
                "classification_incomplete",
                "Cross-week classification is incomplete; the batch cannot be written",
            )
        )
    return tuple(issues)


def reconcile_disappeared_keys(
    connection: Any,
    week_start: date,
    aggregates: Sequence[WeeklyAggregate],
) -> ReconciliationResult:
    """Return published keys absent from the calculated batch.

    Must be called inside the same open transaction as the future protected
    publication. ``FOR UPDATE`` locks rows already visible to this transaction
    only. It is not a lease or fencing token: concurrent inserts of new keys
    are not prevented, and this function never deletes disappeared rows.
    """
    week_issue = _week_issue(week_start)
    if week_issue is not None:
        return ReconciliationResult(week_start, errors=(week_issue,))
    transaction_issue = _transaction_issue(connection)
    if transaction_issue is not None:
        return ReconciliationResult(week_start, errors=(transaction_issue,))

    calculated: set[AggregateKey] = set()
    if isinstance(aggregates, (str, bytes)) or not isinstance(aggregates, Sequence):
        return ReconciliationResult(
            week_start,
            errors=(
                PublicationIssue(
                    "schema_incompatible",
                    "aggregates must be a sequence of WeeklyAggregate instances",
                ),
            ),
        )
    for aggregate in aggregates:
        if not isinstance(aggregate, WeeklyAggregate):
            return ReconciliationResult(
                week_start,
                errors=(
                    PublicationIssue(
                        "schema_incompatible",
                        "reconciliation requires WeeklyAggregate instances",
                    ),
                ),
            )
        if aggregate.week_start == week_start:
            calculated.add(
                AggregateKey(aggregate.warehouse, aggregate.client_id, week_start)
            )

    try:
        result = _execute(connection, PUBLISHED_KEYS_SQL, {"week_start": week_start})
        rows = _mapping_rows(result)
    except _StatementFailure as failure:
        return ReconciliationResult(
            week_start,
            errors=(_database_issue(failure, phase="reconcile"),),
        )

    published: list[AggregateKey] = []
    published_values: list[tuple[AggregateKey, tuple[int, int, int, int, Decimal]]] = []
    seen_published: set[AggregateKey] = set()
    for row in rows:
        try:
            key = _published_key(row, week_start)
        except (KeyError, TypeError, ValueError) as exc:
            return ReconciliationResult(
                week_start,
                errors=(
                    PublicationIssue(
                        "published_key_unreadable",
                        f"Published key could not be read: {exc}",
                    ),
                ),
            )
        if key in seen_published:
            return ReconciliationResult(
                week_start,
                errors=(
                    PublicationIssue(
                        "published_key_duplicate",
                        "Destination returned a duplicate logical key for the week",
                    ),
                ),
            )
        try:
            published_values.append((key, _kpi_tuple(row)))
        except (KeyError, TypeError, ValueError) as exc:
            return ReconciliationResult(
                week_start,
                errors=(
                    PublicationIssue(
                        "published_snapshot_unreadable",
                        f"Published KPI snapshot could not be read: {exc}",
                    ),
                ),
            )
        seen_published.add(key)
        published.append(key)

    disappeared = tuple(sorted(key for key in published if key not in calculated))
    return ReconciliationResult(
        week_start,
        disappeared_keys=disappeared,
        read_completed=True,
        published_kpis=tuple(published_values),
    )


def stage_weekly_upsert(
    connection: Any,
    week_start: date,
    aggregates: Sequence[WeeklyAggregate],
    *,
    now: datetime | None = None,
    quality: QualityReport | None = None,
) -> PublicationOutcome:
    """Validate structured quality diagnostics, then stage without committing.

    ``quality=None`` and any blocking diagnostic prevent writes. A report with
    no blocking diagnostics may be staged. Staging is not business certification
    and this module never commits.
    """
    validation_issues = list(
        validate_weekly_publication_batch(week_start, aggregates, now=now)
    )
    quality_issues, quality_gate = _public_quality_issues(quality)
    if quality_issues:
        return _outcome(
            week_start,
            STATUS_BLOCKED,
            errors=tuple(validation_issues + list(quality_issues)),
            quality_gate=quality_gate,
        )
    return _stage_weekly_upsert(
        connection,
        week_start,
        aggregates,
        now=now,
        quality_gate=quality_gate,
    )


def _stage_weekly_upsert(
    connection: Any,
    week_start: date,
    aggregates: Sequence[WeeklyAggregate],
    *,
    now: datetime | None = None,
    quality_gate: str = "not_evaluated",
) -> PublicationOutcome:
    """Stage one week inside the caller's open transaction.

    Internal coordinator seam only. It does not evaluate quality, authorize a
    run, or commit. Phase 2.2 may write publication metadata in this same
    transaction before its own COMMIT. Calling this function is not a business
    publication.
    """
    validation_issues = list(
        validate_weekly_publication_batch(week_start, aggregates, now=now)
    )
    week_invalid = any(issue.code == "invalid_week_start" for issue in validation_issues)
    only_empty = bool(validation_issues) and all(
        issue.code == "empty_batch_not_certified" for issue in validation_issues
    )
    if week_invalid or (validation_issues and not only_empty):
        return _outcome(
            week_start,
            STATUS_BLOCKED,
            errors=tuple(validation_issues),
            quality_gate=quality_gate,
        )

    transaction_issue = _transaction_issue(connection)
    if transaction_issue is not None:
        return _outcome(
            week_start,
            _status_for_issues((transaction_issue,)),
            errors=tuple(validation_issues + [transaction_issue]),
            quality_gate=quality_gate,
        )

    reconciliation = reconcile_disappeared_keys(connection, week_start, aggregates)
    if not reconciliation.read_completed:
        errors = tuple(validation_issues + list(reconciliation.errors))
        database_failure = any(
            issue.code in {"connection_error", "database_error", "integrity_error", "write_error"}
            for issue in reconciliation.errors
        )
        rollback_observed = False
        if database_failure:
            rollback_observed = _rollback_quietly(connection)
            _close_failed_transaction(
                connection,
                connection_failure=any(issue.code == "connection_error" for issue in errors),
                rollback_observed=rollback_observed,
            )
        return _outcome(
            week_start,
            _status_for_issues(errors),
            errors=errors,
            rollback_observed=rollback_observed,
            quality_gate=quality_gate,
        )

    blockers = list(validation_issues)
    if reconciliation.disappeared_keys:
        blockers.append(
            PublicationIssue(
                "reconciliation_conflict",
                "Published keys are absent from the calculated batch; "
                "automatic deletion is not allowed",
            )
        )
    if blockers:
        return _outcome(
            week_start,
            STATUS_BLOCKED,
            errors=tuple(blockers),
            disappeared_keys=reconciliation.disappeared_keys,
            quality_gate=quality_gate,
        )

    ordered = tuple(sorted(aggregates, key=_aggregate_sort_key))
    statement = build_weekly_upsert_statement(len(ordered))
    parameters: dict[str, Any] = {}
    for index, aggregate in enumerate(ordered):
        parameters.update(_bind_aggregate(index, aggregate))
    try:
        result = _execute(connection, statement, parameters)
        rows = _mapping_rows(result)
        inserted, updated, unchanged = _counts_from_snapshot(
            ordered,
            reconciliation.published_kpis,
            rows,
            week_start,
        )
    except (_StatementFailure, ValueError, KeyError, TypeError) as exc:
        failure = exc if isinstance(exc, _StatementFailure) else None
        issue = (
            _database_issue(failure, phase="upsert")
            if failure is not None
            else PublicationIssue("upsert_result_unreadable", str(exc))
        )
        connection_failure = failure is not None and _is_connection_failure(failure.exc)
        rollback_observed = _rollback_quietly(connection)
        status = _close_failed_transaction(
            connection,
            connection_failure=connection_failure,
            rollback_observed=rollback_observed,
        )
        return _outcome(
            week_start,
            status,
            errors=(issue,),
            writes_attempted=True,
            rollback_observed=rollback_observed,
            quality_gate=quality_gate,
        )

    return _outcome(
        week_start,
        STATUS_STAGED,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        staged_uncommitted=True,
        writes_attempted=True,
        quality_gate=quality_gate,
    )


def build_weekly_upsert_statement(row_count: int) -> Any:
    """Build one multi-row UPSERT so RETURNING covers the whole batch."""
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 1:
        raise ValueError("upsert requires at least one row")
    values = ", ".join(_value_clause(index) for index in range(row_count))
    sql = (
        f"INSERT INTO {DESTINATION_TABLE} ({_INSERT_COLUMNS})\n"
        f"VALUES {values}\n"
        f"{_UPDATE_CLAUSE}\n"
        "RETURNING warehouse, client_id"
    )
    _assert_sql_contract(sql)
    return text(sql)


def rollback_publication_transaction(connection: Any) -> TransactionObservation:
    """Roll back the caller's transaction. Staged rows are not published."""
    if connection is None or not hasattr(connection, "rollback"):
        return TransactionObservation(
            status=STATUS_ROLLBACK_FAILED,
            error=PublicationIssue(
                "rollback_failed",
                "Connection does not expose rollback()",
            ),
        )
    try:
        connection.rollback()
    except Exception as exc:
        _mark_unusable(connection)
        return TransactionObservation(
            status=STATUS_ROLLBACK_FAILED,
            error=PublicationIssue(
                "rollback_failed",
                str(exc),
                sqlstate=_sqlstate(exc),
            ),
        )
    return TransactionObservation(status=STATUS_ROLLED_BACK, rollback_observed=True)


def attempt_technical_commit(connection: Any) -> TransactionObservation:
    """Refuse to COMMIT a publication transaction.

    Phase 2.1 has no business authorization. Calling this function never calls
    ``connection.commit()``, including after a staged or blocked batch and after
    an aborted transaction. Phase 2.2 must write its authorization metadata in
    the same open transaction and perform its own COMMIT. A lost COMMIT remains
    uncertain; ``_commit_failure_is_uncertain`` classifies that exception and
    does not retry writes. This module does not apply that classification
    because it does not attempt COMMIT.
    """
    if _connection_unusable(connection):
        return TransactionObservation(
            status=STATUS_COMMIT_UNAUTHORIZED,
            error=PublicationIssue(
                "transaction_aborted",
                "Refusing to commit an aborted or unusable transaction",
            ),
        )
    return TransactionObservation(
        status=STATUS_COMMIT_UNAUTHORIZED,
        error=PublicationIssue(
            "commit_unauthorized",
            "This module does not commit. Phase 2.2 must authorize the same "
            "transaction before its own COMMIT.",
        ),
    )


def _outcome(
    week_start: date,
    status: str,
    *,
    inserted: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    errors: tuple[PublicationIssue, ...] = (),
    disappeared_keys: tuple[AggregateKey, ...] = (),
    writes_attempted: bool = False,
    rollback_observed: bool = False,
    staged_uncommitted: bool = False,
    quality_gate: str = "not_evaluated",
) -> PublicationOutcome:
    return PublicationOutcome(
        week_start=week_start if isinstance(week_start, date) and not isinstance(week_start, datetime) else date.min,
        status=status,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        errors=errors,
        disappeared_keys=disappeared_keys,
        committed=False,
        writes_attempted=writes_attempted,
        rollback_observed=rollback_observed,
        staged_uncommitted=staged_uncommitted,
        quality_gate=quality_gate,
    )


def _week_issue(week_start: date) -> PublicationIssue | None:
    if isinstance(week_start, datetime) or not isinstance(week_start, date):
        return PublicationIssue(
            "invalid_week_start",
            "week_start must be a date representing an ISO Monday",
        )
    try:
        iso_week_bounds(week_start)
    except ValueError as exc:
        return PublicationIssue("invalid_week_start", str(exc))
    return None


def _complete_week_issue(week_start: date, now: datetime) -> PublicationIssue | None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return PublicationIssue(
            "invalid_clock",
            "now must be a timezone-aware datetime",
        )
    _start, next_start = iso_week_bounds(week_start)
    if now.astimezone(timezone.utc) < next_start:
        return PublicationIssue(
            "incomplete_week",
            "week_start is not a fully elapsed ISO UTC week",
        )
    return None


def _aggregate_issues(week_start: date, aggregate: Any) -> tuple[PublicationIssue, ...]:
    if not isinstance(aggregate, WeeklyAggregate):
        return (
            PublicationIssue(
                "schema_incompatible",
                "batch items must be WeeklyAggregate instances",
            ),
        )
    issues: list[PublicationIssue] = []
    if aggregate.week_start != week_start:
        issues.append(
            PublicationIssue(
                "week_mismatch",
                "aggregate week_start does not match the requested publication week",
            )
        )
    if aggregate.warehouse not in CANONICAL_WAREHOUSES:
        issues.append(
            PublicationIssue(
                "noncanonical_warehouse",
                "warehouse must already be los_angeles or zaragoza; aliases are not rewritten",
            )
        )
    try:
        client_id = _valid_client_id(aggregate.client_id)
    except ValueError as exc:
        issues.append(PublicationIssue("invalid_client_id", str(exc)))
    else:
        if client_id != aggregate.client_id:
            issues.append(
                PublicationIssue(
                    "invalid_client_id",
                    "client_id would require silent modification",
                )
            )
    counters_ok = True
    for name in KPI_COUNT_FIELDS:
        value = getattr(aggregate, name)
        if isinstance(value, bool) or not isinstance(value, int):
            counters_ok = False
            issues.append(PublicationIssue("counter_type", f"{name} must be an integer"))
        elif value < 0:
            counters_ok = False
            issues.append(PublicationIssue("negative_counter", f"{name} must be non-negative"))
        elif value > POSTGRES_INTEGER_MAX:
            counters_ok = False
            issues.append(
                PublicationIssue(
                    "counter_overflow",
                    f"{name} exceeds PostgreSQL integer {POSTGRES_INTEGER_MAX}",
                )
            )
    rate = aggregate.discrepancy_rate
    if not isinstance(rate, Decimal) or not rate.is_finite():
        issues.append(
            PublicationIssue(
                "discrepancy_rate_type",
                "discrepancy_rate must be a finite Decimal compatible with numeric",
            )
        )
    elif counters_ok:
        expected = official_discrepancy_rate(
            aggregate.outbound_orders_count,
            aggregate.discrepancy_events_count,
        )
        if rate != expected:
            issues.append(
                PublicationIssue(
                    "discrepancy_rate_mismatch",
                    f"discrepancy_rate {rate} does not match the official value {expected}",
                )
            )
    return tuple(issues)


def _aggregate_sort_key(aggregate: WeeklyAggregate) -> tuple[str, str, date]:
    return (aggregate.warehouse, aggregate.client_id, aggregate.week_start)


def _value_clause(index: int) -> str:
    names = (
        "warehouse",
        "client_id",
        "week_start",
        "inbound_units_count",
        "outbound_orders_count",
        "stockout_events_count",
        "discrepancy_events_count",
        "discrepancy_rate",
    )
    return "(" + ", ".join(f":{name}_{index}" for name in names) + ")"


def _bind_aggregate(index: int, aggregate: WeeklyAggregate) -> dict[str, Any]:
    return {
        f"warehouse_{index}": aggregate.warehouse,
        f"client_id_{index}": aggregate.client_id,
        f"week_start_{index}": aggregate.week_start,
        f"inbound_units_count_{index}": aggregate.inbound_units_count,
        f"outbound_orders_count_{index}": aggregate.outbound_orders_count,
        f"stockout_events_count_{index}": aggregate.stockout_events_count,
        f"discrepancy_events_count_{index}": aggregate.discrepancy_events_count,
        f"discrepancy_rate_{index}": aggregate.discrepancy_rate,
    }


def _published_key(row: Mapping[str, Any], week_start: date) -> AggregateKey:
    warehouse = row["warehouse"]
    client_id = row["client_id"]
    if not isinstance(warehouse, str) or not isinstance(client_id, str):
        raise TypeError("published warehouse and client_id must be text")
    return AggregateKey(warehouse, client_id, week_start)


def _public_quality_issues(
    quality: QualityReport | None,
) -> tuple[tuple[PublicationIssue, ...], str]:
    if quality is None:
        return (
            (
                PublicationIssue(
                    "quality_required",
                    "A QualityReport is required; omitting it does not clear coverage or classification",
                ),
            ),
            "missing",
        )
    issues = blocking_quality_issues(quality)
    return issues, ("blocked" if issues else "diagnostics_clear")


def _transaction_issue(connection: Any) -> PublicationIssue | None:
    if _connection_unusable(connection):
        return PublicationIssue(
            "transaction_aborted",
            "The transaction is aborted or unusable and cannot accept more statements",
        )
    if connection is None or not hasattr(connection, "execute"):
        return PublicationIssue(
            "connection_error",
            "A PostgreSQL connection with execute() is required",
        )
    options: Mapping[str, Any] = {}
    getter = getattr(connection, "get_execution_options", None)
    if callable(getter):
        try:
            loaded = getter() or {}
        except Exception:
            loaded = {}
        if isinstance(loaded, Mapping):
            options = loaded
    if str(options.get("isolation_level", "")).upper() == "AUTOCOMMIT":
        return PublicationIssue(
            "autocommit_forbidden",
            "Autocommit connections cannot stage a weekly batch atomically",
        )
    dialect_name = getattr(getattr(connection, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        return PublicationIssue(
            "postgresql_required",
            "Weekly publication requires a PostgreSQL connection",
        )
    in_transaction = getattr(connection, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        return PublicationIssue(
            "transaction_required",
            "An open transaction is required; this module will not begin or commit one",
        )
    return None


def _execute(connection: Any, statement: Any, parameters: Mapping[str, Any]) -> Any:
    try:
        return connection.execute(statement, dict(parameters))
    except SQLAlchemyError as exc:
        raise _StatementFailure(exc, "execute") from exc


def _mapping_rows(result: Any) -> list[Mapping[str, Any]]:
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return [dict(row) for row in mappings().all()]
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return [dict(row) for row in result]
    raise ValueError("statement result does not expose mappings")


def _kpi_tuple(source: Mapping[str, Any] | WeeklyAggregate) -> tuple[int, int, int, int, Decimal]:
    counts: list[int] = []
    for name in KPI_COUNT_FIELDS:
        value = source[name] if isinstance(source, Mapping) else getattr(source, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} is not an integer")
        counts.append(value)
    rate = source["discrepancy_rate"] if isinstance(source, Mapping) else source.discrepancy_rate
    if isinstance(rate, bool) or isinstance(rate, float):
        raise TypeError("discrepancy_rate is not numeric")
    if isinstance(rate, int):
        rate = Decimal(rate)
    if not isinstance(rate, Decimal) or not rate.is_finite():
        raise TypeError("discrepancy_rate is not a finite numeric value")
    return (counts[0], counts[1], counts[2], counts[3], rate)


def _counts_from_snapshot(
    aggregates: Sequence[WeeklyAggregate],
    published_kpis: Sequence[tuple[AggregateKey, tuple[int, int, int, int, Decimal]]],
    returned_rows: Sequence[Mapping[str, Any]],
    week_start: date,
) -> tuple[int, int, int]:
    """Count from the locked pre-image, then require RETURNING to match.

    PostgreSQL ``xmax`` is not used. It is a system column, not a documented
    insert/update flag, and a preceding ``FOR UPDATE`` in the same transaction
    makes that inference unreliable. ``RETURNING`` is used only to confirm which
    logical keys the UPSERT actually inserted or updated.
    """
    published = dict(published_kpis)
    inserted_keys: list[AggregateKey] = []
    updated_keys: list[AggregateKey] = []
    unchanged = 0
    for aggregate in aggregates:
        key = AggregateKey(aggregate.warehouse, aggregate.client_id, aggregate.week_start)
        current = published.get(key)
        proposed = _kpi_tuple(aggregate)
        if current is None:
            inserted_keys.append(key)
        elif current != proposed:
            updated_keys.append(key)
        else:
            unchanged += 1
    returned = [_published_key(row, week_start) for row in returned_rows]
    if len(returned) != len(set(returned)):
        raise ValueError("RETURNING contained a duplicate logical key")
    if sorted(returned) != sorted(inserted_keys + updated_keys):
        raise ValueError("RETURNING keys do not match the locked snapshot classification")
    return len(inserted_keys), len(updated_keys), unchanged


def _mark_unusable(connection: Any) -> None:
    """Remember an aborted connection without retaining a reusable object id."""
    if connection is None:
        return
    try:
        _UNUSABLE_CONNECTIONS.add(connection)
        return
    except TypeError:
        pass
    info = getattr(connection, "info", None)
    if isinstance(info, dict):
        info[_UNUSABLE_FLAG] = True
        return
    try:
        setattr(connection, _UNUSABLE_FLAG, True)
    except (AttributeError, TypeError):
        return


def _connection_unusable(connection: Any) -> bool:
    if connection is None:
        return False
    try:
        if connection in _UNUSABLE_CONNECTIONS:
            return True
    except TypeError:
        pass
    info = getattr(connection, "info", None)
    if isinstance(info, dict) and info.get(_UNUSABLE_FLAG) is True:
        return True
    return getattr(connection, _UNUSABLE_FLAG, False) is True


def _close_failed_transaction(
    connection: Any,
    *,
    connection_failure: bool,
    rollback_observed: bool,
) -> str:
    """Do not present an aborted transaction as reusable or committed."""
    if connection_failure:
        _mark_unusable(connection)
        return STATUS_CONNECTION_ERROR
    if rollback_observed:
        return STATUS_ROLLED_BACK
    _mark_unusable(connection)
    return STATUS_ROLLBACK_FAILED


def _database_issue(failure: _StatementFailure, *, phase: str) -> PublicationIssue:
    exc = failure.exc
    if _is_connection_failure(exc):
        code = "connection_error"
    elif isinstance(exc, IntegrityError):
        code = "integrity_error"
    elif isinstance(exc, (DataError, ProgrammingError)):
        code = "write_error"
    elif phase == "upsert":
        code = "write_error"
    else:
        code = "database_error"
    return PublicationIssue(code, f"{phase}: {exc}", sqlstate=_sqlstate(exc))


def _status_for_issues(issues: Sequence[PublicationIssue]) -> str:
    if any(issue.code == "connection_error" for issue in issues):
        return STATUS_CONNECTION_ERROR
    return STATUS_BLOCKED


def _is_connection_failure(exc: BaseException) -> bool:
    if isinstance(exc, (DisconnectionError, InterfaceError)):
        return True
    sqlstate = _sqlstate(exc)
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _UNCERTAIN_SQLSTATES):
        return True
    text_value = str(exc).lower()
    return any(marker in text_value for marker in _CONNECTION_MARKERS)


def _commit_failure_is_uncertain(exc: BaseException) -> bool:
    if isinstance(exc, (IntegrityError, DataError, ProgrammingError, PendingRollbackError)):
        return False
    sqlstate = _sqlstate(exc)
    if sqlstate is not None:
        if sqlstate in _UNCERTAIN_SQLSTATES or sqlstate.startswith("08"):
            return True
        if sqlstate[:2] in {"22", "23", "40", "42"}:
            return False
    if isinstance(exc, (DisconnectionError, InterfaceError)):
        return True
    text_value = str(exc).lower()
    if any(marker in text_value for marker in _CONNECTION_MARKERS):
        return True
    if "timed out" in text_value or "timeout" in text_value:
        return True
    return isinstance(exc, OperationalError) or not isinstance(exc, SQLAlchemyError)


def _sqlstate(exc: BaseException) -> str | None:
    seen: set[int] = set()
    current: Any = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attr in ("pgcode", "sqlstate"):
            value = getattr(current, attr, None)
            if isinstance(value, str) and value:
                return value
        current = getattr(current, "orig", None)
    return None


def _rollback_quietly(connection: Any) -> bool:
    rollback = getattr(connection, "rollback", None)
    if not callable(rollback):
        return False
    try:
        rollback()
    except Exception:
        return False
    return True


def _assert_read_sql(sql: str) -> None:
    upper = sql.upper()
    without_row_lock = upper.replace("FOR UPDATE", "")
    if (
        "DELETE" in upper
        or "INSERT" in without_row_lock
        or "UPDATE" in without_row_lock
        or "TELEMETRY_EVENTS" in upper
    ):
        raise RuntimeError("published-key reconciliation must be a read of the destination table")


def _assert_sql_contract(sql: str) -> None:
    upper = sql.upper()
    if "DELETE" in upper or "TELEMETRY_EVENTS" in upper or "XMAX" in upper:
        raise RuntimeError("publication SQL must not delete rows, write telemetry, or infer counts from xmax")
    if "ON CONFLICT (WAREHOUSE, CLIENT_ID, WEEK_START)" not in upper:
        raise RuntimeError("upsert conflict target is missing")
    if upper.count("IS DISTINCT FROM") < len(KPI_VALUE_FIELDS):
        raise RuntimeError("upsert no longer updates only when a KPI changes")
    insert_columns = upper.split("VALUES", 1)[0]
    if "COMPUTED_AT" in insert_columns or " ID " in f" {insert_columns} ":
        raise RuntimeError("upsert must not assign id or computed_at on insert")


_assert_read_sql(PUBLISHED_KEYS_SQL_TEXT)
_assert_sql_contract(WEEKLY_UPSERT_SQL)
