"""Opt-in DEV-53 integration tests against an authorized disposable PostgreSQL.

The suite never falls back to DATABASE_URL. It requires a dedicated loopback
URL plus the exact tmpfs container name authorized by the test operator.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services.job_runner import (
    ACQUIRED,
    SKIPPED_COMPLETED,
    SKIPPED_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    acquire_job_run,
    complete_job_run,
    fail_job_run,
    get_job_run,
    get_latest_job_run,
)
from services.job_runner import service as job_run_service


TEST_URL_ENV = "TRACKFLOW_JOB_RUNNER_TEST_DATABASE_URL"
TEST_CONTAINER_ENV = "TRACKFLOW_JOB_RUNNER_TEST_CONTAINER"
AUTHORIZED_CONTAINER = "trackflow-pg-f2-933cf5b"
PRESERVED_CONTAINER = "trackflow-pg-f2-933cf5b-preserved-part3"
PROTECTED_URL_ENVS = (
    "DATABASE_URL",
    "TRACKFLOW_PIPELINE_DATABASE_URL",
    "TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL",
    "TRACKFLOW_REPORTING_DATABASE_URL",
    "TRACKFLOW_PUBLICATION_TEST_DATABASE_URL",
)
ROOT = Path(__file__).parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv(TEST_URL_ENV) or not os.getenv(TEST_CONTAINER_ENV),
    reason=f"{TEST_URL_ENV} and {TEST_CONTAINER_ENV} are required for disposable PostgreSQL QA",
)


@pytest.fixture(scope="module")
def job_engine():
    raw_url = os.getenv(TEST_URL_ENV)
    container_name = os.getenv(TEST_CONTAINER_ENV)
    assert raw_url is not None and container_name is not None
    if container_name == PRESERVED_CONTAINER or container_name != AUTHORIZED_CONTAINER:
        pytest.fail("job-runner tests require the explicitly authorized disposable container")
    for protected_name in PROTECTED_URL_ENVS:
        if os.getenv(protected_name) == raw_url:
            pytest.fail(f"{TEST_URL_ENV} must not reuse {protected_name}")
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("job-runner test database must be PostgreSQL")
    if url.host not in {"127.0.0.1", "localhost"} or url.port is None:
        pytest.fail("job-runner test database must use loopback with an explicit port")

    inspection = subprocess.run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{json .State.Running}}|{{json .State.Health.Status}}|"
            "{{json (index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostIp}}|"
            "{{json (index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}|"
            "{{json (index .HostConfig.Tmpfs \"/var/lib/postgresql/data\")}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().split("|")
    running, health, host_ip, host_port, tmpfs = map(json.loads, inspection)
    if not (
        running is True
        and health == "healthy"
        and host_ip == "127.0.0.1"
        and int(host_port) == url.port
        and tmpfs
    ):
        pytest.fail("job-runner tests require the healthy loopback tmpfs container")

    engine = create_engine(raw_url, pool_pre_ping=True)
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT current_database() AS database_name, current_user AS user_name")
        ).mappings().one()
    if identity != {"database_name": "trackflow_test", "user_name": "trackflow_test"}:
        engine.dispose()
        pytest.fail("job-runner tests require the authorized trackflow_test identity")

    ddl = (ROOT / "infra/sql/job_runs.sql").read_text(encoding="utf-8")
    with engine.begin() as connection:
        connection.exec_driver_sql(ddl)
    yield engine
    engine.dispose()


@pytest.fixture
def job_scope(job_engine):
    job_name = f"dev53-{uuid4().hex}"
    try:
        yield job_engine, job_name
    finally:
        with job_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job_runs WHERE job_name = :job_name"),
                {"job_name": job_name},
            )


def test_first_acquisition_persists_processing_with_timestamps(job_scope):
    engine, job_name = job_scope
    target = date(2001, 1, 1)
    now = datetime(2001, 1, 2, 0, 15, tzinfo=timezone.utc)

    result = acquire_job_run(engine, job_name, target, now=now)
    stored = get_job_run(engine, result.run.id)

    assert result.status == ACQUIRED
    assert stored is not None
    assert stored.status == STATUS_PROCESSING
    assert stored.started_at == now
    assert stored.created_at == now
    assert stored.finished_at is None
    with engine.connect() as connection:
        temporal_constraint = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid='job_runs'::regclass "
                "AND conname='job_runs_started_after_created'"
            )
        ).scalar_one()
    assert temporal_constraint == 1


def test_same_date_concurrency_has_one_owner_and_no_pending(job_scope):
    engine, job_name = job_scope
    target = date(2001, 1, 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: acquire_job_run(engine, job_name, target), range(2)))

    assert sorted(result.status for result in results) == [ACQUIRED, SKIPPED_PROCESSING]
    with engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT count(*) FILTER (WHERE status='processing') AS processing, "
                "count(*) FILTER (WHERE status='pending') AS pending FROM job_runs "
                "WHERE job_name=:job_name"
            ),
            {"job_name": job_name},
        ).mappings().one()
    assert counts == {"processing": 1, "pending": 0}


def test_different_dates_cannot_process_same_job_in_parallel(job_scope):
    engine, job_name = job_scope
    targets = (date(2001, 1, 3), date(2001, 1, 4))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda target: acquire_job_run(engine, job_name, target), targets))

    assert sorted(result.status for result in results) == [ACQUIRED, SKIPPED_PROCESSING]
    assert sum(result.run.status == STATUS_PROCESSING for result in results) == 2
    assert len({result.run.id for result in results}) == 1


def test_completed_date_is_idempotent_and_other_date_can_start(job_scope):
    engine, job_name = job_scope
    target = date(2001, 1, 5)
    started_at = datetime(2001, 1, 6, 0, 15, tzinfo=timezone.utc)
    acquired = acquire_job_run(engine, job_name, target, now=started_at)
    finished = complete_job_run(
        engine,
        acquired.run.id,
        now=started_at - timedelta(hours=1),
    )

    duplicate = acquire_job_run(engine, job_name, target)
    next_date = acquire_job_run(engine, job_name, target + timedelta(days=1))

    assert finished.changed and finished.run.status == STATUS_COMPLETED
    assert finished.run.finished_at == started_at
    assert duplicate.status == SKIPPED_COMPLETED
    assert duplicate.run.id == acquired.run.id
    assert next_date.status == ACQUIRED


def test_failed_attempt_is_preserved_and_same_date_can_retry(job_scope):
    engine, job_name = job_scope
    target = date(2001, 1, 7)
    first = acquire_job_run(engine, job_name, target)
    failed = fail_job_run(
        engine,
        first.run.id,
        "password=do-not-store postgresql://user:pw@host/db",
    )
    retry = acquire_job_run(engine, job_name, target)

    assert failed.changed and failed.run.status == STATUS_FAILED
    assert failed.run.finished_at is not None
    assert "do-not-store" not in failed.run.error_message
    assert "postgresql://" not in failed.run.error_message
    assert retry.status == ACQUIRED
    assert retry.run.id != first.run.id
    assert get_job_run(engine, first.run.id).status == STATUS_FAILED
    assert get_latest_job_run(engine, job_name, target).id == retry.run.id


def test_invalid_terminal_transition_is_compare_and_set_noop(job_scope):
    engine, job_name = job_scope
    acquired = acquire_job_run(engine, job_name, date(2001, 1, 8))
    completed = complete_job_run(engine, acquired.run.id)
    incompatible = fail_job_run(engine, acquired.run.id, "late failure")

    assert completed.changed is True
    assert incompatible.changed is False
    assert incompatible.run.status == STATUS_COMPLETED
    assert get_job_run(engine, acquired.run.id).status == STATUS_COMPLETED


def test_database_error_during_acquisition_rolls_back_pending_insert(
    job_scope, monkeypatch
):
    engine, job_name = job_scope
    monkeypatch.setattr(job_run_service, "_START_PROCESSING", text("SELECT 1 / 0"))

    with pytest.raises(job_run_service.JobRunPersistenceError):
        acquire_job_run(engine, job_name, date(2001, 1, 9))

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM job_runs WHERE job_name = :job_name"),
            {"job_name": job_name},
        ).scalar_one()
    assert count == 0
