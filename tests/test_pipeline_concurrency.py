"""Unit tests for week leases. The fake does not prove cross-process exclusion."""

from __future__ import annotations

import inspect
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError, ProgrammingError

from pipelines.concurrency import (
    GRANT_LEASE_SQL,
    LOCK_CONTROL_SQL,
    RELEASE_LEASE_SQL,
    VERIFY_LEASE_SQL,
    ConcurrencyError,
    acquire_week_lease,
    record_lease_run_status,
    release_week_lease,
    renew_week_lease,
    verify_week_lease,
)
from pipelines.publication import attempt_technical_commit
from pipelines.run_store import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED_IN_PROGRESS,
    STATUS_SUCCEEDED,
    STATUS_SUPERSEDED,
    RunStoreError,
    create_run,
    get_week_control,
    record_fencing_advance,
    update_run,
)
from tests.test_pipeline_run_store import MemoryConnection

WEEK = date(2026, 10, 19)
OTHER_WEEK = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
TTL = 900


class LeaseConnection(MemoryConnection):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.db_now = NOW
        self.locks = set()
        self.fail_with = None

    def execute(self, statement, parameters=None):
        if self.fail_with is not None:
            raise self.fail_with
        if "FOR UPDATE" in str(statement).upper():
            self.locks.add((parameters or {}).get("week_start"))
        return super().execute(statement, parameters)

    def _apply(self, op, params):
        if op == "lock_control":
            return [self._lock(params)]
        if op == "grant_lease":
            return self._grant(params)
        if op == "verify_lease":
            return self._verify(params)
        if op == "renew_lease":
            return self._renew(params)
        if op == "release_lease":
            return self._release(params)
        return super()._apply(op, params)

    def _moment(self, params):
        del params
        return self.db_now

    def _lock(self, params):
        row = self.controls[params["week_start"]]
        expires = row["lease_expires_at"]
        moment = self._moment(params)
        return {
            **row,
            "is_free": row["owner_run_id"] is None,
            "is_owner": row["owner_run_id"] == params["run_id"],
            "is_expired": expires is not None and expires <= moment,
        }

    def _grant(self, params):
        row = self.controls[params["week_start"]]
        moment = self._moment(params)
        expired = row["lease_expires_at"] is not None and row["lease_expires_at"] <= moment
        if row["fencing_token"] != params["expected_fencing_token"]:
            return []
        if row["owner_run_id"] is not None and not expired:
            return []
        row.update(
            {
                "owner_run_id": params["run_id"],
                "lease_acquired_at": moment,
                "lease_expires_at": moment + timedelta(seconds=params["lease_seconds"]),
                "fencing_token": row["fencing_token"] + 1,
            }
        )
        return [row]

    def _verify(self, params):
        row = self.controls.get(params["week_start"])
        if row is None:
            return []
        expires = row["lease_expires_at"]
        moment = self._moment(params)
        return [
            {
                **row,
                "is_owner": row["owner_run_id"] == params["run_id"],
                "token_matches": row["fencing_token"] == params["fencing_token"],
                "unexpired": expires is not None and expires > moment,
            }
        ]

    def _renew(self, params):
        row = self.controls.get(params["week_start"])
        moment = self._moment(params)
        if (
            row is None
            or row["owner_run_id"] != params["run_id"]
            or row["fencing_token"] != params["fencing_token"]
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= moment
        ):
            return []
        row["lease_expires_at"] = moment + timedelta(seconds=params["lease_seconds"])
        return [row]

    def _release(self, params):
        row = self.controls.get(params["week_start"])
        if (
            row is None
            or row["owner_run_id"] != params["run_id"]
            or row["fencing_token"] != params["fencing_token"]
        ):
            return []
        token = row["fencing_token"]
        row["owner_run_id"] = None
        row["lease_acquired_at"] = None
        row["lease_expires_at"] = None
        return [{"week_start": row["week_start"], "fencing_token": token}]


def _run(connection, week=WEEK, started_at=NOW):
    return create_run(connection, target_week_start=week, trigger_type="manual", started_at=started_at)


