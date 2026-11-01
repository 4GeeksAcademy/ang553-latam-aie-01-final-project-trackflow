"""Opt-in DEV-53 E2E across job state, export, image and real pipeline.

The test never starts Supercronic. It overrides the scheduler image command
with one ``python -m scripts.nightly_export`` execution and owns two empty,
disposable PostgreSQL databases plus one uniquely named Docker volume.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

import services.api.inventory_service as inventory_service
from pipelines.database_identity import ensure_distinct_postgresql_databases
from pipelines.pipeline import RULES_VERSION
from services.api.inventory_models import SKU
from services.api.inventory_schemas import (
    CommercialOrderCreate,
    InventoryPhysicalCountCreate,
    StockEntryCreate,
)
from services.api.telemetry_storage import telemetry_metadata
from services.job_runner import (
    ACQUIRED,
    SKIPPED_PROCESSING,
    acquire_job_run,
    fail_job_run,
)
from tests.test_operational_outbox_postgres import (
    REPORTING_URL_ENV,
    SOURCE_URL_ENV,
    disposable_part2_databases,
)


ROOT = Path(__file__).parents[1]
LEGACY_QA_CONTAINER = "trackflow-pg-f2-933cf5b"
PRESERVED_CONTAINER = "trackflow-pg-f2-933cf5b-preserved-part3"
CONTAINER_ENV = "TRACKFLOW_NIGHTLY_E2E_POSTGRES_CONTAINER"
IMAGE_ENV = "TRACKFLOW_NIGHTLY_E2E_IMAGE"
VOLUME_ENV = "TRACKFLOW_NIGHTLY_E2E_VOLUME"
DEFAULT_IMAGE = "trackflow-nightly-scheduler:dev53-qa"
SERIALIZATION_ERRORS = (
    "Unable to serialize unknown type",
    "cannot pickle '_thread.RLock' object",
)
PROTECTED_URL_ENVS = (
    "DATABASE_URL",
    "TRACKFLOW_PIPELINE_DATABASE_URL",
    "TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL",
    "TRACKFLOW_REPORTING_DATABASE_URL",
    "TRACKFLOW_JOB_RUNNER_TEST_DATABASE_URL",
    "TRACKFLOW_NIGHTLY_EXPORT_TEST_DATABASE_URL",
    "TRACKFLOW_PUBLICATION_TEST_DATABASE_URL",
)

pytestmark = pytest.mark.skipif(
    not os.getenv(SOURCE_URL_ENV)
    or not os.getenv(REPORTING_URL_ENV)
    or not os.getenv(CONTAINER_ENV)
    or not os.getenv(IMAGE_ENV)
    or not os.getenv(VOLUME_ENV),
    reason=(
        f"{SOURCE_URL_ENV}, {REPORTING_URL_ENV}, and {VOLUME_ENV} are required "
        "for the destructive disposable E2E"
    ),
)


def _docker(*arguments: str, check: bool = False, **kwargs):
    return subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        check=check,
        **kwargs,
    )


def _preflight(
    source_url: str,
    reporting_url: str,
    postgres_container: str,
    image: str,
    volume: str,
) -> None:
    if not re.fullmatch(r"trackflow-nightly-scheduler:dev53-e2e-[a-z0-9-]+", image):
        pytest.fail("E2E image name is not safely scoped", pytrace=False)
    if not re.fullmatch(r"trackflow-dev53-e2e-[a-z0-9-]+", postgres_container):
        pytest.fail("E2E PostgreSQL container name is not safely scoped", pytrace=False)
    if postgres_container in {LEGACY_QA_CONTAINER, PRESERVED_CONTAINER}:
        pytest.fail("E2E must not use a preserved QA PostgreSQL container", pytrace=False)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{5,127}", volume):
        pytest.fail("E2E volume name is not safely scoped", pytrace=False)
    if volume in {"telemetry-raw", "trackflow-telemetry-raw"}:
        pytest.fail("E2E requires an exclusive QA volume name", pytrace=False)
    if _docker("volume", "inspect", volume).returncode == 0:
        pytest.fail("exclusive E2E volume already exists", pytrace=False)

    source = make_url(source_url)
    reporting = make_url(reporting_url)
    for url in (source, reporting):
        if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
            pytest.fail("E2E URLs must use supported PostgreSQL drivers", pytrace=False)
        if url.host not in {"127.0.0.1", "localhost"} or url.port is None:
            pytest.fail("E2E URLs must use explicit loopback ports", pytrace=False)
        if "test" not in (url.database or "").lower():
            pytest.fail("E2E database names must be visibly disposable", pytrace=False)
    if source_url == reporting_url or source.database == reporting.database:
        pytest.fail("source and reporting E2E databases must differ", pytrace=False)
    for name in PROTECTED_URL_ENVS:
        protected = os.getenv(name)
        if protected and protected in {source_url, reporting_url}:
            pytest.fail(f"E2E URL must not reuse {name}", pytrace=False)

    raw = _docker("inspect", postgres_container, check=True).stdout
    inspection = json.loads(raw)[0]
    inspected_name = inspection["Name"].lstrip("/")
    if inspected_name != postgres_container:
        pytest.fail("E2E inspected an unexpected PostgreSQL container", pytrace=False)
    binding = inspection["NetworkSettings"]["Ports"]["5432/tcp"][0]
    if not (
        inspection["State"]["Running"] is True
        and inspection["State"]["Health"]["Status"] == "healthy"
        and binding["HostIp"] == "127.0.0.1"
        and int(binding["HostPort"]) == source.port == reporting.port
        and inspection["HostConfig"]["Tmpfs"].get("/var/lib/postgresql/data")
    ):
        pytest.fail("authorized PostgreSQL must be healthy loopback tmpfs", pytrace=False)

    source_engine = create_engine(source_url, pool_pre_ping=True)
    reporting_engine = create_engine(reporting_url, pool_pre_ping=True)
    try:
        ensure_distinct_postgresql_databases(source_engine, reporting_engine)
        identities = []
        for engine in (source_engine, reporting_engine):
            with engine.connect() as connection:
                identity = connection.execute(
                    text(
                        "SELECT current_database() AS database_name, "
                        "current_user AS user_name, "
                        "(SELECT oid FROM pg_database "
                        "WHERE datname=current_database()) AS database_oid, "
                        "(SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE') "
                        "AS public_table_count, "
                        "to_regnamespace('reporting') AS reporting_schema"
                    )
                ).mappings().one()
            identities.append(identity)
        if identities[0]["database_oid"] == identities[1]["database_oid"]:
            pytest.fail("E2E database OIDs must differ", pytrace=False)
        for identity in identities:
            if (
                "test" not in identity["database_name"].lower()
                or "test" not in identity["user_name"].lower()
                or identity["public_table_count"] != 0
                or identity["reporting_schema"] is not None
            ):
                pytest.fail(
                    "E2E databases must be empty disposable test identities",
                    pytrace=False,
                )
    finally:
        source_engine.dispose()
        reporting_engine.dispose()

    image_config = json.loads(_docker("image", "inspect", image, check=True).stdout)[0][
        "Config"
    ]
    if image_config["User"] != "10001:10001":
        pytest.fail("scheduler image must run as the approved non-root user", pytrace=False)
    if image_config["Cmd"] != [
        "/usr/local/bin/supercronic",
        "/app/infra/scheduler/crontab",
    ]:
        pytest.fail("scheduler image CMD is not the approved scheduler", pytrace=False)
    if "TZ=UTC" not in image_config["Env"]:
        pytest.fail("scheduler image must be UTC", pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def nightly_e2e_preflight():
    """Run before the provisioning fixture can write either disposable DB."""
    _preflight(
        os.environ[SOURCE_URL_ENV],
        os.environ[REPORTING_URL_ENV],
        os.environ[CONTAINER_ENV],
        os.environ[IMAGE_ENV],
        os.environ[VOLUME_ENV],
    )


def _container_url(raw_url: str) -> str:
    return make_url(raw_url).set(host="host.docker.internal").render_as_string(
        hide_password=False
    )


def _run_one_shot(
    *,
    image: str,
    volume: str,
    environment: dict[str, str],
    name: str,
) -> subprocess.CompletedProcess[str]:
    inherited = dict(os.environ)
    inherited.update(environment)
    arguments = [
        "run",
        "--rm",
        "--name",
        name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--volume",
        f"{volume}:/app/data/raw",
    ]
    for variable in sorted(environment):
        arguments.extend(["--env", variable])
    arguments.extend([image, "python", "-m", "scripts.nightly_export"])
    return _docker(*arguments, env=inherited, timeout=240)


def _artifact_snapshot(image: str, volume: str, target: date) -> dict[str, object]:
    code = (
        "import csv,hashlib,json,pathlib;"
        f"p=pathlib.Path('/app/data/raw/telemetry_{target.isoformat()}.csv');"
        "b=p.read_bytes();"
        "rows=list(csv.DictReader(p.open(encoding='utf-8',newline='')));"
        "print(json.dumps({'sha256':hashlib.sha256(b).hexdigest(),"
        "'rows':len(rows),'header':list(rows[0]) if rows else "
        "next(csv.reader(p.open(encoding='utf-8',newline='')))}))"
    )
    completed = _docker(
        "run",
        "--rm",
        "--volume",
        f"{volume}:/app/data/raw",
        image,
        "python",
        "-c",
        code,
        check=True,
    )
    return json.loads(completed.stdout)


def _seed_authentic_operational_week(source, event_time: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return event_time if tz is not None else event_time.replace(tzinfo=None)

    original_datetime = inventory_service.datetime
    inventory_service.datetime = FrozenDateTime
    try:
        client_id = f"dev53-e2e-client-{uuid4().hex}"
        with Session(source) as session:
            sku = SKU(
                name="DEV-53 authentic E2E SKU",
                sku=f"DEV53-{uuid4().hex}",
                client_name="DEV-53 E2E client",
                client_id=client_id,
                category="electronics",
                warehouse="LA",
                minimum_stock_threshold=3,
            )
            session.add(sku)
            session.commit()
            session.refresh(sku)
            assert sku.id is not None
            inventory_service.create_stock_entry(
                session,
                StockEntryCreate(
                    sku_id=int(sku.id),
                    quantity=10,
                    reference="DEV53-E2E-INBOUND",
                    warehouse="LA",
                ),
                user_uuid="dev53-e2e",
            )
            inventory_service.create_commercial_order(
                session,
                CommercialOrderCreate(
                    order_id=uuid4(),
                    client_id=client_id,
                    warehouse="LA",
                    dispatches=[
                        {
                            "dispatch_id": uuid4(),
                            "sku_id": int(sku.id),
                            "quantity": 8,
                            "tracking_number": "DEV53-E2E-DISPATCH",
                        }
                    ],
                ),
                user_uuid="dev53-e2e",
            )
            inventory_service.confirm_inventory_physical_count(
                session,
                InventoryPhysicalCountCreate(
                    count_id=uuid4(),
                    sku_id=int(sku.id),
                    observed_quantity=1,
                    warehouse="LA",
                ),
                user_uuid="dev53-e2e",
            )
    finally:
        inventory_service.datetime = original_datetime


def _assert_safe_logs(completed, *forbidden: str) -> str:
    logs = completed.stdout + completed.stderr
    for value in forbidden:
        assert value not in logs
    return logs


def test_scheduler_image_nightly_export_and_real_pipeline_e2e(
    disposable_part2_databases,
):
    source, reporting = disposable_part2_databases
    source_url = os.environ[SOURCE_URL_ENV]
    reporting_url = os.environ[REPORTING_URL_ENV]
    image = os.getenv(IMAGE_ENV, DEFAULT_IMAGE)
    volume = os.environ[VOLUME_ENV]
    ensure_distinct_postgresql_databases(source, reporting)
    with source.connect() as connection:
        identity_source = connection.execute(
            text(
                "SELECT current_database() AS database_name, current_user AS user_name, "
                "(SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid"
            )
        ).mappings().one()
        existing = connection.execute(
            text(
                "SELECT to_regclass('public.telemetry_events') AS telemetry, "
                "to_regclass('public.job_runs') AS jobs"
            )
        ).mappings().one()
    with reporting.connect() as connection:
        identity_reporting = connection.execute(
            text(
                "SELECT current_database() AS database_name, current_user AS user_name, "
                "(SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid"
            )
        ).mappings().one()
    assert identity_source["database_oid"] != identity_reporting["database_oid"]
    assert identity_source["database_name"] != identity_reporting["database_name"]
    assert existing == {"telemetry": None, "jobs": None}

    image_source_url = _container_url(source_url)
    image_reporting_url = _container_url(reporting_url)
    today = datetime.now(timezone.utc).date()
    current_monday = today - timedelta(days=today.weekday())
    week_start = current_monday - timedelta(days=7)
    event_time = datetime.combine(
        week_start + timedelta(days=1), datetime.min.time(), timezone.utc
    )
    success_target = event_time.date()
    retry_target = success_target + timedelta(days=1)
    event_ids = (uuid4(), uuid4())
    volume_created = False
    source_objects_created = False
    try:
        telemetry_metadata.create_all(source)
        source_objects_created = True
        with source.begin() as connection:
            connection.exec_driver_sql(
                (ROOT / "infra/sql/job_runs.sql").read_text(encoding="utf-8")
            )
        _seed_authentic_operational_week(source, event_time)
        with source.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO telemetry_events "
                    "(id, timestamp, service, event_type, level, tags) VALUES "
                    "(:id1, :ts1, 'dev53-e2e', 'api_request_failed', 'error', '{}'::jsonb), "
                    "(:id2, :ts2, 'dev53-e2e', 'api_request_failed', 'error', '{}'::jsonb)"
                ),
                {
                    "id1": event_ids[0],
                    "ts1": datetime.combine(
                        success_target, datetime.min.time(), timezone.utc
                    )
                    + timedelta(hours=12),
                    "id2": event_ids[1],
                    "ts2": datetime.combine(
                        retry_target, datetime.min.time(), timezone.utc
                    )
                    + timedelta(hours=12),
                },
            )
        _docker("volume", "create", volume, check=True)
        volume_created = True

        base_environment = {
            "DATABASE_URL": image_source_url,
            "TRACKFLOW_PIPELINE_DATABASE_URL": image_reporting_url,
            "TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL": image_source_url,
            "TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT": (
                "telemetry=authentic-dev53;movements=authentic-dev53;"
                "stockexit=authentic-dev53;rules=weekly-performance-v1"
            ),
            "TRACKFLOW_PIPELINE_RULES_VERSION": RULES_VERSION,
            "PREFECT_LOGGING_LEVEL": "ERROR",
            "TZ": "UTC",
        }
        first_environment = {
            **base_environment,
            "TARGET_DATE": success_target.isoformat(),
        }
        first_name = f"dev53-e2e-first-{uuid4().hex[:12]}"
        first = _run_one_shot(
            image=image,
            volume=volume,
            environment=first_environment,
            name=first_name,
        )
        first_logs = _assert_safe_logs(
            first,
            *SERIALIZATION_ERRORS,
            source_url,
            reporting_url,
            image_source_url,
            image_reporting_url,
        )
        assert first.returncode == 0, first_logs
        assert '"result":"acquired"' in first_logs
        assert '"terminal":"completed"' in first_logs
        first_artifact = _artifact_snapshot(image, volume, success_target)
        assert first_artifact["rows"] == 1
        assert first_artifact["header"] == [
            "id",
            "timestamp",
            "service",
            "event_type",
            "level",
            "value",
            "message",
            "tags",
        ]
        with source.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM job_runs WHERE job_name='nightly_export' "
                    "AND target_date=:target_date"
                ),
                {"target_date": success_target},
            ).scalar_one() == "completed"
        with reporting.connect() as connection:
            first_publication = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM telemetry_events) AS telemetry, "
                    "(SELECT count(*) FROM reporting.weekly_warehouse_client_performance) AS kpis, "
                    "(SELECT count(*) FROM reporting.pipeline_publication_runs) AS runs"
                )
            ).mappings().one()
        assert first_publication["telemetry"] == 4
        assert first_publication["kpis"] == 1

        second = _run_one_shot(
            image=image,
            volume=volume,
            environment=first_environment,
            name=f"dev53-e2e-repeat-{uuid4().hex[:12]}",
        )
        second_logs = _assert_safe_logs(
            second,
            *SERIALIZATION_ERRORS,
            source_url,
            reporting_url,
            image_source_url,
            image_reporting_url,
        )
        assert second.returncode == 0, second_logs
        assert "skipped_completed" in second_logs
        assert _artifact_snapshot(image, volume, success_target) == first_artifact
        with reporting.connect() as connection:
            repeated_publication = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM telemetry_events) AS telemetry, "
                    "(SELECT count(*) FROM reporting.weekly_warehouse_client_performance) AS kpis, "
                    "(SELECT count(*) FROM reporting.pipeline_publication_runs) AS runs"
                )
            ).mappings().one()
        assert repeated_publication == first_publication

        invalid_environment = {
            key: value
            for key, value in base_environment.items()
            if key != "TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT"
        }
        invalid_environment["TARGET_DATE"] = retry_target.isoformat()
        failed_name = f"dev53-e2e-failed-{uuid4().hex[:12]}"
        failed = _run_one_shot(
            image=image,
            volume=volume,
            environment=invalid_environment,
            name=failed_name,
        )
        failed_logs = _assert_safe_logs(
            failed,
            *SERIALIZATION_ERRORS,
            source_url,
            reporting_url,
            image_source_url,
            image_reporting_url,
        )
        assert failed.returncode == 1
        assert '"terminal":"failed"' in failed_logs
        failed_artifact = _artifact_snapshot(image, volume, retry_target)
        assert failed_artifact["rows"] == 1
        assert _docker("container", "inspect", failed_name).returncode != 0
        with source.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM job_runs WHERE job_name='nightly_export' "
                    "AND target_date=:target_date ORDER BY created_at"
                ),
                {"target_date": retry_target},
            ).scalars().all() == ["failed"]

        retry_environment = {
            **base_environment,
            "TARGET_DATE": retry_target.isoformat(),
        }
        retried = _run_one_shot(
            image=image,
            volume=volume,
            environment=retry_environment,
            name=f"dev53-e2e-retry-{uuid4().hex[:12]}",
        )
        retried_logs = _assert_safe_logs(
            retried,
            *SERIALIZATION_ERRORS,
            source_url,
            reporting_url,
            image_source_url,
            image_reporting_url,
        )
        assert retried.returncode == 0, retried_logs
        assert _artifact_snapshot(image, volume, retry_target) == failed_artifact
        with source.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM job_runs WHERE job_name='nightly_export' "
                    "AND target_date=:target_date ORDER BY created_at"
                ),
                {"target_date": retry_target},
            ).scalars().all() == ["failed", "completed"]
        with reporting.connect() as connection:
            retried_publication = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM telemetry_events) AS telemetry, "
                    "(SELECT count(*) FROM reporting.weekly_warehouse_client_performance) AS kpis, "
                    "(SELECT count(*) FROM reporting.pipeline_publication_runs) AS runs"
                )
            ).mappings().one()
        assert retried_publication["telemetry"] == first_publication["telemetry"]
        assert retried_publication["kpis"] == first_publication["kpis"]
        assert retried_publication["runs"] == first_publication["runs"] + 1

        concurrency_dates = (date(2003, 3, 3), date(2003, 3, 4), date(2003, 3, 5))
        with ThreadPoolExecutor(max_workers=2) as pool:
            same_results = list(
                pool.map(
                    lambda _: acquire_job_run(
                        source, "nightly_export", concurrency_dates[0]
                    ),
                    range(2),
                )
            )
        assert sorted(result.status for result in same_results) == [
            ACQUIRED,
            SKIPPED_PROCESSING,
        ]
        winner = next(result for result in same_results if result.acquired)
        fail_job_run(source, winner.run.id, "controlled E2E concurrency cleanup")

        with ThreadPoolExecutor(max_workers=2) as pool:
            different_results = list(
                pool.map(
                    lambda target: acquire_job_run(source, "nightly_export", target),
                    concurrency_dates[1:],
                )
            )
        assert sorted(result.status for result in different_results) == [
            ACQUIRED,
            SKIPPED_PROCESSING,
        ]
        different_winner = next(result for result in different_results if result.acquired)
        with source.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT count(*) FILTER (WHERE status='pending') AS pending, "
                    "count(*) FILTER (WHERE status='processing') AS processing "
                    ", count(*) AS total "
                    "FROM job_runs WHERE job_name='nightly_export' "
                    "AND target_date = ANY(:dates)"
                ),
                {"dates": list(concurrency_dates)},
            ).mappings().one()
        assert counts == {"pending": 0, "processing": 1, "total": 2}
        fail_job_run(
            source,
            different_winner.run.id,
            "controlled E2E concurrency cleanup",
        )
    finally:
        try:
            if source_objects_created:
                with source.begin() as connection:
                    connection.exec_driver_sql("DROP TABLE IF EXISTS job_runs")
                telemetry_metadata.drop_all(source)
        finally:
            if volume_created:
                _docker("volume", "rm", volume, check=True)
