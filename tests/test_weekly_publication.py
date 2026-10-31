"""Unit tests for weekly publication staging.

PostgreSQL transaction semantics are not claimed by the fake connection.
Those cases stay skipped unless TRACKFLOW_PUBLICATION_TEST_DATABASE_URL points
at an isolated disposable database. DATABASE_URL is intentionally ignored.
"""

from __future__ import annotations

import inspect
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from pipelines.publication import (
    CANONICAL_WAREHOUSES,
    DESTINATION_TABLE,
    PUBLISHED_KEYS_SQL_TEXT,
    WEEKLY_UPSERT_SQL,
    PublicationIssue,
    attempt_technical_commit,
    blocking_quality_issues,
    build_weekly_upsert_statement,
    official_discrepancy_rate,
    reconcile_disappeared_keys,
    rollback_publication_transaction,
    _commit_failure_is_uncertain,
    _stage_weekly_upsert,
    stage_weekly_upsert,
    validate_weekly_publication_batch,
)
from pipelines.weekly_performance import (
    POSTGRES_INTEGER_MAX,
    AggregateKey,
    QualityIssue,
    QualityReport,
    WeeklyAggregate,
)

WEEK = date(2026, 10, 19)
COMPLETE_NOW = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
CLIENT_A = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
CLIENT_B = "legacy-client"
POSTGRES_SKIP_REASON = (
    "Pending isolated PostgreSQL. Set TRACKFLOW_PUBLICATION_TEST_DATABASE_URL "
    "to a disposable test database. DATABASE_URL is not used."
)


class MappingResult:
    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class FakeConnection:
    """Records statements. It does not implement PostgreSQL."""

    def __init__(
        self,
        *,
        published=(),
        returning=(),
        error=None,
        fail_on=None,
        commit_error=None,
        rollback_error=None,
        autocommit=False,
        dialect="postgresql",
        in_transaction=True,
    ):
        self.published = list(published)
        self.returning = list(returning)
        self.error = error
        self.fail_on = fail_on
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.autocommit = autocommit
        self.dialect = SimpleNamespace(name=dialect)
        self.open = in_transaction
        self.statements = []
        self.parameters = []
        self.committed = False
        self.rolled_back = False

    def in_transaction(self):
        return self.open

    def get_execution_options(self):
        if self.autocommit:
            return {"isolation_level": "AUTOCOMMIT"}
        return {}

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(parameters or {})
        if self.error is not None and (self.fail_on is None or self.fail_on in sql):
            raise self.error
        if "INSERT INTO" in sql:
            return MappingResult(self.returning)
        return MappingResult(self.published)

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True
        self.open = False

    def rollback(self):
        if self.rollback_error is not None:
            raise self.rollback_error
        self.rolled_back = True
        self.open = False


def aggregate(**overrides) -> WeeklyAggregate:
    values = {
        "warehouse": "los_angeles",
        "client_id": CLIENT_A,
        "week_start": WEEK,
        "inbound_units_count": 3,
        "outbound_orders_count": 2,
        "stockout_events_count": 1,
        "discrepancy_events_count": 1,
    }
    values.update(overrides)
    if "discrepancy_rate" not in overrides:
        outbound = values["outbound_orders_count"]
        discrepancies = values["discrepancy_events_count"]
        if (
            isinstance(outbound, int)
            and not isinstance(outbound, bool)
            and isinstance(discrepancies, int)
            and not isinstance(discrepancies, bool)
            and outbound >= 0
            and discrepancies >= 0
        ):
            values["discrepancy_rate"] = official_discrepancy_rate(outbound, discrepancies)
        else:
            values["discrepancy_rate"] = Decimal(0)
    return WeeklyAggregate(**values)