def test_sql_is_parameterized_and_locks_without_committing_or_writing_kpis():
    source = Path("pipelines/concurrency.py").read_text(encoding="utf-8")
    schema = Path("infra/sql/reporting_pipeline_control.sql").read_text(encoding="utf-8")
    assert ":run_id" in str(GRANT_LEASE_SQL)
    assert ":expected_fencing_token" in str(GRANT_LEASE_SQL)
    assert "FOR UPDATE" in str(LOCK_CONTROL_SQL).upper()
    assert "FOR UPDATE" in str(VERIFY_LEASE_SQL).upper()
    assert "fencing_token + 1" in str(GRANT_LEASE_SQL)
    assert "fencing_token +" not in str(RELEASE_LEASE_SQL)
    assert "telemetry_events" not in source
    assert ".commit(" not in source
    assert "force" not in inspect.signature(acquire_week_lease).parameters
    assert "skip_lock" not in inspect.signature(acquire_week_lease).parameters
    assert "observed_at" not in inspect.signature(acquire_week_lease).parameters
    assert "observed_at" not in inspect.signature(verify_week_lease).parameters
    lease_sql = "\n".join(str(statement) for statement in (LOCK_CONTROL_SQL, GRANT_LEASE_SQL, VERIFY_LEASE_SQL))
    assert "clock_timestamp()" in lease_sql
    assert "transaction_timestamp()" not in lease_sql
    assert "statement_timestamp()" not in lease_sql
    assert "clock:clock_timestamp_in_lock_recheck" in str(GRANT_LEASE_SQL)
    assert "lease_expires_at <= clock_timestamp()" in str(GRANT_LEASE_SQL)
    assert "clock:clock_timestamp_after_row_lock" in str(VERIFY_LEASE_SQL)
    assert "owner_run_id IS NOT NULL" in schema
    assert "weekly_warehouse_client_performance" not in str(GRANT_LEASE_SQL)


def test_available_week_is_acquired_without_marking_success_or_committing():
    connection = LeaseConnection()
    run = _run(connection)
    decision = acquire_week_lease(connection, WEEK, run.run_id, lease_seconds=TTL)
    assert decision.status == "acquired"
    assert decision.granted is True
    assert decision.fencing_token == 1
    assert decision.owner_run_id == run.run_id
    assert decision.lease_expires_at == NOW + timedelta(seconds=TTL)
    assert decision.durable is False
    assert decision.business_certified is False
    assert decision.concurrency_guaranteed is False
    assert connection.committed is False
    assert run.status == STATUS_PENDING
    again = acquire_week_lease(connection, WEEK, run.run_id, lease_seconds=TTL)
    assert again.status == "already_held"
    assert again.fencing_token == 1
    assert get_week_control(connection, WEEK).fencing_token == 1


def test_valid_lease_blocks_another_run_and_expired_lease_advances_token():
    connection = LeaseConnection()
    owner = _run(connection)
    other = _run(connection, started_at=NOW + timedelta(minutes=1))
    first = acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    blocked = acquire_week_lease(
        connection,
        WEEK,
        other.run_id,
        lease_seconds=TTL,
    )
    assert blocked.status == "occupied"
    assert blocked.granted is False
    assert blocked.owner_run_id == owner.run_id
    assert get_week_control(connection, WEEK).fencing_token == first.fencing_token
    connection.db_now = NOW + timedelta(seconds=TTL)
    recovered = acquire_week_lease(
        connection,
        WEEK,
        other.run_id,
        lease_seconds=TTL,
    )
    assert recovered.status == "recovered"
    assert recovered.fencing_token == 2
    assert recovered.owner_run_id == other.run_id
    stale = verify_week_lease(
        connection,
        WEEK,
        owner.run_id,
        first.fencing_token,
    )
    assert stale.status == "rejected"
    assert stale.lease_verified is False
    assert get_week_control(connection, WEEK).owner_run_id == other.run_id
    assert get_week_control(connection, WEEK).fencing_token == 2


def test_verify_renew_and_release_require_the_current_owner_and_token():
    connection = LeaseConnection()
    owner = _run(connection)
    intruder = _run(connection, started_at=NOW + timedelta(minutes=1))
    granted = acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    verified = verify_week_lease(
        connection, WEEK, owner.run_id, granted.fencing_token
    )
    assert verified.lease_verified is True
    assert verified.lock_held is True
    assert WEEK in {item for item in connection.locks}
    assert connection.committed is False
    assert attempt_technical_commit(connection).business_certified is False
    connection.db_now = NOW + timedelta(minutes=5)
    renewed = renew_week_lease(
        connection,
        WEEK,
        owner.run_id,
        granted.fencing_token,
        lease_seconds=TTL,
    )
    assert renewed.status == "renewed"
    assert renewed.fencing_token == granted.fencing_token
    assert renewed.lease_expires_at == NOW + timedelta(minutes=5, seconds=TTL)
    wrong_owner = renew_week_lease(
        connection,
        WEEK,
        intruder.run_id,
        granted.fencing_token,
        lease_seconds=TTL,
    )
    wrong_token = renew_week_lease(
        connection,
        WEEK,
        owner.run_id,
        0,
        lease_seconds=TTL,
    )
    assert wrong_owner.status == "rejected"
    assert wrong_token.status == "rejected"
    assert get_week_control(connection, WEEK).fencing_token == 1
    foreign_release = release_week_lease(connection, WEEK, intruder.run_id, granted.fencing_token)
    stale_release = release_week_lease(connection, WEEK, owner.run_id, 0)
    assert foreign_release.status == "rejected"
    assert stale_release.status == "rejected"
    assert get_week_control(connection, WEEK).owner_run_id == owner.run_id
    released = release_week_lease(connection, WEEK, owner.run_id, granted.fencing_token)
    assert released.status == "released"
    assert released.fencing_token == 1
    control = get_week_control(connection, WEEK)
    assert control is not None
    assert control.owner_run_id is None
    assert control.fencing_token == 1


