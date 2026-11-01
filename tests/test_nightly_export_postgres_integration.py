"""Opt-in DEV-53 Fase 2 PostgreSQL and filesystem integration.

This module never reads DATABASE_URL.  It requires an explicitly configured,
isolated test database and writes artifacts only below pytest's tmp_path.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import nightly_export


TEST_URL_ENV = "TRACKFLOW_NIGHTLY_EXPORT_TEST_DATABASE_URL"
TEST_CONTAINER_ENV = "TRACKFLOW_NIGHTLY_EXPORT_TEST_CONTAINER"
AUTHORIZED_CONTAINER = "trackflow-pg-f2-933cf5b"
PRESERVED_CONTAINER = "trackflow-pg-f2-933cf5b-preserved-part3"
PROTECTED_URL_ENVS = (
    "DATABASE_URL",
    "TRACKFLOW_PIPELINE_DATABASE_URL",
    "TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL",
    "TRACKFLOW_REPORTING_DATABASE_URL",
    "TRACKFLOW_JOB_RUNNER_TEST_DATABASE_URL",
)

pytestmark = pytest.mark.skipif(
    not os.getenv(TEST_URL_ENV) or not os.getenv(TEST_CONTAINER_ENV),
    reason=f"{TEST_URL_ENV} and {TEST_CONTAINER_ENV} are required for isolated PostgreSQL integration",
)


@pytest.fixture
def postgres_engine():
    test_url = os.environ[TEST_URL_ENV]
    container_name = os.environ[TEST_CONTAINER_ENV]
    if container_name == PRESERVED_CONTAINER or container_name != AUTHORIZED_CONTAINER:
        pytest.fail("nightly export tests require the authorized disposable container")
    if any(test_url == os.getenv(name) for name in PROTECTED_URL_ENVS):
        pytest.fail(f"{TEST_URL_ENV} must not reuse a protected database URL")
    url = make_url(test_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("nightly export integration requires PostgreSQL")
    if url.host not in {"127.0.0.1", "localhost"} or url.port is None:
        pytest.fail("nightly export integration requires explicit loopback PostgreSQL")
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
        pytest.fail("nightly export integration requires healthy loopback tmpfs")
    engine = create_engine(test_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text("SELECT current_database() AS database_name, current_user AS user_name")
            ).mappings().one()
            tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() "
                        "AND table_name IN ('telemetry_events', 'job_runs')"
                    )
                ).scalars()
            )
        if identity != {"database_name": "trackflow_test", "user_name": "trackflow_test"}:
            pytest.fail("nightly export integration requires trackflow_test identity")
        if tables != {"telemetry_events", "job_runs"}:
            pytest.fail("isolated database must be pre-provisioned for DEV-53")
        yield engine
    finally:
        engine.dispose()


def test_real_postgres_export_and_filesystem_publication(postgres_engine, tmp_path):
    target = date(2002, 2, 2)
    included = uuid4()
    excluded = uuid4()
    with postgres_engine.connect() as connection:
        existing = connection.execute(
            text(
                "SELECT count(*) FROM telemetry_events "
                "WHERE timestamp >= :start_at AND timestamp < :end_at"
            ),
            {
                "start_at": datetime(2002, 2, 2, tzinfo=timezone.utc),
                "end_at": datetime(2002, 2, 3, tzinfo=timezone.utc),
            },
        ).scalar_one()
    if existing:
        pytest.fail("reserved nightly-export test day must be empty")
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO telemetry_events "
                "(id, timestamp, service, event_type, level, value, message, tags) "
                "VALUES (:id1, :ts1, 'dev53-test', 'api_request_failed', 'error', "
                "1.2500, 'safe test row', CAST(:tags1 AS jsonb)), "
                "(:id2, :ts2, 'dev53-test', 'api_request_failed', 'error', "
                "NULL, NULL, CAST(:tags2 AS jsonb))"
            ),
            {
                "id1": included,
                "ts1": datetime(2002, 2, 2, 12, tzinfo=timezone.utc),
                "tags1": json.dumps({"b": 2, "a": 1}),
                "id2": excluded,
                "ts2": datetime(2002, 2, 3, tzinfo=timezone.utc),
                "tags2": "{}",
            },
        )
    try:
        final = nightly_export.export_telemetry_day(postgres_engine, target, tmp_path)
        with final.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert [row["id"] for row in rows] == [str(included)]
        assert rows[0]["tags"] == '{"a":1,"b":2}'
        assert rows[0]["value"] == "1.2500"
        assert nightly_export.export_telemetry_day(postgres_engine, target, tmp_path) == final
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM telemetry_events WHERE id IN (:id1, :id2)"),
                {"id1": included, "id2": excluded},
            )


def test_real_postgres_job_lifecycle_export_and_pipeline_boundary(
    postgres_engine, tmp_path
):
    target = date(2002, 2, 4)
    event_id = uuid4()
    cleanup_key = {"job_name": nightly_export.JOB_NAME, "target_date": target}
    with postgres_engine.connect() as connection:
        existing_runs = connection.execute(
            text(
                "SELECT count(*) FROM job_runs "
                "WHERE job_name = :job_name AND target_date = :target_date"
            ),
            cleanup_key,
        ).scalar_one()
        existing_events = connection.execute(
            text(
                "SELECT count(*) FROM telemetry_events "
                "WHERE timestamp >= :start_at AND timestamp < :end_at"
            ),
            {
                "start_at": datetime(2002, 2, 4, tzinfo=timezone.utc),
                "end_at": datetime(2002, 2, 5, tzinfo=timezone.utc),
            },
        ).scalar_one()
    if existing_runs or existing_events:
        pytest.fail("reserved nightly-export lifecycle test key must be empty")
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO telemetry_events "
                "(id, timestamp, service, event_type, level, tags) "
                "VALUES (:id, :timestamp, 'dev53-test', "
                "'api_request_failed', 'error', '{}'::jsonb)"
            ),
            {
                "id": event_id,
                "timestamp": datetime(2002, 2, 4, 12, tzinfo=timezone.utc),
            },
        )
    pipeline_calls = []
    try:
        result = nightly_export.execute_nightly_run(
            postgres_engine,
            target,
            output_directory=tmp_path,
            pipeline_runner=lambda: pipeline_calls.append("called"),
        )
        assert result == "completed"
        assert pipeline_calls == ["called"]
        assert nightly_export.daily_csv_path(tmp_path, target).is_file()
        with postgres_engine.connect() as connection:
            run = connection.execute(
                text(
                    "SELECT status, started_at, finished_at FROM job_runs "
                    "WHERE job_name = :job_name AND target_date = :target_date"
                ),
                cleanup_key,
            ).mappings().one()
        assert run["status"] == "completed"
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM telemetry_events WHERE id = :id"),
                {"id": event_id},
            )
            connection.execute(
                text(
                    "DELETE FROM job_runs "
                    "WHERE job_name = :job_name AND target_date = :target_date"
                ),
                cleanup_key,
            )