def published_row(item: WeeklyAggregate | None = None, **overrides):
    source = aggregate() if item is None else item
    row = {
        "warehouse": source.warehouse,
        "client_id": source.client_id,
        "inbound_units_count": source.inbound_units_count,
        "outbound_orders_count": source.outbound_orders_count,
        "stockout_events_count": source.stockout_events_count,
        "discrepancy_events_count": source.discrepancy_events_count,
        "discrepancy_rate": source.discrepancy_rate,
    }
    row.update(overrides)
    return row


def returned_key(item: WeeklyAggregate):
    return {"warehouse": item.warehouse, "client_id": item.client_id}


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def staged_connection(**kwargs) -> FakeConnection:
    return FakeConnection(**kwargs)


def test_upsert_sql_is_idempotent_and_does_not_rewrite_identity():
    sql = WEEKLY_UPSERT_SQL.upper()
    assert f"INSERT INTO {DESTINATION_TABLE.upper()}" in sql
    assert "ON CONFLICT (WAREHOUSE, CLIENT_ID, WEEK_START)" in sql
    assert sql.count("IS DISTINCT FROM") == 5
    assert "DELETE" not in sql
    assert "TELEMETRY_EVENTS" not in sql
    insert_columns = sql.split("VALUES", 1)[0]
    assert "COMPUTED_AT" not in insert_columns
    assert " ID " not in f" {insert_columns} "
    update_clause = sql.split("DO UPDATE SET", 1)[1].split("WHERE", 1)[0]
    assert "COMPUTED_AT = STATEMENT_TIMESTAMP()" in update_clause
    assert "INBOUND_UNITS_COUNT = EXCLUDED.INBOUND_UNITS_COUNT" in update_clause
    assert "DELETE" not in PUBLISHED_KEYS_SQL_TEXT.upper()
    assert "FOR UPDATE" in PUBLISHED_KEYS_SQL_TEXT.upper()
    assert "XMAX" not in sql
    assert "RETURNING WAREHOUSE, CLIENT_ID" in sql


def test_module_cannot_declare_business_publication_success():
    import pipelines.publication as publication

    forbidden = {
        "publish",
        "publish_week",
        "certify_publication",
        "force_publish",
        "skip_validation",
    }
    assert forbidden.isdisjoint(set(dir(publication)))
    parameters = inspect.signature(stage_weekly_upsert).parameters
    assert "skip_quality" not in parameters
    assert "force" not in parameters
    assert "certify" not in parameters
    connection = staged_connection(returning=[returned_key(aggregate())])
    outcome = stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert outcome.status == "blocked"
    assert outcome.quality_gate == "missing"
    assert "quality_required" in codes(outcome.errors)
    assert outcome.business_certified is False
    assert outcome.committed is False
    assert outcome.staged_uncommitted is False
    assert connection.statements == []
    assert connection.committed is False


def test_insert_update_and_unchanged_counts_come_from_returning():
    new_row = aggregate()
    changed = aggregate(warehouse="zaragoza", client_id=CLIENT_B, inbound_units_count=4)
    unchanged_row = aggregate(warehouse="zaragoza", client_id=CLIENT_A, inbound_units_count=5)
    connection = staged_connection(
        published=[
            published_row(changed, inbound_units_count=1),
            published_row(unchanged_row),
        ],
        returning=[returned_key(new_row), returned_key(changed)],
    )
    batch = (new_row, changed, unchanged_row)
    outcome = _stage_weekly_upsert(connection, WEEK, batch, now=COMPLETE_NOW)
    assert outcome.status == "staged"
    assert outcome.inserted == 1
    assert outcome.updated == 1
    assert outcome.unchanged == 1
    assert outcome.committed is False
    assert connection.committed is False
    assert len(connection.statements) == 2
    assert "INSERT INTO" in connection.statements[1]
    assert CLIENT_B in connection.parameters[1].values()
    assert "DROP TABLE" not in connection.statements[1]


