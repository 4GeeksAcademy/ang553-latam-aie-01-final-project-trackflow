"""Real DEV-55 persistence tests against an explicitly isolated PostgreSQL."""

from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from services.task_queue.relay import _claim_one
from services.task_queue.reconciliation import reconcile_one, reconcile_task
from services.task_queue.store import (
    accept_async_task,
    claim_execution,
    fail_claim,
    insert_dead_letter,
    start_prefect,
)


URL_ENV = "TRACKFLOW_DEV55_TEST_DATABASE_URL"
QA_DATABASE = "trackflow_dev55_qa"
QA_USER = "trackflow_dev55_qa"


@pytest.fixture()
def qa_engine():
    url = os.getenv(URL_ENV)
    if not url:
        pytest.skip(f"{URL_ENV} is required")
    if url in {os.getenv("DATABASE_URL"), os.getenv("TRACKFLOW_REPORTING_DATABASE_URL")}:
        pytest.fail("DEV-55 integration refuses operational/reporting database URLs")
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        assert identity == (QA_DATABASE, QA_USER)
        tables = connection.execute(
            text(
                "SELECT to_regclass('reporting.pipeline_publication_runs'), "
                "to_regclass('reporting.async_tasks'), "
                "to_regclass('reporting.task_dispatch_outbox'), "
                "to_regclass('reporting.task_dead_letters')"
            )
        ).one()
        assert all(tables)
    yield engine
    engine.dispose()


def _cleanup(engine, week: date) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM reporting.task_dead_letters WHERE run_id IN "
                "(SELECT run_id FROM reporting.pipeline_publication_runs "
                "WHERE target_week_start=:week)"
            ),
            {"week": week},
        )
        connection.execute(
            text(
                "DELETE FROM reporting.task_dispatch_outbox WHERE task_id IN "
                "(SELECT task_id FROM reporting.async_tasks WHERE week_start=:week)"
            ),
            {"week": week},
        )
        connection.execute(
            text("DELETE FROM reporting.async_tasks WHERE week_start=:week"),
            {"week": week},
        )
        connection.execute(
            text("DELETE FROM reporting.pipeline_publication_control WHERE week_start=:week"),
            {"week": week},
        )
        connection.execute(
            text("DELETE FROM reporting.pipeline_publication_runs WHERE target_week_start=:week"),
            {"week": week},
        )


def test_acceptance_is_atomic_and_payload_is_lightweight(qa_engine) -> None:
    week = date(2023, 1, 2)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            run_id, task_id, created = accept_async_task(connection, week, str(uuid4()))
        assert created is True
        with qa_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT t.run_id,o.payload FROM reporting.async_tasks t JOIN "
                    "reporting.task_dispatch_outbox o USING (task_id) WHERE t.task_id=:task_id"
                ),
                {"task_id": task_id},
            ).mappings().one()
        assert row["run_id"] == run_id
        assert set(row["payload"]) == {"contract_version", "task_id", "run_id", "week_start"}

        rollback_week = date(2023, 1, 9)
        _cleanup(qa_engine, rollback_week)
        with pytest.raises(RuntimeError):
            with qa_engine.begin() as connection:
                accept_async_task(connection, rollback_week, str(uuid4()))
                raise RuntimeError("force rollback")
        with qa_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM reporting.async_tasks WHERE week_start=:week"),
                {"week": rollback_week},
            ).scalar_one() == 0
        _cleanup(qa_engine, rollback_week)
    finally:
        _cleanup(qa_engine, week)


