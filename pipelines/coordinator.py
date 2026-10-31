"""Weekly publication coordinator.

This module sequences quality checks, a previously committed lease, and one
business transaction. It does not calculate KPIs, extract telemetry, acquire a
lease, or certify production coverage. A synthetic fixture can exercise the
technical path; ``production_certified`` stays false.

The protected sequence is verify, reconcile, UPSERT, publication metadata, and
lease release. Those statements share the caller's business transaction. The
coordinator commits that transaction once. Run history for a failure is written
on a separate operational connection so a business rollback cannot erase it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from pipelines.concurrency import (
    ConcurrencyError,
    release_week_lease,
    verify_week_lease,
)
from pipelines.publication import (
    STATUS_STAGED,
    PublicationIssue,
    _commit_failure_is_uncertain,
    _stage_weekly_upsert,
    blocking_quality_issues,
    rollback_publication_transaction,
    validate_weekly_publication_batch,
)
from pipelines.run_store import (
    COMMIT_ROLLED_BACK,
    COMMIT_TECHNICALLY_OBSERVED,
    COMMIT_UNCERTAIN,
    COMMIT_UNCOMMITTED,
    STATUS_BLOCKED_QUALITY,
    STATUS_FAILED_RETRYABLE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED_IN_PROGRESS,
    STATUS_SUCCEEDED,
    STATUS_SUCCEEDED_NOOP,
    STATUS_SUPERSEDED,
    RunStoreError,
    get_run,
    record_commit_observation,
    record_valid_publication,
    update_run,
)
from pipelines.weekly_performance import QualityReport, WeeklyAggregate

REQUIRED_COVERAGE_EVENTS = frozenset(
    {
        "stock_threshold_triggered",
        "inventory_discrepancy_detected",
    }
)
SYNTHETIC_COVERAGE_SOURCE = "synthetic_fixture"
OPERATIONAL_COVERAGE_SOURCE = "operational_checkpoints"
FINGERPRINT_PARTS = ("telemetry=", "movements=", "stockexit=", "rules=")

STATUS_COMMIT_UNCERTAIN = "commit_uncertain"
_SUCCESS_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_SUCCEEDED_NOOP})


class CoordinatorError(RuntimeError):
    """A coordinator contract failure. Not a certified publication."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CoverageEvidence:
    """Structured coverage facts. A string, force flag, or Prefect state is not evidence."""

    confirmed_event_types: frozenset[str]
    dispatch_order_ids: tuple[tuple[str, str], ...]
    lookup_complete: bool
    source: str


@dataclass(frozen=True)
class ConfirmedLeaseRight:
    """Caller evidence that an earlier acquisition transaction committed.

    ``acquisition_committed`` is not a database proof. The business transaction
    still has to verify week, owner, token, and expiry. A lease observation with
    ``durable=False`` is not accepted in its place.
    """

    week_start: date
    run_id: UUID
    fencing_token: int
    acquisition_committed: bool


@dataclass(frozen=True)
class CoordinatorResult:
    """Publication outcome. Success requires an observed business COMMIT."""

    week_start: date
    run_id: UUID | None
    status: str
    errors: tuple[str, ...] = ()
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    commit_result: str = "not_attempted"
    publication_recorded: bool = False
    recovered: bool = False
    evidence_inconclusive: bool = False
    coverage_source: str | None = None

    @property
    def production_certified(self) -> bool:
        return False

    @property
    def business_publication_valid(self) -> bool:
        return (
            self.publication_recorded
            and self.status in _SUCCESS_STATUSES
            and self.commit_result == COMMIT_TECHNICALLY_OBSERVED
            and not self.evidence_inconclusive
        )


