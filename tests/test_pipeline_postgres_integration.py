"""Real PostgreSQL integration coverage for the weekly publication pipeline.

The suite only runs when ``TRACKFLOW_PUBLICATION_TEST_DATABASE_URL`` identifies
an isolated PostgreSQL database and refuses to reuse ``DATABASE_URL``.  Every
case owns a historical week/client namespace and removes only those rows.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from pipelines.concurrency import (
    ConcurrencyError,
    acquire_week_lease,
    release_week_lease,
    verify_week_lease,
)
from pipelines.coordinator import (
    REQUIRED_COVERAGE_EVENTS,
    SYNTHETIC_COVERAGE_SOURCE,
    ConfirmedLeaseRight,
    CoverageEvidence,
    publish_weekly_business_report,
    resolve_uncertain_publication,
)
from pipelines.publication import _stage_weekly_upsert
from pipelines.run_store import (
    STATUS_BLOCKED_QUALITY,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_SUCCEEDED_NOOP,
    create_run,
    get_run,
    get_week_control,
    record_valid_publication,
    update_run,
)
from pipelines.weekly_performance import QualityReport, WeeklyAggregate

TEST_URL_ENV = "TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"
FINGERPRINT = "telemetry=f2;movements=f2;stockexit=f2;rules=f2-contract-v1"
RULES_VERSION = "f2-contract-v1"


def _isolated_engine():
    url = os.getenv(TEST_URL_ENV)
    if not url:
        pytest.skip(f"{TEST_URL_ENV} is required for isolated PostgreSQL integration")
    if url == os.getenv("DATABASE_URL"):
        pytest.fail("Refusing to reuse DATABASE_URL as the isolated PostgreSQL test database")
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("F2 integration requires PostgreSQL")
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT current_database() AS database_name, current_user AS user_name")
        ).mappings().one()
        if identity["database_name"] != "trackflow_test" or identity["user_name"] != "trackflow_test":
            engine.dispose()
            pytest.fail("F2 integration is not connected to the authorized isolated database")
        required = connection.execute(
            text(
                "SELECT to_regclass('reporting.weekly_warehouse_client_performance') AS kpi, "
                "to_regclass('reporting.pipeline_publication_runs') AS runs, "
                "to_regclass('reporting.pipeline_publication_control') AS control"
            )
        ).mappings().one()
        if any(required[name] is None for name in ("kpi", "runs", "control")):
            engine.dispose()
            pytest.fail("F2 integration requires the pre-applied reporting DDL")
    return engine


@dataclass(frozen=True)
class _Scope:
    week: date
    client_id: str


@contextmanager
def _isolated_scope(week: date):
    engine = _isolated_engine()
    scope = _Scope(week=week, client_id=f"f2-{uuid4()}")
    _cleanup(engine, scope)
    try:
        yield engine, scope
    finally:
        _cleanup(engine, scope)
        engine.dispose()


def _cleanup(engine, scope: _Scope) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM reporting.weekly_warehouse_client_performance "
                "WHERE week_start = :week_start AND client_id = :client_id"
            ),
            {"week_start": scope.week, "client_id": scope.client_id},
        )
        connection.execute(
            text("DELETE FROM reporting.pipeline_publication_control WHERE week_start = :week_start"),
            {"week_start": scope.week},
        )
        connection.execute(
            text("DELETE FROM reporting.pipeline_publication_runs WHERE target_week_start = :week_start"),
            {"week_start": scope.week},
        )


def _aggregate(scope: _Scope, *, inbound: int = 5) -> WeeklyAggregate:
    return WeeklyAggregate(
        warehouse="los_angeles",
        client_id=scope.client_id,
        week_start=scope.week,
        inbound_units_count=inbound,
        outbound_orders_count=2,
        stockout_events_count=1,
        discrepancy_events_count=1,
        discrepancy_rate=Decimal("0.5"),
    )


def _quality(*, coverage_pending: tuple[str, ...] = ()) -> QualityReport:
    return QualityReport(
        accepted_event_ids=("f2-event",),
        rejected_event_ids=(),
        duplicate_event_ids_consolidated=(),
        duplicate_movement_keys_consolidated=(),
        duplicate_movement_event_ids_consolidated=(),
        conflict_movement_keys=(),
        issues=(),
        cross_week_verification_pending=(),
        coverage_pending=coverage_pending,
    )


def _coverage() -> CoverageEvidence:
    return CoverageEvidence(
        confirmed_event_types=REQUIRED_COVERAGE_EVENTS,
        dispatch_order_ids=(("f2-movement", "f2-order"),),
        lookup_complete=True,
        source=SYNTHETIC_COVERAGE_SOURCE,
    )


def _create_committed_run(engine, week: date) -> UUID:
    with engine.begin() as connection:
        return create_run(
            connection,
            target_week_start=week,
            trigger_type="f2-integration",
            started_at=datetime.combine(week + timedelta(days=7), datetime.min.time(), timezone.utc),
        ).run_id


def _acquire_committed_lease(engine, week: date, run_id: UUID, *, seconds: int = 900):
    with engine.begin() as connection:
        decision = acquire_week_lease(connection, week, run_id, lease_seconds=seconds)
    assert decision.status in {"acquired", "recovered"}
    return decision


def _assert_locking_fencing_and_contention() -> None:
    with _isolated_scope(date(2024, 1, 1)) as (engine, scope):
        owner = _create_committed_run(engine, scope.week)
        contender = _create_committed_run(engine, scope.week)

        owner_connection = engine.connect()
        owner_transaction = owner_connection.begin()
        try:
            granted = acquire_week_lease(owner_connection, scope.week, owner, lease_seconds=900)
            assert granted.status == "acquired"

            blocked_connection = engine.connect()
            blocked_transaction = blocked_connection.begin()
            try:
                blocked_connection.exec_driver_sql("SET LOCAL statement_timeout = '400ms'")
                with pytest.raises(ConcurrencyError) as blocked:
                    acquire_week_lease(blocked_connection, scope.week, contender, lease_seconds=900)
                assert blocked.value.code == "transaction_uncertain"
                blocked_transaction.rollback()
            finally:
                blocked_connection.close()

            owner_transaction.commit()
        finally:
            if owner_transaction.is_active:
                owner_transaction.rollback()
            owner_connection.close()

        with engine.begin() as connection:
            occupied = acquire_week_lease(connection, scope.week, contender, lease_seconds=900)
            assert occupied.status == "occupied"
            assert occupied.owner_run_id == owner

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE reporting.pipeline_publication_control "
                    "SET lease_acquired_at = clock_timestamp() - interval '2 seconds', "
                    "lease_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE week_start = :week_start"
                ),
                {"week_start": scope.week},
            )

        recovered = _acquire_committed_lease(engine, scope.week, contender)
        assert recovered.status == "recovered"
        assert recovered.fencing_token == granted.fencing_token + 1

        with engine.begin() as connection:
            assert verify_week_lease(
                connection, scope.week, owner, granted.fencing_token
            ).status == "rejected"
            assert release_week_lease(
                connection, scope.week, owner, granted.fencing_token
            ).status == "rejected"
            assert verify_week_lease(
                connection, scope.week, contender, recovered.fencing_token
            ).status == "verified"


def _assert_coordinator_atomicity_and_recovery() -> None:
    with _isolated_scope(date(2024, 1, 8)) as (engine, scope):
        run_id = _create_committed_run(engine, scope.week)
        lease = _acquire_committed_lease(engine, scope.week, run_id)
        aggregate = _aggregate(scope)
        now = datetime(2024, 1, 15, 1, 0, tzinfo=timezone.utc)

        business = engine.connect()
        operational = engine.connect()
        business.begin()
        operational.begin()
        try:
            result = publish_weekly_business_report(
                week_start=scope.week,
                run_id=run_id,
                aggregates=(aggregate,),
                quality=_quality(),
                coverage=_coverage(),
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
                lease=ConfirmedLeaseRight(
                    week_start=scope.week,
                    run_id=run_id,
                    fencing_token=lease.fencing_token,
                    acquisition_committed=True,
                ),
                business=business,
                operational=operational,
                now=now,
            )
        finally:
            business.close()
            operational.close()

        assert result.status == STATUS_SUCCEEDED
        assert result.business_publication_valid is True
        assert result.production_certified is False

        with engine.begin() as reader:
            persisted = reader.execute(
                text(
                    "SELECT inbound_units_count FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": scope.week, "client_id": scope.client_id},
            ).scalar_one()
            control = get_week_control(reader, scope.week)
            run = get_run(reader, run_id)
        assert persisted == aggregate.inbound_units_count
        assert control.owner_run_id is None
        assert control.last_valid_publication_run_id == run_id
        assert run.status == STATUS_SUCCEEDED

        with engine.begin() as reader:
            recovered = resolve_uncertain_publication(
                reader,
                week_start=scope.week,
                run_id=run_id,
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
                published_row_count=1,
            )
        assert recovered.status == STATUS_SUCCEEDED
        assert recovered.recovered is True
        assert recovered.publication_recorded is True


def _assert_metadata_recovery_without_fault_injection() -> None:
    """Exercise real persisted recovery; network-loss-at-COMMIT remains non-reproducible."""
    _assert_coordinator_atomicity_and_recovery()


def test_prefect_flow_persists_correlation_and_blocks_before_lease_on_empty_evidence():
    from pipelines.pipeline import PipelineRuntime, run_weekly_pipeline

    with _isolated_scope(date(2000, 1, 3)) as (engine, scope):
        runtime = PipelineRuntime(
            engine=engine,
            coverage=_coverage(),
            dependency_fingerprint=FINGERPRINT,
            rules_version=RULES_VERSION,
            trigger_type="f3-postgres-integration",
            clock=lambda: datetime(2026, 1, 19, tzinfo=timezone.utc),
        )
        result = run_weekly_pipeline(runtime, scope.week)
        assert result.status == STATUS_BLOCKED_QUALITY
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, finished_at, records_processed, error_message, "
                    "prefect_flow_run_id, prefect_task_run_id "
                    "FROM reporting.pipeline_publication_runs WHERE run_id = :run_id"
                ),
                {"run_id": result.run_id},
            ).mappings().one()
            control = get_week_control(connection, scope.week)
        assert row["status"] == STATUS_BLOCKED_QUALITY
        assert row["finished_at"] is not None
        assert row["records_processed"] == 0
        assert row["error_message"]
        assert row["prefect_flow_run_id"]
        assert row["prefect_task_run_id"]
        assert control is None


def test_reporting_store_real_postgres_fallback_decimal_latest_and_duplicate_acceptance():
    from services.reporting.store import (
        accept_pipeline_run,
        latest_pipeline_run,
        mark_scheduling_failed,
        weekly_report,
    )

    with _isolated_scope(date(2001, 1, 1)) as (engine, scope):
        publication_run = _create_committed_run(engine, scope.week)
        with engine.begin() as connection:
            update_run(
                connection,
                publication_run,
                expected_status="pending",
                status="running",
                stage_reached="publish",
            )
        with engine.begin() as connection:
            update_run(
                connection,
                publication_run,
                expected_status="running",
                status="succeeded",
                finished_at=datetime(2026, 1, 19, tzinfo=timezone.utc),
                stage_reached="committed",
                commit_result="technically_observed",
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reporting.weekly_warehouse_client_performance "
                    "(warehouse, client_id, week_start, inbound_units_count, "
                    "outbound_orders_count, stockout_events_count, "
                    "discrepancy_events_count, discrepancy_rate) "
                    "VALUES ('los_angeles', :client_id, :week_start, 5, 3, 1, 1, 0.333333333333333333)"
                ),
                {"client_id": scope.client_id, "week_start": scope.week},
            )
            record_valid_publication(
                connection,
                scope.week,
                publication_run,
                published_at=datetime(2026, 1, 19, tzinfo=timezone.utc),
                published_row_count=1,
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
            )
        with engine.connect() as connection:
            selected, entries = weekly_report(connection, None)
            latest = latest_pipeline_run(connection)
        assert selected == scope.week
        assert entries[0]["client_id"] == scope.client_id
        assert entries[0]["discrepancy_rate"] == Decimal("0.333333333333333333")
        assert latest["run_id"] == publication_run

        with engine.begin() as connection:
            accepted_run, created = accept_pipeline_run(connection, scope.week)
        with engine.begin() as connection:
            duplicate_run, duplicate_created = accept_pipeline_run(connection, scope.week)
        assert created is True
        assert duplicate_created is False
        assert accepted_run != publication_run
        assert accepted_run == duplicate_run
        with engine.begin() as connection:
            mark_scheduling_failed(
                connection,
                accepted_run,
                datetime.now(timezone.utc),
            )
        with engine.begin() as connection:
            retry_run, retry_created = accept_pipeline_run(connection, scope.week)
        assert retry_created is True
        assert retry_run != accepted_run


def test_reporting_fallback_prefers_greatest_valid_week_not_latest_reprocess_time():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pipelines.run_store import ensure_week_control
    import services.reporting.router as reporting
    from services.api.auth_models import Role, UserInDB
    from services.api.auth_security import get_current_user
    from services.reporting.store import weekly_report

    engine = _isolated_engine()
    old_week = date(2003, 1, 6)
    recent_week = date(2003, 1, 13)
    run_ids = []
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM reporting.pipeline_publication_control "
                "WHERE week_start IN (:old_week, :recent_week)"
            ),
            {"old_week": old_week, "recent_week": recent_week},
        ).scalar_one() == 0
    try:
        for week in (old_week, recent_week):
            run_ids.append(_create_committed_run(engine, week))
        with engine.begin() as connection:
            ensure_week_control(connection, old_week)
            ensure_week_control(connection, recent_week)
            record_valid_publication(
                connection, recent_week, run_ids[1],
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                published_row_count=0,
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
            )
            record_valid_publication(
                connection, old_week, run_ids[0],
                published_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                published_row_count=0,
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
            )
        with engine.connect() as connection:
            selected, entries = weekly_report(connection, None)
        assert selected == recent_week
        assert entries == []

        app = FastAPI()
        app.include_router(reporting.router)
        app.dependency_overrides[get_current_user] = lambda: UserInDB(
            email="manager-fallback@example.com",
            hashed_password="not-used",
            role=Role.manager,
        )
        app.dependency_overrides[reporting.get_reporting_engine] = lambda: engine
        response = TestClient(app).get(
            "/reporting/weekly-warehouse-client-performance"
        )
        assert response.status_code == 200
        assert response.json()["week_start"] == recent_week.isoformat()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM reporting.pipeline_publication_control WHERE week_start IN (:old_week, :recent_week)"),
                {"old_week": old_week, "recent_week": recent_week},
            )
            if run_ids:
                connection.execute(
                    text("DELETE FROM reporting.pipeline_publication_runs WHERE run_id = ANY(:run_ids)"),
                    {"run_ids": run_ids},
                )
        engine.dispose()


def test_default_http_scheduler_runs_prefect_and_blocks_without_technical_coverage(
    monkeypatch, tmp_path
):
    """The real POST scheduler must reach Prefect without CLI-only DB configuration."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import pipelines.pipeline as pipeline
    import services.reporting.router as reporting
    from services.api import auth_security
    from services.api.auth_models import Role, UserInDB

    engine = _isolated_engine()
    week = date(1997, 3, 3)
    namespace = uuid4().hex
    client_id = f"http-scheduler-{namespace}"
    sku_id = int(namespace[:7], 16) + 1
    stockexit_id = sku_id
    event_ids = tuple(uuid4() for _ in range(4))
    coverage_file = tmp_path / "http-coverage.json"
    coverage_file.write_text(
        json.dumps({
            "confirmed_event_types": sorted(REQUIRED_COVERAGE_EVENTS),
            "dispatch_order_ids": [["http-movement", "http-order"]],
            "lookup_complete": True,
            "source": SYNTHETIC_COVERAGE_SOURCE,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv(pipeline.DEPENDENCY_FINGERPRINT_ENV, FINGERPRINT)
    monkeypatch.setenv(pipeline.RULES_VERSION_ENV, RULES_VERSION)
    monkeypatch.setenv(pipeline.COVERAGE_EVIDENCE_FILE_ENV, str(coverage_file))
    monkeypatch.delenv(pipeline.PIPELINE_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv("PREFECT_LOGGING_LEVEL", "ERROR")

    app = FastAPI()
    app.include_router(reporting.router)
    admin = UserInDB(
        id=str(uuid4()),
        email="admin-http-integration@example.com",
        hashed_password="unused",
        role=Role.admin,
    )
    monkeypatch.setattr(
        auth_security,
        "_find_user_by_id",
        lambda user_id: admin if user_id == admin.id else None,
    )
    access_token = auth_security.create_access_token(admin.id)
    app.dependency_overrides[reporting.get_reporting_engine] = lambda: engine
    app.dependency_overrides[reporting.get_pipeline_source_engine] = lambda: engine

    with engine.begin() as connection:
        existing_events = connection.execute(
            text(
                "SELECT count(*) FROM telemetry_events "
                "WHERE timestamp >= :week AND timestamp < :next_week"
            ),
            {"week": week, "next_week": week + timedelta(days=7)},
        ).scalar_one()
        existing_kpis = connection.execute(
            text(
                "SELECT count(*) FROM reporting.weekly_warehouse_client_performance "
                "WHERE week_start = :week"
            ),
            {"week": week},
        ).scalar_one()
        existing_runs = connection.execute(
            text(
                "SELECT count(*) FROM reporting.pipeline_publication_runs "
                "WHERE target_week_start = :week"
            ),
            {"week": week},
        ).scalar_one()
        source_tables = connection.execute(
            text(
                "SELECT to_regclass('sku') AS sku, "
                "to_regclass('stockexit') AS stockexit"
            )
        ).mappings().one()
    assert existing_events == existing_kpis == existing_runs == 0
    assert source_tables == {"sku": "sku", "stockexit": "stockexit"}

    try:
        fixture_events = (
            (
                event_ids[0],
                "inbound_order_created",
                {
                    "warehouse": "los_angeles",
                    "client_id": client_id,
                    "product_id": str(sku_id),
                    "movement_id": f"http-inbound-{namespace}",
                    "quantity": 5,
                },
            ),
            (
                event_ids[1],
                "outbound_order_created",
                {
                    "warehouse": "los_angeles",
                    "client_id": client_id,
                    "product_id": str(sku_id),
                    "movement_id": str(stockexit_id),
                    "quantity": 2,
                },
            ),
            (
                event_ids[2],
                "stock_threshold_triggered",
                {
                    "warehouse": "los_angeles",
                    "client_id": client_id,
                    "product_id": str(sku_id),
                    "quantity": 0,
                },
            ),
            (
                event_ids[3],
                "inventory_discrepancy_detected",
                {
                    "warehouse": "los_angeles",
                    "client_id": client_id,
                    "product_id": str(sku_id),
                    "quantity": 1,
                },
            ),
        )
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO sku (id, client_id) VALUES (:id, :client_id)"),
                {"id": sku_id, "client_id": client_id},
            )
            connection.execute(
                text(
                    "INSERT INTO stockexit "
                    "(id, exit_type, warehouse, quantity, sku_id) "
                    "VALUES (:id, 'dispatch', 'los_angeles', 2, :sku_id)"
                ),
                {"id": stockexit_id, "sku_id": sku_id},
            )
            for offset, (event_id, event_type, tags) in enumerate(fixture_events):
                connection.execute(
                    text(
                        "INSERT INTO telemetry_events "
                        "(id, timestamp, service, event_type, tags) "
                        "VALUES (:id, :timestamp, 'api', :event_type, CAST(:tags AS jsonb))"
                    ),
                    {
                        "id": event_id,
                        "timestamp": datetime(
                            1997, 3, 4 + offset, 12, tzinfo=timezone.utc
                        ),
                        "event_type": event_type,
                        "tags": json.dumps(tags),
                    },
                )
        with TestClient(app) as client:
            unauthorized = client.post(
                "/reporting/pipeline-runs", json={"week_start": week.isoformat()}
            )
            assert unauthorized.status_code == 401

            accepted = client.post(
                "/reporting/pipeline-runs",
                json={"week_start": week.isoformat()},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert accepted.status_code == 202
            assert accepted.json()["published"] is False

            latest = client.get(
                "/reporting/pipeline-runs/latest",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert latest.status_code == 200
            payload = latest.json()
            assert payload["run_id"] == accepted.json()["run_id"]
            assert payload["status"] == STATUS_BLOCKED_QUALITY
            assert payload["block_reason"] == "coverage_insufficient"
            assert payload["commit_result"] == "not_attempted"
            assert payload["prefect_flow_run_id"]
            assert payload["prefect_task_run_id"]

        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week"
                ),
                {"week": week},
            ).scalar_one() == 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM telemetry_events WHERE id = ANY(:event_ids)"),
                {"event_ids": list(event_ids)},
            )
            connection.execute(
                text(
                    "DELETE FROM reporting.pipeline_publication_control "
                    "WHERE week_start = :week"
                ),
                {"week": week},
            )
            connection.execute(
                text(
                    "DELETE FROM reporting.pipeline_publication_runs "
                    "WHERE target_week_start = :week"
                ),
                {"week": week},
            )
            connection.execute(
                text("DELETE FROM stockexit WHERE id = :id"), {"id": stockexit_id}
            )
            connection.execute(text("DELETE FROM sku WHERE id = :id"), {"id": sku_id})
        engine.dispose()


def test_postgres_rollback_removes_kpi_metadata_and_keeps_committed_lease():
    with _isolated_scope(date(2024, 1, 15)) as (engine, scope):
        run_id = _create_committed_run(engine, scope.week)
        lease = _acquire_committed_lease(engine, scope.week, run_id)
        aggregate = _aggregate(scope)

        with engine.connect() as business:
            transaction = business.begin()
            staged = _stage_weekly_upsert(
                business,
                scope.week,
                (aggregate,),
                now=datetime(2024, 1, 22, 1, 0, tzinfo=timezone.utc),
                quality_gate="f2-real-postgres",
            )
            assert staged.status == "staged"
            record_valid_publication(
                business,
                scope.week,
                run_id,
                published_at=datetime(2024, 1, 22, 1, 0, tzinfo=timezone.utc),
                published_row_count=1,
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
            )
            released = release_week_lease(
                business, scope.week, run_id, lease.fencing_token
            )
            assert released.status == "released"
            transaction.rollback()

        with engine.begin() as reader:
            kpi_count = reader.execute(
                text(
                    "SELECT count(*) FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": scope.week, "client_id": scope.client_id},
            ).scalar_one()
            control = get_week_control(reader, scope.week)
        assert kpi_count == 0
        assert control.last_valid_publication_run_id is None
        assert control.owner_run_id == run_id
        assert control.fencing_token == lease.fencing_token


def test_postgres_operational_history_survives_business_rollback():
    with _isolated_scope(date(2024, 1, 22)) as (engine, scope):
        run_id = _create_committed_run(engine, scope.week)
        aggregate = _aggregate(scope)
        now = datetime(2024, 1, 29, 1, 0, tzinfo=timezone.utc)

        business = engine.connect()
        operational = engine.connect()
        business.begin()
        operational.begin()
        try:
            result = publish_weekly_business_report(
                week_start=scope.week,
                run_id=run_id,
                aggregates=(aggregate,),
                quality=_quality(coverage_pending=("f2-unresolved",)),
                coverage=_coverage(),
                dependency_fingerprint=FINGERPRINT,
                rules_version=RULES_VERSION,
                lease=None,
                business=business,
                operational=operational,
                now=now,
            )
            business.rollback()
        finally:
            business.close()
            operational.close()

        assert result.status == STATUS_BLOCKED_QUALITY
        with engine.begin() as reader:
            run = get_run(reader, run_id)
            kpi_count = reader.execute(
                text(
                    "SELECT count(*) FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": scope.week, "client_id": scope.client_id},
            ).scalar_one()
        assert run.status == STATUS_BLOCKED_QUALITY
        assert kpi_count == 0


def test_postgres_identical_republication_is_noop_after_committed_publication():
    with _isolated_scope(date(2024, 1, 29)) as (engine, scope):
        aggregate = _aggregate(scope)
        now = datetime(2024, 2, 5, 1, 0, tzinfo=timezone.utc)

        first_run = _create_committed_run(engine, scope.week)
        first_lease = _acquire_committed_lease(engine, scope.week, first_run)
        first = _publish(engine, scope, first_run, first_lease.fencing_token, aggregate, now)
        assert first.status == STATUS_SUCCEEDED

        with engine.begin() as reader:
            identity = reader.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": scope.week, "client_id": scope.client_id},
            ).mappings().one()

        second_run = _create_committed_run(engine, scope.week)
        second_lease = _acquire_committed_lease(engine, scope.week, second_run)
        second = _publish(engine, scope, second_run, second_lease.fencing_token, aggregate, now)
        assert second.status == STATUS_SUCCEEDED_NOOP
        assert second.unchanged == 1

        with engine.begin() as reader:
            repeated = reader.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": scope.week, "client_id": scope.client_id},
            ).mappings().one()
        assert repeated == identity


def _publish(engine, scope, run_id, fencing_token, aggregate, now):
    business = engine.connect()
    operational = engine.connect()
    business.begin()
    operational.begin()
    try:
        return publish_weekly_business_report(
            week_start=scope.week,
            run_id=run_id,
            aggregates=(aggregate,),
            quality=_quality(),
            coverage=_coverage(),
            dependency_fingerprint=FINGERPRINT,
            rules_version=RULES_VERSION,
            lease=ConfirmedLeaseRight(
                week_start=scope.week,
                run_id=run_id,
                fencing_token=fencing_token,
                acquisition_committed=True,
            ),
            business=business,
            operational=operational,
            now=now,
        )
    finally:
        business.close()
        operational.close()