def test_claim_recovery_is_preflight_only_and_old_epoch_is_fenced(qa_engine) -> None:
    week = date(2023, 2, 6)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            _run_id, task_id, _created = accept_async_task(connection, week, str(uuid4()))
        with qa_engine.begin() as connection:
            first = claim_execution(connection, task_id, lease_seconds=30)
        assert first is not None
        with qa_engine.begin() as connection:
            assert claim_execution(connection, task_id, lease_seconds=30) is None
            connection.execute(
                text(
                    "UPDATE reporting.async_tasks SET claim_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            )
            assert not start_prefect(
                connection,
                task_id=task_id,
                claim_token=first["claim_token"],
                epoch=first["execution_epoch"],
                lease_seconds=30,
            )
        with qa_engine.begin() as connection:
            second = claim_execution(connection, task_id, lease_seconds=30)
        assert second is not None
        assert second["execution_epoch"] == first["execution_epoch"] + 1
        with qa_engine.begin() as connection:
            assert not start_prefect(
                connection,
                task_id=task_id,
                claim_token=first["claim_token"],
                epoch=first["execution_epoch"],
                lease_seconds=30,
            )
            assert start_prefect(
                connection,
                task_id=task_id,
                claim_token=second["claim_token"],
                epoch=second["execution_epoch"],
                lease_seconds=30,
            )
        with qa_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE reporting.async_tasks SET claim_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            )
            assert claim_execution(connection, task_id, lease_seconds=30) is None
            assert reconcile_task(connection, task_id) == "reconciliation_required"
        with qa_engine.connect() as connection:
            state = connection.execute(
                text(
                    "SELECT execution_state,pipeline_status FROM reporting.async_tasks "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            ).one()
        assert state == ("reconciliation_required", "execution_abandoned")
    finally:
        _cleanup(qa_engine, week)


def test_expired_preflight_is_left_for_worker_reclaim(qa_engine) -> None:
    week = date(2023, 2, 27)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            _run_id, task_id, _created = accept_async_task(
                connection, week, str(uuid4())
            )
            first = claim_execution(connection, task_id, lease_seconds=30)
            assert first is not None
            connection.execute(
                text(
                    "UPDATE reporting.async_tasks SET "
                    "claim_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            )

        assert reconcile_one(qa_engine) is False

        with qa_engine.begin() as connection:
            recovered = claim_execution(connection, task_id, lease_seconds=30)
        assert recovered is not None
        assert recovered["execution_epoch"] == first["execution_epoch"] + 1
    finally:
        _cleanup(qa_engine, week)


def test_terminal_preflight_failure_sets_published_false(qa_engine) -> None:
    week = date(2023, 3, 13)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            _run_id, task_id, _created = accept_async_task(
                connection, week, str(uuid4())
            )
            claim = claim_execution(connection, task_id, lease_seconds=30)
            assert claim is not None
            assert fail_claim(
                connection,
                task_id=task_id,
                claim_token=claim["claim_token"],
                epoch=claim["execution_epoch"],
                state="dead_lettered",
                error_code="qa_preflight_failure",
                error_message="safe preflight failure",
            )
        with qa_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT execution_state,published FROM reporting.async_tasks "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            ).one() == ("dead_lettered", False)
    finally:
        _cleanup(qa_engine, week)


def test_blocked_quality_attempt_and_celery_state_are_consistent(qa_engine) -> None:
    week = date(2023, 2, 13)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            _run_id, task_id, _created = accept_async_task(connection, week, str(uuid4()))
            claim = claim_execution(connection, task_id, lease_seconds=30)
        assert claim is not None
        assert claim["attempt_count"] == claim["attempt_number"] == 1
        assert claim["celery_state"] == "STARTED"
        assert claim["execution_state"] == "claimed"
    finally:
        _cleanup(qa_engine, week)


def test_reconciler_does_not_race_an_active_worker_with_terminal_history(qa_engine) -> None:
    week = date(2023, 2, 20)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            run_id, task_id, _created = accept_async_task(connection, week, str(uuid4()))
            claim = claim_execution(connection, task_id, lease_seconds=30)
            assert claim is not None
            assert start_prefect(
                connection,
                task_id=task_id,
                claim_token=claim["claim_token"],
                epoch=claim["execution_epoch"],
                lease_seconds=30,
            )
            connection.execute(
                text(
                    "UPDATE reporting.pipeline_publication_runs SET "
                    "status='blocked_quality',finished_at=clock_timestamp(),"
                    "block_reason='qa_controlled',updated_at=clock_timestamp() "
                    "WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
        assert reconcile_one(qa_engine) is False
        with qa_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT execution_state,celery_state FROM reporting.async_tasks "
                    "WHERE task_id=:task_id"
                ),
                {"task_id": task_id},
            ).one() == ("running", "STARTED")
    finally:
        _cleanup(qa_engine, week)


def test_two_relays_exclude_each_other_and_dlq_is_idempotent(qa_engine) -> None:
    week = date(2023, 3, 6)
    _cleanup(qa_engine, week)
    try:
        with qa_engine.begin() as connection:
            run_id, task_id, _created = accept_async_task(connection, week, str(uuid4()))
        with qa_engine.begin() as connection:
            first = _claim_one(connection, "relay-a", uuid4(), 30, 3)
        assert first is not None
        with qa_engine.begin() as connection:
            assert _claim_one(connection, "relay-b", uuid4(), 30, 3) is None
        with qa_engine.begin() as connection:
            assert insert_dead_letter(
                connection,
                task_id=task_id,
                run_id=run_id,
                attempt_number=1,
                reason="non_retryable",
                error_code="invalid_configuration",
                error_message="safe terminal failure",
            )
            assert insert_dead_letter(
                connection,
                task_id=task_id,
                run_id=run_id,
                attempt_number=1,
                reason="non_retryable",
                error_code="invalid_configuration",
                error_message="safe terminal failure",
            )
        with qa_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM reporting.task_dead_letters WHERE task_id=:task_id"),
                {"task_id": task_id},
            ).scalar_one() == 1
    finally:
        _cleanup(qa_engine, week)