def publish_weekly_business_report(
    *,
    week_start: date,
    run_id: UUID,
    aggregates: Sequence[WeeklyAggregate],
    quality: QualityReport | None,
    coverage: CoverageEvidence | None,
    dependency_fingerprint: str | None,
    rules_version: str | None,
    lease: ConfirmedLeaseRight | None,
    business: Any,
    operational: Any,
    now: datetime,
    records_processed: int | None = None,
) -> CoordinatorResult:
    """Publish one elapsed ISO week inside one business transaction.

    ``operational`` records run history and is committed separately. ``business``
    is committed only after the protected sequence. Neither connection may be
    autocommit, and they must not be the same transaction.
    """
    _require_separated_transactions(business, operational)
    run = _identified_run(operational, run_id, week_start)
    issues = _pre_write_issues(
        week_start,
        aggregates,
        quality,
        coverage,
        dependency_fingerprint,
        rules_version,
        now,
    )
    if issues:
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_PENDING,
            status=STATUS_BLOCKED_QUALITY,
            now=now,
            stage_reached="quality",
            errors=issues,
            commit_result="not_attempted",
            block_reason=issues[0],
        )
        return _result(
            week_start,
            run.run_id,
            STATUS_BLOCKED_QUALITY,
            errors=issues,
            coverage=coverage,
        )

    if not _durable_claim(lease, week_start, run.run_id):
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_PENDING,
            status=STATUS_SUPERSEDED,
            now=now,
            stage_reached="lease",
            errors=("lease_not_durable",),
            commit_result="not_attempted",
            block_reason="lease acquisition was not confirmed before publication",
        )
        return _result(week_start, run.run_id, STATUS_SUPERSEDED, errors=("lease_not_durable",))

    try:
        decision = verify_week_lease(business, week_start, run.run_id, lease.fencing_token)
    except ConcurrencyError as exc:
        _release_business_lock(business)
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_PENDING,
            status=STATUS_FAILED_RETRYABLE,
            now=now,
            stage_reached="lease",
            errors=(exc.code,),
            commit_result=COMMIT_UNCERTAIN,
            error_code=exc.code,
            error_message=str(exc),
        )
        return _result(week_start, run.run_id, STATUS_FAILED_RETRYABLE, errors=(exc.code,))
    if not decision.lease_verified:
        _release_business_lock(business)
        status = _lease_rejection_status(decision)
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_PENDING,
            status=status,
            now=now,
            stage_reached="lease",
            errors=("lease_rejected",),
            commit_result="not_attempted",
            block_reason=f"lease verification rejected: {decision.status}",
        )
        return _result(week_start, run.run_id, status, errors=("lease_rejected",))

    try:
        _mark_running(operational, run.run_id)
    except CoordinatorError:
        _release_business_lock(business)
        raise
    staged = _stage_weekly_upsert(
        business,
        week_start,
        aggregates,
        now=now,
        quality_gate="coordinator_checked",
    )
    if staged.status != STATUS_STAGED:
        return _abort_staged(
            operational,
            business,
            week_start,
            run.run_id,
            staged.status,
            staged.errors,
            now,
            staged.rollback_observed,
        )

    try:
        record_valid_publication(
            business,
            week_start,
            run.run_id,
            published_at=now,
            published_row_count=staged.inserted + staged.updated + staged.unchanged,
            dependency_fingerprint=dependency_fingerprint,
            rules_version=rules_version,
        )
        released = release_week_lease(business, week_start, run.run_id, lease.fencing_token)
    except Exception as exc:
        _release_business_lock(business)
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_RUNNING,
            status=STATUS_FAILED_RETRYABLE,
            now=now,
            stage_reached="publish",
            errors=("publication_metadata_failed",),
            commit_result=COMMIT_ROLLED_BACK,
            error_code="publication_metadata_failed",
            error_message=str(exc)[:500],
        )
        return _result(
            week_start,
            run.run_id,
            STATUS_FAILED_RETRYABLE,
            errors=("publication_metadata_failed",),
            commit_result=COMMIT_ROLLED_BACK,
        )
    if released.status != "released":
        _release_business_lock(business)
        _finish_run(
            operational,
            run.run_id,
            expected_status=STATUS_RUNNING,
            status=STATUS_FAILED_RETRYABLE,
            now=now,
            stage_reached="lease",
            errors=("lease_release_rejected",),
            commit_result=COMMIT_ROLLED_BACK,
            error_code="lease_release_rejected",
            error_message="owner or fencing token no longer matches",
        )
        return _result(
            week_start,
            run.run_id,
            STATUS_FAILED_RETRYABLE,
            errors=("lease_release_rejected",),
            commit_result=COMMIT_ROLLED_BACK,
        )

    commit_result = _commit_business(business)
    if commit_result != COMMIT_TECHNICALLY_OBSERVED:
        _observe_uncertain(operational, run.run_id, commit_result)
        return _result(
            week_start,
            run.run_id,
            STATUS_COMMIT_UNCERTAIN,
            errors=("commit_uncertain",),
            inserted=staged.inserted,
            updated=staged.updated,
            unchanged=staged.unchanged,
            commit_result=commit_result,
            coverage=coverage,
        )

    final_status = (
        STATUS_SUCCEEDED_NOOP
        if staged.inserted == 0 and staged.updated == 0
        else STATUS_SUCCEEDED
    )
    _finish_run(
        operational,
        run.run_id,
        expected_status=STATUS_RUNNING,
        status=final_status,
        now=now,
        stage_reached="committed",
        errors=(),
        commit_result=COMMIT_TECHNICALLY_OBSERVED,
        inserted=staged.inserted,
        updated=staged.updated,
        unchanged=staged.unchanged,
        aggregates=len(aggregates),
        records_processed=records_processed,
    )
    return _result(
        week_start,
        run.run_id,
        final_status,
        inserted=staged.inserted,
        updated=staged.updated,
        unchanged=staged.unchanged,
        commit_result=COMMIT_TECHNICALLY_OBSERVED,
        publication_recorded=True,
        coverage=coverage,
    )