def test_weeks_are_independent_and_invalid_inputs_do_not_write():
    connection = LeaseConnection()
    first = _run(connection)
    second = _run(connection, OTHER_WEEK, NOW + timedelta(days=7))
    left = acquire_week_lease(connection, WEEK, first.run_id, lease_seconds=TTL)
    right = acquire_week_lease(
        connection,
        OTHER_WEEK,
        second.run_id,
        lease_seconds=TTL,
    )
    assert left.fencing_token == 1
    assert right.fencing_token == 1
    assert get_week_control(connection, WEEK).owner_run_id == first.run_id
    assert get_week_control(connection, OTHER_WEEK).owner_run_id == second.run_id
    before = {
        week: (
            control["owner_run_id"],
            control["fencing_token"],
            control["lease_expires_at"],
        )
        for week, control in connection.controls.items()
    }
    with pytest.raises(RunStoreError) as invalid_week:
        acquire_week_lease(connection, WEEK + timedelta(days=1), first.run_id, lease_seconds=TTL)
    assert invalid_week.value.code == "invalid_week_start"
    with pytest.raises(RunStoreError) as invalid_run:
        acquire_week_lease(connection, WEEK, "not-a-uuid", lease_seconds=TTL)
    assert invalid_run.value.code == "invalid_identifier"
    with pytest.raises(ConcurrencyError) as ttl:
        acquire_week_lease(connection, WEEK, first.run_id, lease_seconds=0)
    assert ttl.value.code == "invalid_lease_ttl"
    with pytest.raises(ConcurrencyError) as mismatch:
        acquire_week_lease(connection, OTHER_WEEK, first.run_id, lease_seconds=TTL)
    assert mismatch.value.code == "week_mismatch"
    after = {
        week: (
            control["owner_run_id"],
            control["fencing_token"],
            control["lease_expires_at"],
        )
        for week, control in connection.controls.items()
    }
    assert after == before


def test_connection_and_query_failures_do_not_mean_the_week_is_free():
    connection = LeaseConnection()
    run = _run(connection)
    acquire_week_lease(connection, WEEK, run.run_id, lease_seconds=TTL)
    connection.fail_with = OperationalError("SELECT", {}, Exception("connection lost"))
    with pytest.raises(ConcurrencyError) as uncertain:
        verify_week_lease(connection, WEEK, run.run_id, 1)
    assert uncertain.value.code == "transaction_uncertain"
    connection.fail_with = ProgrammingError("SELECT", {}, Exception("syntax"))
    with pytest.raises(ConcurrencyError) as failed:
        verify_week_lease(connection, WEEK, run.run_id, 1)
    assert failed.value.code == "query_failed"
    connection.fail_with = None
    assert get_week_control(connection, WEEK).owner_run_id == run.run_id
    autocommit = LeaseConnection(autocommit=True)
    with pytest.raises(RunStoreError) as rejected:
        acquire_week_lease(autocommit, WEEK, uuid4(), lease_seconds=TTL)
    assert rejected.value.code == "autocommit_forbidden"
    assert autocommit.committed is False


def test_occupied_and_superseded_follow_run_store_and_are_not_success():
    connection = LeaseConnection()
    owner = _run(connection)
    waiting = _run(connection, started_at=NOW + timedelta(minutes=1))
    acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    blocked = acquire_week_lease(
        connection, WEEK, waiting.run_id, lease_seconds=TTL
    )
    assert blocked.status == "occupied"
    skipped = record_lease_run_status(
        connection,
        waiting.run_id,
        status=STATUS_SKIPPED_IN_PROGRESS,
        finished_at=NOW + timedelta(minutes=1),
        reason="lease_occupied",
    )
    assert skipped.status == STATUS_SKIPPED_IN_PROGRESS
    assert skipped.business_certified is False
    update_run(connection, owner.run_id, expected_status=STATUS_PENDING, status=STATUS_RUNNING)
    lost = record_lease_run_status(
        connection,
        owner.run_id,
        status=STATUS_SUPERSEDED,
        finished_at=NOW + timedelta(minutes=2),
        reason="lease_lost",
    )
    assert lost.status == STATUS_SUPERSEDED
    with pytest.raises(ConcurrencyError) as success:
        record_lease_run_status(
            connection,
            waiting.run_id,
            status=STATUS_SUCCEEDED,
            finished_at=NOW + timedelta(minutes=3),
            reason="no",
        )
    assert success.value.code == "success_forbidden"


