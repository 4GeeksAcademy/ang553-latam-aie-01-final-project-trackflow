"""Unit tests for operational run history and week control.

The in-memory connection applies the statement parameters. It does not prove
PostgreSQL atomicity, locking, or concurrent lease acquisition.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from pipelines.publication import PublicationOutcome, attempt_technical_commit
from pipelines.run_store import (
    COMMIT_TECHNICALLY_OBSERVED,
    COMMIT_UNCERTAIN,
    CONTROL_TABLE,
    CREATE_RUN_SQL,
    RUNS_TABLE,
    STATUS_BLOCKED_QUALITY,
    STATUS_FAILED_RETRYABLE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    WEEKLY_PIPELINE_NAME,
    RunStoreError,
    create_run,
    ensure_week_control,
    get_week_control,
    latest_run,
    latest_valid_publication,
    record_commit_observation,
    record_fencing_advance,
    record_valid_publication,
    update_run,
)
from pipelines.weekly_performance import WeeklyAggregate

WEEK = date(2026, 10, 19)
OTHER_WEEK = date(2026, 10, 26)
STARTED = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)
FINISHED = datetime(2026, 10, 26, 1, 5, tzinfo=timezone.utc)


class MappingResult:
    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class MemoryConnection:
    """Applies the run-store statements. Not a PostgreSQL stand-in."""

    def __init__(self, *, autocommit=False, dialect="postgresql", in_transaction=True):
        self.runs = {}
        self.controls = {}
        self.statements = []
        self.parameters = []
        self.committed = False
        self.autocommit = autocommit
        self.dialect = SimpleNamespace(name=dialect)
        self.open = in_transaction

    def in_transaction(self):
        return self.open

    def get_execution_options(self):
        if self.autocommit:
            return {"isolation_level": "AUTOCOMMIT"}
        return {}

    def commit(self):
        self.committed = True

    def execute(self, statement, parameters=None):
        sql = str(statement)
        params = dict(parameters or {})
        self.statements.append(sql)
        self.parameters.append(params)
        if ":" not in sql:
            raise AssertionError("statement is not parameterized")
        op = sql.split("op:", 1)[1].split("*/", 1)[0].strip()
        return MappingResult(self._apply(op, params))

    def _apply(self, op, params):
        if op == "create_run":
            return [self._create_run(params)]
        if op == "select_run":
            row = self.runs.get(params["run_id"])
            return [] if row is None else [row]
        if op == "update_run":
            return self._update_run(params)
        if op == "latest_run":
            return self._latest_run(params)
        if op == "ensure_control":
            self.controls.setdefault(params["week_start"], self._new_control(params["week_start"]))
            return []
        if op == "select_control":
            row = self.controls.get(params["week_start"])
            return [] if row is None else [row]
        if op == "record_valid_publication":
            return self._record_valid(params)
        if op == "advance_fencing":
            return self._advance(params)
        if op == "record_commit_observation":
            return self._observe_commit(params)
        if op == "latest_valid_publication":
            row = self.controls.get(params["week_start"])
            if row is None or row["last_valid_publication_run_id"] is None:
                return []
            return [
                {
                    "week_start": row["week_start"],
                    "run_id": row["last_valid_publication_run_id"],
                    "published_at": row["last_valid_published_at"],
                    "published_row_count": row["published_row_count"],
                    "dependency_fingerprint": row["dependency_fingerprint"],
                    "rules_version": row["rules_version"],
                    "fencing_token": row["fencing_token"],
                }
            ]
        raise AssertionError(f"unexpected operation {op}")

    def _create_run(self, params):
        if params["run_id"] in self.runs:
            raise IntegrityError("INSERT", params, Exception("duplicate key"))
        row = {
            "run_id": params["run_id"],
            "pipeline_name": params["pipeline_name"],
            "target_week_start": params["target_week_start"],
            "trigger_type": params["trigger_type"],
            "started_at": params["started_at"] or STARTED,
            "finished_at": None,
            "status": STATUS_PENDING,
            "stage_reached": params["stage_reached"],
            "records_processed": None,
            "aggregates_calculated": None,
            "rows_inserted": None,
            "rows_updated": None,
            "rows_unchanged": None,
            "error_code": None,
            "error_message": None,
            "block_reason": None,
            "commit_result": "not_attempted",
            "prefect_flow_run_id": params["prefect_flow_run_id"],
            "prefect_task_run_id": params["prefect_task_run_id"],
        }
        self.runs[row["run_id"]] = row
        return row

    def _update_run(self, params):
        row = self.runs.get(params["run_id"])
        if row is None or row["status"] != params["expected_status"]:
            return []
        row.update(
            {
                "status": params["status"],
                "stage_reached": params["stage_reached"],
                "finished_at": params["finished_at"],
                "records_processed": params["records_processed"],
                "aggregates_calculated": params["aggregates_calculated"],
                "rows_inserted": params["rows_inserted"],
                "rows_updated": params["rows_updated"],
                "rows_unchanged": params["rows_unchanged"],
                "error_code": params["error_code"],
                "error_message": params["error_message"],
                "block_reason": params["block_reason"],
                "commit_result": params["commit_result"],
                "prefect_flow_run_id": params["prefect_flow_run_id"],
                "prefect_task_run_id": params["prefect_task_run_id"],
            }
        )
        return [row]

    def _latest_run(self, params):
        rows = [
            row
            for row in self.runs.values()
            if row["pipeline_name"] == params["pipeline_name"]
            and (
                params["target_week_start"] is None
                or row["target_week_start"] == params["target_week_start"]
            )
        ]
        rows.sort(key=lambda row: (row["started_at"], str(row["run_id"])), reverse=True)
        return rows[:1]

    def _new_control(self, week_start):
        return {
            "week_start": week_start,
            "owner_run_id": None,
            "lease_acquired_at": None,
            "lease_expires_at": None,
            "fencing_token": 0,
            "last_valid_publication_run_id": None,
            "last_valid_published_at": None,
            "dependency_fingerprint": None,
            "published_row_count": None,
            "rules_version": None,
        }

    def _record_valid(self, params):
        row = self.controls.get(params["week_start"])
        if row is None:
            return []
        row.update(
            {
                "last_valid_publication_run_id": params["run_id"],
                "last_valid_published_at": params["published_at"],
                "published_row_count": params["published_row_count"],
                "dependency_fingerprint": params["dependency_fingerprint"],
                "rules_version": params["rules_version"],
            }
        )
        return [row]

    def _observe_commit(self, params):
        row = self.runs.get(params["run_id"])
        if row is None or row["status"] != params["expected_status"]:
            return []
        row.update(
            {
                "commit_result": params["commit_result"],
                "stage_reached": params["stage_reached"],
                "error_code": params["error_code"],
                "error_message": params["error_message"],
            }
        )
        return [row]

    def _advance(self, params):
        row = self.controls.get(params["week_start"])
        if row is None or row["fencing_token"] != params["expected_fencing_token"]:
            return []
        row.update(
            {
                "owner_run_id": params["owner_run_id"],
                "lease_acquired_at": params["lease_acquired_at"],
                "lease_expires_at": params["lease_expires_at"],
                "fencing_token": row["fencing_token"] + 1,
            }
        )
        return [row]


def test_sql_is_parameterized_and_does_not_write_business_or_telemetry_tables():
    from pathlib import Path

    sql = str(CREATE_RUN_SQL)
    module = Path("pipelines/run_store.py").read_text(encoding="utf-8")
    assert ":run_id" in sql
    assert "advance_fencing" not in module
    assert "SET owner_run_id" not in module
    assert "TELEMETRY_EVENTS" not in sql.upper()
    assert "DELETE " not in sql.upper()
    schema = Path("infra/sql/reporting_pipeline_control.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS reporting.pipeline_publication_control" in schema
    assert "CREATE TABLE IF NOT EXISTS reporting.pipeline_publication_runs" in schema
    assert "fencing_token" in schema
    assert "INSERT INTO reporting.weekly_warehouse_client_performance" not in schema
    assert "telemetry_events" not in schema
    assert ".commit(" not in module
    assert ".rollback(" not in module
    assert RUNS_TABLE != "reporting.weekly_warehouse_client_performance"
    assert CONTROL_TABLE != RUNS_TABLE


def test_new_runs_have_unique_ids_and_do_not_commit():
    connection = MemoryConnection()
    first = create_run(
        connection,
        target_week_start=WEEK,
        trigger_type="manual",
        started_at=STARTED,
        prefect_flow_run_id=str(uuid4()),
    )
    second = create_run(
        connection,
        target_week_start=WEEK,
        trigger_type="retry",
        started_at=STARTED + timedelta(minutes=1),
    )
    assert first.run_id != second.run_id
    assert first.status == STATUS_PENDING
    assert first.business_certified is False
    assert connection.committed is False
    with pytest.raises(RunStoreError) as duplicate:
        create_run(
            connection,
            target_week_start=WEEK,
            trigger_type="manual",
            run_id=first.run_id,
            started_at=STARTED,
        )
    assert duplicate.value.code == "duplicate_run_id"


def test_allowed_and_rejected_status_transitions_record_times_counts_and_errors():
    connection = MemoryConnection()
    created = create_run(connection, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    running = update_run(
        connection,
        created.run_id,
        expected_status=STATUS_PENDING,
        status=STATUS_RUNNING,
        stage_reached="stage",
    )
    assert running.started_at == STARTED
    assert running.finished_at is None
    finished = update_run(
        connection,
        created.run_id,
        expected_status=STATUS_RUNNING,
        status=STATUS_SUCCEEDED,
        finished_at=FINISHED,
        records_processed=4,
        aggregates_calculated=1,
        rows_inserted=1,
        rows_updated=0,
        rows_unchanged=0,
        commit_result=COMMIT_TECHNICALLY_OBSERVED,
    )
    assert finished.status == STATUS_SUCCEEDED
    assert finished.rows_inserted == 1
    assert finished.commit_result == COMMIT_TECHNICALLY_OBSERVED
    assert finished.business_certified is False
    with pytest.raises(RunStoreError) as invalid:
        update_run(
            connection,
            created.run_id,
            expected_status=STATUS_SUCCEEDED,
            status=STATUS_FAILED_RETRYABLE,
            finished_at=FINISHED,
            error_code="write_error",
        )
    assert invalid.value.code == "invalid_transition"
    pending = create_run(
        connection,
        target_week_start=OTHER_WEEK,
        trigger_type="manual",
        started_at=STARTED,
    )
    with pytest.raises(RunStoreError) as unknown:
        update_run(
            connection,
            pending.run_id,
            expected_status=STATUS_PENDING,
            status="published",
            finished_at=FINISHED,
        )
    assert unknown.value.code == "invalid_status"


def test_latest_run_is_separate_from_latest_valid_publication_and_week():
    connection = MemoryConnection()
    valid = create_run(connection, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    update_run(connection, valid.run_id, expected_status=STATUS_PENDING, status=STATUS_RUNNING)
    update_run(
        connection,
        valid.run_id,
        expected_status=STATUS_RUNNING,
        status=STATUS_SUCCEEDED,
        finished_at=FINISHED,
        rows_inserted=2,
        commit_result=COMMIT_TECHNICALLY_OBSERVED,
    )
    record_valid_publication(
        connection,
        WEEK,
        valid.run_id,
        published_at=FINISHED,
        published_row_count=2,
        dependency_fingerprint="events-v1",
        rules_version="phase-1",
    )
    later = create_run(
        connection,
        target_week_start=WEEK,
        trigger_type="retry",
        started_at=FINISHED + timedelta(minutes=5),
    )
    update_run(connection, later.run_id, expected_status=STATUS_PENDING, status=STATUS_RUNNING)
    failed = update_run(
        connection,
        later.run_id,
        expected_status=STATUS_RUNNING,
        status=STATUS_FAILED_RETRYABLE,
        finished_at=FINISHED + timedelta(minutes=6),
        error_code="integrity_error",
        error_message="constraint rejected the batch",
        commit_result="rolled_back",
    )
    other = create_run(
        connection,
        target_week_start=OTHER_WEEK,
        trigger_type="manual",
        started_at=FINISHED + timedelta(days=7),
    )
    assert latest_run(connection, target_week_start=WEEK).run_id == later.run_id
    assert latest_run(connection, target_week_start=OTHER_WEEK).run_id == other.run_id
    publication = latest_valid_publication(connection, WEEK)
    assert publication is not None
    assert publication.run_id == valid.run_id
    assert publication.run_id != failed.run_id
    assert publication.published_row_count == 2
    assert publication.business_certified is False
    assert latest_valid_publication(connection, OTHER_WEEK) is None
    assert get_week_control(connection, WEEK).last_valid_publication_run_id == valid.run_id


def test_record_fencing_advance_cannot_grant_or_replace_a_lease():
    connection = MemoryConnection()
    owner = create_run(connection, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    before = len(connection.statements)
    with pytest.raises(RunStoreError) as blocked:
        record_fencing_advance(
            connection,
            WEEK,
            expected_fencing_token=0,
            owner_run_id=owner.run_id,
            lease_acquired_at=STARTED,
            lease_expires_at=STARTED + timedelta(minutes=15),
        )
    assert blocked.value.code == "lease_authority_forbidden"
    assert len(connection.statements) == before
    assert get_week_control(connection, WEEK) is None
    assert connection.committed is False


def test_autocommit_invalid_week_and_secrets_are_rejected_before_sql():
    connection = MemoryConnection()
    with pytest.raises(RunStoreError) as autocommit:
        create_run(MemoryConnection(autocommit=True), target_week_start=WEEK, trigger_type="manual")
    assert autocommit.value.code == "autocommit_forbidden"
    with pytest.raises(RunStoreError) as invalid_week:
        create_run(connection, target_week_start=WEEK + timedelta(days=1), trigger_type="manual")
    assert invalid_week.value.code == "invalid_week_start"
    with pytest.raises(RunStoreError) as secret:
        create_run(connection, target_week_start=WEEK, trigger_type="Bearer token")
    assert secret.value.code == "secret_rejected"
    assert connection.statements == []


def test_blocked_quality_and_publication_modules_remain_uncertified():
    connection = MemoryConnection()
    created = create_run(connection, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    blocked = update_run(
        connection,
        created.run_id,
        expected_status=STATUS_PENDING,
        status=STATUS_BLOCKED_QUALITY,
        finished_at=FINISHED,
        block_reason="coverage_insufficient",
        commit_result="not_attempted",
    )
    assert blocked.business_certified is False
    outcome = PublicationOutcome(week_start=WEEK, status="staged", inserted=1)
    assert outcome.business_certified is False
    observation = attempt_technical_commit(connection)
    assert observation.business_certified is False
    assert connection.committed is False
    assert WeeklyAggregate.__dataclass_fields__["discrepancy_rate"].name == "discrepancy_rate"


def test_control_row_is_per_week_and_ensure_does_not_acquire_a_lease():
    connection = MemoryConnection()
    control = ensure_week_control(connection, WEEK)
    again = ensure_week_control(connection, WEEK)
    other = ensure_week_control(connection, OTHER_WEEK)
    assert control.fencing_token == 0
    assert control.owner_run_id is None
    assert control.lease_expires_at is None
    assert again.week_start == control.week_start
    assert other.week_start == OTHER_WEEK
    assert len(connection.controls) == 2


def test_run_store_accepts_uuid_strings_and_rejects_a_closed_transaction():
    connection = MemoryConnection(in_transaction=False)
    with pytest.raises(RunStoreError) as closed:
        latest_run(connection, target_week_start=WEEK)
    assert closed.value.code == "transaction_required"
    open_connection = MemoryConnection()
    created = create_run(
        open_connection,
        target_week_start=WEEK,
        trigger_type="manual",
        run_id=UUID(str(uuid4())),
        started_at=STARTED,
    )
    assert created.pipeline_name == WEEKLY_PIPELINE_NAME


def test_unresolved_commit_observation_does_not_finish_the_run_or_move_the_lease():
    connection = MemoryConnection()
    created = create_run(connection, target_week_start=WEEK, trigger_type="manual", started_at=STARTED)
    update_run(
        connection,
        created.run_id,
        expected_status=STATUS_PENDING,
        status=STATUS_RUNNING,
        stage_reached="publish",
    )
    control = ensure_week_control(connection, WEEK)
    observed = record_commit_observation(
        connection,
        created.run_id,
        commit_result=COMMIT_UNCERTAIN,
        stage_reached="commit",
        error_code="commit_uncertain",
        error_message="commit outcome was not observed",
    )
    assert observed.status == STATUS_RUNNING
    assert observed.finished_at is None
    assert observed.commit_result == COMMIT_UNCERTAIN
    assert observed.business_certified is False
    assert get_week_control(connection, WEEK).fencing_token == control.fencing_token
    assert connection.committed is False
    with pytest.raises(RunStoreError) as success:
        record_commit_observation(
            connection,
            created.run_id,
            commit_result=COMMIT_TECHNICALLY_OBSERVED,
            stage_reached="commit",
            error_code="commit_uncertain",
            error_message="must not look successful",
        )
    assert success.value.code == "invalid_commit_result"