def test_identical_repeat_counts_as_unchanged_without_committing():
    item = aggregate()
    connection = staged_connection(published=[published_row(item)], returning=[])
    outcome = _stage_weekly_upsert(connection, WEEK, (item,), now=COMPLETE_NOW)
    assert outcome.inserted == 0
    assert outcome.updated == 0
    assert outcome.unchanged == 1
    assert outcome.committed is False
    assert connection.committed is False


def test_different_clients_and_warehouses_are_distinct_keys():
    first = aggregate()
    second = aggregate(warehouse="zaragoza", client_id=CLIENT_B)
    connection = staged_connection(returning=[returned_key(first), returned_key(second)])
    outcome = _stage_weekly_upsert(
        connection,
        WEEK,
        (first, second),
        now=COMPLETE_NOW,
    )
    assert outcome.status == "staged"
    assert outcome.inserted == 2
    assert outcome.errors == ()
    assert CANONICAL_WAREHOUSES == frozenset({"los_angeles", "zaragoza"})


def test_invalid_week_is_rejected_before_any_sql():
    connection = staged_connection()
    tuesday = WEEK + timedelta(days=1)
    outcome = stage_weekly_upsert(connection, tuesday, (aggregate(week_start=tuesday),), now=COMPLETE_NOW)
    assert outcome.status == "blocked"
    assert "invalid_week_start" in codes(outcome.errors)
    assert connection.statements == []
    incomplete = stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(),),
        now=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    assert "incomplete_week" in codes(incomplete.errors)
    assert connection.statements == []


def test_duplicate_keys_negative_overflow_and_bad_rate_block_writes():
    connection = staged_connection()
    duplicate = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(), aggregate(inbound_units_count=9)),
        now=COMPLETE_NOW,
    )
    negative = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(stockout_events_count=-1),),
        now=COMPLETE_NOW,
    )
    overflow = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(inbound_units_count=POSTGRES_INTEGER_MAX + 1),),
        now=COMPLETE_NOW,
    )
    bad_rate = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(discrepancy_rate=Decimal("0.10")),),
        now=COMPLETE_NOW,
    )
    float_rate = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(discrepancy_rate=0.5),),
        now=COMPLETE_NOW,
    )
    assert codes(duplicate.errors) == {"duplicate_aggregate_key"}
    assert "negative_counter" in codes(negative.errors)
    assert "counter_overflow" in codes(overflow.errors)
    assert "discrepancy_rate_mismatch" in codes(bad_rate.errors)
    assert "discrepancy_rate_type" in codes(float_rate.errors)
    assert connection.statements == []
    limit = validate_weekly_publication_batch(
        WEEK,
        (aggregate(inbound_units_count=POSTGRES_INTEGER_MAX),),
        now=COMPLETE_NOW,
    )
    assert limit == ()


def test_aliases_and_foreign_weeks_are_not_rewritten():
    issues = validate_weekly_publication_batch(
        WEEK,
        (
            aggregate(warehouse="LA"),
            aggregate(warehouse="zaragoza", client_id="   "),
            aggregate(week_start=date(2026, 10, 12)),
        ),
        now=COMPLETE_NOW,
    )
    assert "noncanonical_warehouse" in codes(issues)
    assert "invalid_client_id" in codes(issues)
    assert "week_mismatch" in codes(issues)


def test_empty_batch_is_not_a_certified_inactive_week():
    connection = staged_connection(published=[])
    outcome = stage_weekly_upsert(connection, WEEK, (), now=COMPLETE_NOW)
    assert outcome.status == "blocked"
    assert outcome.business_certified is False
    assert "empty_batch_not_certified" in codes(outcome.errors)
    assert outcome.disappeared_keys == ()
    assert outcome.inserted == 0
    assert all("INSERT INTO" not in sql for sql in connection.statements)
    disappeared = _stage_weekly_upsert(
        staged_connection(published=[published_row()]),
        WEEK,
        (),
        now=COMPLETE_NOW,
    )
    assert disappeared.status == "blocked"
    assert disappeared.business_certified is False
    assert "empty_batch_not_certified" in codes(disappeared.errors)
    assert "reconciliation_conflict" in codes(disappeared.errors)
    assert disappeared.disappeared_keys == (AggregateKey("los_angeles", CLIENT_A, WEEK),)