def test_exact_expiry_rejects_the_old_owner_and_release_does_not_reuse_the_token():
    connection = LeaseConnection()
    owner = _run(connection)
    granted = acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    expires = NOW + timedelta(seconds=TTL)
    connection.db_now = expires - timedelta(seconds=1)
    still_valid = verify_week_lease(
        connection,
        WEEK,
        owner.run_id,
        granted.fencing_token,
    )
    assert still_valid.status == "verified"
    assert still_valid.lock_held is True
    connection.db_now = expires
    at_expiry = verify_week_lease(
        connection, WEEK, owner.run_id, granted.fencing_token
    )
    assert at_expiry.status == "rejected"
    assert at_expiry.lease_verified is False
    renew_at_expiry = renew_week_lease(
        connection,
        WEEK,
        owner.run_id,
        granted.fencing_token,
        lease_seconds=TTL,
    )
    assert renew_at_expiry.status == "rejected"
    assert get_week_control(connection, WEEK).fencing_token == granted.fencing_token
    released = release_week_lease(connection, WEEK, owner.run_id, granted.fencing_token)
    assert released.fencing_token == granted.fencing_token
    again = acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    assert again.status == "acquired"
    assert again.fencing_token == granted.fencing_token + 1
    assert get_week_control(connection, WEEK).fencing_token == granted.fencing_token + 1


def test_aborted_transaction_does_not_grant_or_clear_a_lease():
    connection = LeaseConnection()
    owner = _run(connection)
    acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    connection.fail_with = PendingRollbackError("UPDATE", {}, Exception("transaction aborted"))
    with pytest.raises(ConcurrencyError) as aborted:
        acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    assert aborted.value.code == "transaction_uncertain"
    connection.fail_with = None
    control = get_week_control(connection, WEEK)
    assert control.owner_run_id == owner.run_id
    assert control.fencing_token == 1
    assert connection.committed is False


def test_later_server_clock_expires_a_lease_and_fencing_store_cannot_replace_the_owner():
    connection = LeaseConnection()
    owner = _run(connection)
    intruder = _run(connection, started_at=NOW + timedelta(minutes=1))
    granted = acquire_week_lease(connection, WEEK, owner.run_id, lease_seconds=TTL)
    connection.db_now = granted.lease_expires_at - timedelta(seconds=1)
    assert verify_week_lease(connection, WEEK, owner.run_id, granted.fencing_token).status == "verified"
    connection.db_now = granted.lease_expires_at
    expired = verify_week_lease(connection, WEEK, owner.run_id, granted.fencing_token)
    assert expired.status == "rejected"
    before = len(connection.statements)
    with pytest.raises(RunStoreError) as blocked:
        record_fencing_advance(
            connection,
            WEEK,
            expected_fencing_token=granted.fencing_token,
            owner_run_id=intruder.run_id,
            lease_acquired_at=connection.db_now,
            lease_expires_at=connection.db_now + timedelta(seconds=TTL),
        )
    assert blocked.value.code == "lease_authority_forbidden"
    assert len(connection.statements) == before
    control = get_week_control(connection, WEEK)
    assert control.owner_run_id == owner.run_id
    assert control.fencing_token == granted.fencing_token
    recovered = acquire_week_lease(connection, WEEK, intruder.run_id, lease_seconds=TTL)
    assert recovered.status == "recovered"
    assert recovered.fencing_token == granted.fencing_token + 1
    assert renew_week_lease(
        connection, WEEK, owner.run_id, granted.fencing_token, lease_seconds=TTL
    ).status == "rejected"
    assert release_week_lease(connection, WEEK, owner.run_id, granted.fencing_token).status == "rejected"
    assert get_week_control(connection, WEEK).owner_run_id == intruder.run_id


@pytest.mark.skipif(
    not os.getenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"),
    reason=(
        "Pending isolated PostgreSQL. Set TRACKFLOW_PUBLICATION_TEST_DATABASE_URL "
        "to a disposable database before claiming cross-process lease exclusion."
    ),
)
def test_two_postgres_connections_cannot_both_hold_one_week():
    url = os.environ["TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"]
    if url == os.getenv("DATABASE_URL"):
        pytest.skip("Refusing to reuse DATABASE_URL as the lease test database")
    pytest.fail("Isolated PostgreSQL is configured, but the two-connection lease test is not executed yet")