def resolve_uncertain_publication(
    connection: Any,
    *,
    week_start: date,
    run_id: UUID,
    dependency_fingerprint: str,
    rules_version: str,
    published_row_count: int | None = None,
    operational: Any | None = None,
    now: datetime | None = None,
    persist: bool = False,
) -> CoordinatorResult:
    """Recognize a committed publication from stored metadata. Does not UPSERT."""
    from pipelines.run_store import latest_valid_publication

    publication = latest_valid_publication(connection, week_start)
    matches = (
        publication is not None
        and publication.run_id == run_id
        and publication.dependency_fingerprint == dependency_fingerprint
        and publication.rules_version == rules_version
        and (
            published_row_count is None
            or publication.published_row_count == published_row_count
        )
    )
    if not matches:
        if persist and operational is not None and now is not None:
            _observe_uncertain(operational, run_id, COMMIT_UNCERTAIN)
        return _result(
            week_start,
            run_id,
            STATUS_COMMIT_UNCERTAIN,
            errors=("evidence_inconclusive",),
            commit_result=COMMIT_UNCERTAIN,
            evidence_inconclusive=True,
        )
    if persist:
        if operational is None or now is None:
            raise CoordinatorError("operational_required", "persisting recovery needs an operational transaction and now")
        current = get_run(operational, run_id)
        if current is not None and current.status == STATUS_RUNNING:
            _finish_run(
                operational,
                run_id,
                expected_status=STATUS_RUNNING,
                status=STATUS_SUCCEEDED,
                now=now,
                stage_reached="recovered",
                errors=(),
                commit_result=COMMIT_TECHNICALLY_OBSERVED,
            )
    return _result(
        week_start,
        run_id,
        STATUS_SUCCEEDED,
        commit_result=COMMIT_TECHNICALLY_OBSERVED,
        publication_recorded=True,
        recovered=True,
    )


def _pre_write_issues(
    week_start: date,
    aggregates: Sequence[WeeklyAggregate],
    quality: QualityReport | None,
    coverage: CoverageEvidence | None,
    dependency_fingerprint: str | None,
    rules_version: str | None,
    now: datetime,
) -> tuple[str, ...]:
    issues = [
        issue.code
        for issue in validate_weekly_publication_batch(week_start, aggregates, now=now)
    ]
    if not isinstance(quality, QualityReport):
        issues.append("quality_required")
    else:
        issues.extend(issue.code for issue in blocking_quality_issues(quality))
    issues.extend(_coverage_issues(coverage))
    if _fingerprint_issue(dependency_fingerprint, week_start):
        issues.append("fingerprint_incomplete")
    if not isinstance(rules_version, str) or not rules_version.strip():
        issues.append("rules_version_required")
    return tuple(dict.fromkeys(issues))