def test_reconciliation_reports_disappeared_keys_and_blocks_writes():
    missing = AggregateKey("zaragoza", CLIENT_B, WEEK)
    connection = staged_connection(
        published=[
            published_row(),
            published_row(aggregate(warehouse="zaragoza", client_id=CLIENT_B)),
        ]
    )
    outcome = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert outcome.status == "blocked"
    assert outcome.disappeared_keys == (missing,)
    assert "reconciliation_conflict" in codes(outcome.errors)
    assert outcome.writes_attempted is False
    assert all("INSERT INTO" not in sql for sql in connection.statements)
    assert "DELETE" not in connection.statements[0].upper()
    direct = reconcile_disappeared_keys(connection, WEEK, (aggregate(),))
    assert direct.read_completed is True
    assert missing in direct.disappeared_keys


def test_quality_diagnostics_block_before_sql():
    connection = staged_connection()
    quality = QualityReport(
        accepted_event_ids=(),
        rejected_event_ids=(),
        duplicate_event_ids_consolidated=(),
        duplicate_movement_keys_consolidated=(),
        duplicate_movement_event_ids_consolidated=(),
        conflict_movement_keys=(),
        issues=(
            QualityIssue(
                "aggregate_integer_overflow",
                "inbound_units_count exceeds PostgreSQL integer range",
            ),
        ),
        cross_week_verification_pending=(),
        coverage_pending=("coverage is unverified",),
    )
    outcome = stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(),),
        now=COMPLETE_NOW,
        quality=quality,
    )
    assert outcome.status == "blocked"
    assert outcome.quality_gate == "blocked"
    assert "quality_blocked" in codes(outcome.errors)
    assert "coverage_insufficient" in codes(blocking_quality_issues(quality))
    assert connection.statements == []


def test_connection_error_is_identifiable_and_not_certified():
    connection = staged_connection(
        error=OperationalError("SELECT", {}, Exception("could not connect to server")),
        fail_on="SELECT",
    )
    outcome = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert outcome.status == "connection_error"
    assert "connection_error" in codes(outcome.errors)
    assert outcome.business_certified is False
    assert outcome.committed is False
    assert outcome.writes_attempted is False


def test_write_error_rolls_back_the_open_transaction():
    connection = staged_connection(
        error=IntegrityError("INSERT", {}, Exception("duplicate key")),
        fail_on="INSERT INTO",
    )
    outcome = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert outcome.status == "rolled_back"
    assert outcome.rollback_observed is True
    assert outcome.writes_attempted is True
    assert outcome.committed is False
    assert outcome.inserted == 0
    assert connection.rolled_back is True
    assert connection.committed is False
    assert "integrity_error" in codes(outcome.errors)


def test_explicit_rollback_does_not_certify_staged_work():
    connection = staged_connection()
    observation = rollback_publication_transaction(connection)
    assert observation.status == "rolled_back"
    assert observation.rollback_observed is True
    assert observation.business_certified is False
    assert connection.rolled_back is True


def test_uncertain_commit_does_not_assume_rollback_or_retry():
    lost = OperationalError(
        "COMMIT",
        {},
        Exception("server closed the connection unexpectedly"),
    )
    assert _commit_failure_is_uncertain(lost) is True
    connection = staged_connection(commit_error=lost)
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert observation.error is not None
    assert observation.error.code == "commit_unauthorized"
    assert observation.rollback_observed is False
    assert connection.rolled_back is False
    assert connection.committed is False
    assert connection.statements == []
    assert observation.business_certified is False


def test_definite_commit_failure_is_not_retried_or_certified():
    definite = IntegrityError("COMMIT", {}, Exception("deferred constraint"))
    assert _commit_failure_is_uncertain(definite) is False
    connection = staged_connection(commit_error=definite)
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert observation.business_certified is False
    assert connection.rolled_back is False
    assert connection.committed is False
    assert connection.statements == []


def test_technical_commit_is_not_business_certification():
    item = aggregate()
    connection = staged_connection(returning=[returned_key(item)])
    staged = _stage_weekly_upsert(connection, WEEK, (item,), now=COMPLETE_NOW)
    assert staged.status == "staged"
    assert staged.committed is False
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert observation.business_certified is False
    assert staged.business_certified is False
    assert connection.committed is False


def test_blocked_batch_cannot_be_confirmed():
    connection = staged_connection()
    blocked = stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert blocked.status == "blocked"
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert connection.committed is False
    assert connection.statements == []


def test_autocommit_and_closed_transaction_are_refused():
    autocommit = staged_connection(autocommit=True)
    closed = staged_connection(in_transaction=False)
    refused = _stage_weekly_upsert(autocommit, WEEK, (aggregate(),), now=COMPLETE_NOW)
    not_open = _stage_weekly_upsert(closed, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert "autocommit_forbidden" in codes(refused.errors)
    assert "transaction_required" in codes(not_open.errors)
    assert autocommit.statements == []
    assert closed.statements == []


def test_unreadable_returning_does_not_invent_counts():
    connection = staged_connection(returning=[{"inserted": "yes"}])
    outcome = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert outcome.status == "rolled_back"
    assert outcome.inserted == 0
    assert outcome.updated == 0
    assert outcome.unchanged == 0
    assert "upsert_result_unreadable" in codes(outcome.errors)


def test_multi_row_statement_uses_bound_parameters_only():
    statement = build_weekly_upsert_statement(2)
    sql = str(statement)
    assert ":warehouse_0" in sql
    assert ":warehouse_1" in sql
    assert sql.upper().count("ON CONFLICT") == 1
    hostile = "client'); DELETE FROM reporting.weekly_warehouse_client_performance; --"
    connection = staged_connection(returning=[{"warehouse": "los_angeles", "client_id": hostile}])
    outcome = _stage_weekly_upsert(
        connection,
        WEEK,
        (aggregate(client_id=hostile),),
        now=COMPLETE_NOW,
    )
    assert outcome.status == "staged"
    assert hostile not in connection.statements[1]
    assert hostile in connection.parameters[1].values()


def test_interface_error_on_commit_is_uncertain():
    lost = InterfaceError("COMMIT", {}, Exception("connection already closed"))
    assert _commit_failure_is_uncertain(lost) is True
    connection = staged_connection(commit_error=lost)
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert connection.committed is False
    assert connection.rolled_back is False


def test_publication_issue_keeps_an_identifiable_code():
    issue = PublicationIssue("reconciliation_conflict", "missing key", sqlstate=None)
    assert issue.code == "reconciliation_conflict"


def _quality(**overrides) -> QualityReport:
    values = {
        "accepted_event_ids": (),
        "rejected_event_ids": (),
        "duplicate_event_ids_consolidated": (),
        "duplicate_movement_keys_consolidated": (),
        "duplicate_movement_event_ids_consolidated": (),
        "conflict_movement_keys": (),
        "issues": (),
        "cross_week_verification_pending": (),
        "coverage_pending": (),
    }
    values.update(overrides)
    return QualityReport(**values)


def test_quality_gate_matrix_blocks_diagnostics_and_stages_only_clear_reports():
    item = aggregate()
    blocked = staged_connection(returning=[returned_key(item)])
    missing = stage_weekly_upsert(blocked, WEEK, (item,), now=COMPLETE_NOW, quality=None)
    conflicts = stage_weekly_upsert(
        blocked,
        WEEK,
        (item,),
        now=COMPLETE_NOW,
        quality=_quality(conflict_movement_keys=(("outbound_order_created", "move-1"),)),
    )
    pending = stage_weekly_upsert(
        blocked,
        WEEK,
        (item,),
        now=COMPLETE_NOW,
        quality=_quality(coverage_pending=("coverage_pending",)),
    )
    overflow = stage_weekly_upsert(
        blocked,
        WEEK,
        (item,),
        now=COMPLETE_NOW,
        quality=_quality(issues=(QualityIssue("aggregate_integer_overflow", "counter exceeds integer"),)),
    )
    assert missing.status == "blocked"
    assert missing.quality_gate == "missing"
    assert "quality_required" in codes(missing.errors)
    assert conflicts.status == "blocked"
    assert "identity_conflict" in codes(conflicts.errors)
    assert pending.status == "blocked"
    assert "coverage_insufficient" in codes(pending.errors)
    assert overflow.status == "blocked"
    assert "quality_blocked" in codes(overflow.errors)
    assert blocked.statements == []
    for outcome in (missing, conflicts, pending, overflow):
        assert outcome.business_certified is False
        assert outcome.writes_attempted is False
        assert outcome.committed is False

    clear = staged_connection(returning=[returned_key(item)])
    staged = stage_weekly_upsert(clear, WEEK, (item,), now=COMPLETE_NOW, quality=_quality())
    assert staged.status == "staged"
    assert staged.quality_gate == "diagnostics_clear"
    assert staged.inserted == 1
    assert staged.business_certified is False
    assert staged.committed is False
    assert staged.staged_uncommitted is True
    observation = attempt_technical_commit(clear)
    assert observation.status == "commit_unauthorized"
    assert observation.business_certified is False
    assert clear.committed is False

    empty = stage_weekly_upsert(
        staged_connection(published=[published_row(item)]),
        WEEK,
        (),
        now=COMPLETE_NOW,
        quality=_quality(),
    )
    assert empty.status == "blocked"
    assert empty.business_certified is False
    assert "empty_batch_not_certified" in codes(empty.errors)
    assert "reconciliation_conflict" in codes(empty.errors)


def test_counts_ignore_xmax_and_use_the_locked_snapshot():
    item = aggregate()
    connection = staged_connection(
        published=[],
        returning=[{**returned_key(item), "inserted": False, "xmax": 99}],
    )
    outcome = _stage_weekly_upsert(connection, WEEK, (item,), now=COMPLETE_NOW)
    assert outcome.status == "staged"
    assert outcome.inserted == 1
    assert outcome.updated == 0
    assert outcome.unchanged == 0
    assert "xmax" not in connection.statements[1].lower()
    assert outcome.business_certified is False


def test_failed_rollback_is_not_reusable_or_committable():
    connection = staged_connection(
        error=IntegrityError("INSERT", {}, Exception("duplicate key")),
        fail_on="INSERT INTO",
        rollback_error=RuntimeError("rollback failed"),
    )
    failed = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert failed.status == "rollback_failed"
    assert failed.rollback_observed is False
    assert failed.committed is False
    before = len(connection.statements)
    reused = _stage_weekly_upsert(connection, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert reused.status == "blocked"
    assert "transaction_aborted" in codes(reused.errors)
    assert len(connection.statements) == before
    observation = attempt_technical_commit(connection)
    assert observation.status == "commit_unauthorized"
    assert observation.error is not None
    assert observation.error.code == "transaction_aborted"
    assert connection.committed is False


def test_new_connection_does_not_inherit_an_aborted_connection():
    import gc

    import pipelines.publication as publication

    aborted = staged_connection(
        error=IntegrityError("INSERT", {}, Exception("duplicate key")),
        fail_on="INSERT INTO",
        rollback_error=RuntimeError("rollback failed"),
    )
    failed = _stage_weekly_upsert(aborted, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert failed.status == "rollback_failed"
    assert publication._connection_unusable(aborted) is True
    reused = _stage_weekly_upsert(aborted, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert "transaction_aborted" in codes(reused.errors)

    class SlottedConnection:
        __slots__ = ("info", "dialect", "open", "statements", "parameters", "committed")

        def __init__(self):
            self.info = {}
            self.dialect = SimpleNamespace(name="postgresql")
            self.open = True
            self.statements = []
            self.parameters = []
            self.committed = False

        def in_transaction(self):
            return self.open

        def get_execution_options(self):
            return {}

        def execute(self, statement, parameters=None):
            self.statements.append(str(statement))
            self.parameters.append(parameters or {})
            return MappingResult([returned_key(aggregate())])

        def rollback(self):
            raise RuntimeError("rollback failed")

    slotted = SlottedConnection()
    publication._mark_unusable(slotted)
    assert publication._connection_unusable(slotted) is True
    assert slotted.info["trackflow_publication_unusable"] is True
    fresh_slotted = SlottedConnection()
    assert publication._connection_unusable(fresh_slotted) is False

    del aborted
    gc.collect()
    replacement = staged_connection(returning=[returned_key(aggregate())])
    staged = _stage_weekly_upsert(replacement, WEEK, (aggregate(),), now=COMPLETE_NOW)
    assert staged.status == "staged"
    assert staged.inserted == 1
    assert publication._connection_unusable(replacement) is False
    assert not hasattr(publication, "_UNUSABLE_CONNECTION_IDS")


@pytest.mark.skipif(
    not os.getenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"),
    reason=POSTGRES_SKIP_REASON,
)
def test_postgres_insert_update_idempotent_id_and_computed_at():
    """Real PostgreSQL coverage. Not executed without an isolated test URL."""
    from sqlalchemy import create_engine, text

    url = os.environ["TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"]
    if url == os.getenv("DATABASE_URL"):
        pytest.fail("Refusing to reuse DATABASE_URL as the publication test database")
    engine = create_engine(url)
    original = aggregate()
    changed = aggregate(inbound_units_count=9)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            inserted = _stage_weekly_upsert(connection, WEEK, (original,), now=COMPLETE_NOW)
            assert inserted.status == "staged"
            assert inserted.inserted == 1
            first = connection.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE warehouse = :warehouse AND client_id = :client_id AND week_start = :week_start"
                ),
                {
                    "warehouse": original.warehouse,
                    "client_id": original.client_id,
                    "week_start": original.week_start,
                },
            ).mappings().one()
            repeated = _stage_weekly_upsert(connection, WEEK, (original,), now=COMPLETE_NOW)
            assert repeated.unchanged == 1
            second = connection.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE warehouse = :warehouse AND client_id = :client_id AND week_start = :week_start"
                ),
                {
                    "warehouse": original.warehouse,
                    "client_id": original.client_id,
                    "week_start": original.week_start,
                },
            ).mappings().one()
            assert second["id"] == first["id"]
            assert second["computed_at"] == first["computed_at"]
            updated = _stage_weekly_upsert(connection, WEEK, (changed,), now=COMPLETE_NOW)
            assert updated.updated == 1
            third = connection.execute(
                text(
                    "SELECT id, computed_at, inbound_units_count "
                    "FROM reporting.weekly_warehouse_client_performance "
                    "WHERE warehouse = :warehouse AND client_id = :client_id AND week_start = :week_start"
                ),
                {
                    "warehouse": original.warehouse,
                    "client_id": original.client_id,
                    "week_start": original.week_start,
                },
            ).mappings().one()
            assert third["id"] == first["id"]
            assert third["computed_at"] != first["computed_at"]
            assert third["inbound_units_count"] == 9
        finally:
            transaction.rollback()


@pytest.mark.skipif(
    not os.getenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"),
    reason=POSTGRES_SKIP_REASON,
)
def test_postgres_uncertain_commit_pending():
    from tests.test_pipeline_postgres_integration import (
        _assert_metadata_recovery_without_fault_injection,
    )

    _assert_metadata_recovery_without_fault_injection()