def _coverage_issues(coverage: CoverageEvidence | None) -> tuple[str, ...]:
    if not isinstance(coverage, CoverageEvidence):
        return ("coverage_evidence_required",)
    if coverage.source not in {
        SYNTHETIC_COVERAGE_SOURCE,
        OPERATIONAL_COVERAGE_SOURCE,
    } or coverage.lookup_complete is not True:
        return ("coverage_insufficient",)
    confirmed = coverage.confirmed_event_types
    if not isinstance(confirmed, frozenset) or not REQUIRED_COVERAGE_EVENTS <= confirmed:
        return ("coverage_insufficient",)
    pairs = coverage.dispatch_order_ids
    if not isinstance(pairs, tuple):
        return ("dispatch_order_not_unique",)
    movements = []
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            return ("dispatch_order_not_unique",)
        movement, order = pair
        if not isinstance(movement, str) or not isinstance(order, str) or not movement or not order:
            return ("dispatch_order_not_unique",)
        movements.append(movement)
    if len(movements) != len(set(movements)):
        return ("dispatch_order_not_unique",)
    return ()


def _fingerprint_issue(value: str | None, week_start: date) -> bool:
    if not isinstance(value, str):
        return True
    compact = value.strip()
    if len(compact) < 32 or compact == week_start.isoformat() or compact.startswith("MAX("):
        return True
    return any(part not in compact for part in FINGERPRINT_PARTS)


def _durable_claim(
    lease: ConfirmedLeaseRight | None,
    week_start: date,
    run_id: UUID,
) -> bool:
    return (
        isinstance(lease, ConfirmedLeaseRight)
        and lease.acquisition_committed is True
        and lease.week_start == week_start
        and lease.run_id == run_id
        and isinstance(lease.fencing_token, int)
        and not isinstance(lease.fencing_token, bool)
        and lease.fencing_token >= 0
    )


def _lease_rejection_status(decision: Any) -> str:
    if decision.owner_run_id is not None and decision.owner_run_id != decision.run_id:
        return STATUS_SKIPPED_IN_PROGRESS
    return STATUS_SUPERSEDED


def _identified_run(connection: Any, run_id: UUID, week_start: date) -> Any:
    try:
        run = get_run(connection, run_id)
    except RunStoreError as exc:
        raise CoordinatorError(exc.code, str(exc)) from exc
    if run is None:
        raise CoordinatorError("run_not_found", "publication requires an existing run")
    if run.target_week_start != week_start:
        raise CoordinatorError("week_mismatch", "run target week does not match the publication week")
    if run.status != STATUS_PENDING:
        raise CoordinatorError("status_conflict", "publication requires a pending run")
    return run


def _require_separated_transactions(business: Any, operational: Any) -> None:
    if business is None or operational is None or business is operational:
        raise CoordinatorError(
            "transaction_separation_required",
            "business and operational transactions must be distinct",
        )
    for connection, name in ((business, "business"), (operational, "operational")):
        if not hasattr(connection, "execute") or not hasattr(connection, "commit"):
            raise CoordinatorError("transaction_required", f"{name} connection is incomplete")
        options: Mapping[str, Any] = {}
        getter = getattr(connection, "get_execution_options", None)
        if callable(getter):
            loaded = getter() or {}
            if isinstance(loaded, Mapping):
                options = loaded
        if str(options.get("isolation_level", "")).upper() == "AUTOCOMMIT":
            raise CoordinatorError("autocommit_forbidden", f"{name} connection must not autocommit")
        in_transaction = getattr(connection, "in_transaction", None)
        if not callable(in_transaction) or not in_transaction():
            raise CoordinatorError("transaction_required", f"{name} connection has no open transaction")


def _mark_running(connection: Any, run_id: UUID) -> None:
    update_run(
        connection,
        run_id,
        expected_status=STATUS_PENDING,
        status=STATUS_RUNNING,
        stage_reached="publish",
        commit_result=COMMIT_UNCOMMITTED,
    )
    _commit_operational(connection)


def _finish_run(
    connection: Any,
    run_id: UUID,
    *,
    expected_status: str,
    status: str,
    now: datetime,
    stage_reached: str,
    errors: tuple[str, ...],
    commit_result: str,
    block_reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    inserted: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    aggregates: int | None = None,
    records_processed: int | None = None,
) -> None:
    _begin_operational_transaction(connection)
    update_run(
        connection,
        run_id,
        expected_status=expected_status,
        status=status,
        finished_at=now,
        stage_reached=stage_reached,
        records_processed=records_processed,
        aggregates_calculated=aggregates,
        rows_inserted=inserted,
        rows_updated=updated,
        rows_unchanged=unchanged,
        error_code=error_code or (errors[0] if status == STATUS_FAILED_RETRYABLE and errors else None),
        error_message=error_message,
        block_reason=block_reason,
        commit_result=commit_result,
    )
    _commit_operational(connection)


def _abort_staged(
    operational: Any,
    business: Any,
    week_start: date,
    run_id: UUID,
    staged_status: str,
    errors: tuple[PublicationIssue, ...],
    now: datetime,
    rollback_observed: bool,
) -> CoordinatorResult:
    codes = tuple(issue.code for issue in errors) or (staged_status,)
    if not rollback_observed:
        _release_business_lock(business)
    blocked = staged_status == "blocked" or any(
        code in {"reconciliation_conflict", "empty_batch_not_certified", "quality_blocked"}
        for code in codes
    )
    status = STATUS_BLOCKED_QUALITY if blocked else STATUS_FAILED_RETRYABLE
    commit_result = COMMIT_ROLLED_BACK if rollback_observed or blocked else COMMIT_UNCERTAIN
    _finish_run(
        operational,
        run_id,
        expected_status=STATUS_RUNNING,
        status=status,
        now=now,
        stage_reached="publish",
        errors=codes,
        commit_result=commit_result,
        block_reason=codes[0] if status == STATUS_BLOCKED_QUALITY else None,
        error_code=None if status == STATUS_BLOCKED_QUALITY else codes[0],
        error_message=errors[0].message if errors else staged_status,
    )
    return _result(
        week_start,
        run_id,
        status,
        errors=codes,
        commit_result=commit_result,
    )


def _observe_uncertain(connection: Any, run_id: UUID, commit_result: str) -> None:
    _begin_operational_transaction(connection)
    record_commit_observation(
        connection,
        run_id,
        commit_result=COMMIT_UNCERTAIN if commit_result == COMMIT_UNCERTAIN else commit_result,
        stage_reached="commit",
        error_code="commit_uncertain",
        error_message="business commit was not confirmed; writes were not retried",
    )
    _commit_operational(connection)


def _commit_business(connection: Any) -> str:
    try:
        connection.commit()
    except Exception as exc:
        if _commit_failure_is_uncertain(exc):
            return COMMIT_UNCERTAIN
        return COMMIT_ROLLED_BACK
    return COMMIT_TECHNICALLY_OBSERVED


def _commit_operational(connection: Any) -> None:
    try:
        connection.commit()
    except Exception as exc:
        raise CoordinatorError("operational_commit_failed", "run history could not be committed") from exc


def _begin_operational_transaction(connection: Any) -> None:
    in_transaction = getattr(connection, "in_transaction", None)
    if callable(in_transaction) and not in_transaction():
        begin = getattr(connection, "begin", None)
        if not callable(begin):
            raise CoordinatorError(
                "transaction_required",
                "operational connection cannot start a transaction",
            )
        begin()


def _release_business_lock(connection: Any) -> None:
    rollback_publication_transaction(connection)


def _result(
    week_start: date,
    run_id: UUID | None,
    status: str,
    *,
    errors: tuple[str, ...] = (),
    inserted: int = 0,
    updated: int = 0,
    unchanged: int = 0,
    commit_result: str = "not_attempted",
    publication_recorded: bool = False,
    recovered: bool = False,
    evidence_inconclusive: bool = False,
    coverage: CoverageEvidence | None = None,
) -> CoordinatorResult:
    return CoordinatorResult(
        week_start=week_start,
        run_id=run_id,
        status=status,
        errors=errors,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        commit_result=commit_result,
        publication_recorded=publication_recorded,
        recovered=recovered,
        evidence_inconclusive=evidence_inconclusive,
        coverage_source=None if coverage is None else coverage.source,
    )
