"""Real PostgreSQL contract for the Part 2 operational outbox path.

The test is opt-in and owns both disposable databases completely for its
duration.  It refuses pre-existing TrackFlow tables rather than reconciling or
deleting data it did not create.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlmodel import Session

import services.api.inventory_service as inventory_service
import services.api.routes.auth as auth_route
import services.reporting.router as reporting_router
from pipelines.database_identity import (
    DatabaseIsolationError,
    ensure_distinct_postgresql_databases,
)
from pipelines.extraction import extract_weekly_inputs
from pipelines.outbox_relay import relay_inventory_outbox
from pipelines.pipeline import (
    COVERAGE_EVIDENCE_FILE_ENV,
    DEPENDENCY_FINGERPRINT_ENV,
    PIPELINE_DATABASE_URL_ENV,
    PIPELINE_SOURCE_DATABASE_URL_ENV,
    RULES_VERSION_ENV,
)
from pipelines.weekly_performance import transform_weekly_performance
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user, hash_password
from services.api.main import app
from services.api.inventory_models import (
    CommercialOrder,
    InventoryPhysicalCount,
    InventoryThresholdEvaluation,
    SKU,
    StockEntry,
    StockExit,
)
from services.api.inventory_outbox import (
    inventory_event_outbox,
    inventory_outbox_metadata,
)
from services.api.inventory_schemas import (
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    InventoryPhysicalCountCreate,
    StockEntryCreate,
    StockExitCreate,
)
from services.api.telemetry_storage import telemetry_events, telemetry_metadata


SOURCE_URL_ENV = "TRACKFLOW_PART2_SOURCE_TEST_DATABASE_URL"
REPORTING_URL_ENV = "TRACKFLOW_PART2_REPORTING_TEST_DATABASE_URL"
_PROTECTED_URL_ENVS = (
    "DATABASE_URL",
    "TRACKFLOW_REPORTING_DATABASE_URL",
    "TRACKFLOW_PIPELINE_DATABASE_URL",
    "TRACKFLOW_PUBLICATION_TEST_DATABASE_URL",
)
_ROOT = Path(__file__).parents[1]


def _required_url(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is required for disposable PostgreSQL QA")
    try:
        parsed = make_url(value)
    except Exception:
        pytest.fail(f"{name} is not a valid database URL", pytrace=False)
    if not parsed.drivername.startswith("postgresql"):
        pytest.fail(f"{name} must identify PostgreSQL", pytrace=False)
    for protected_name in _PROTECTED_URL_ENVS:
        protected = os.getenv(protected_name)
        if protected and value == protected:
            pytest.fail(
                f"{name} must not reuse {protected_name}",
                pytrace=False,
            )
    return value


def _validated_urls() -> tuple[str, str]:
    source_url = _required_url(SOURCE_URL_ENV)
    reporting_url = _required_url(REPORTING_URL_ENV)
    if source_url == reporting_url:
        pytest.fail("source and reporting test URLs must be distinct", pytrace=False)
    return source_url, reporting_url


def _objects_present(engine, expressions: dict[str, str]) -> tuple[str, ...]:
    projection = ", ".join(
        f"{expression} IS NOT NULL AS {label}" for label, expression in expressions.items()
    )
    with engine.connect() as connection:
        row = connection.execute(text(f"SELECT {projection}")).mappings().one()
    return tuple(label for label, present in row.items() if present)


def _apply_sql_file(engine, relative_path: str) -> None:
    sql = (_ROOT / relative_path).read_text(encoding="utf-8")
    with engine.begin() as connection:
        connection.exec_driver_sql(sql)


@pytest.fixture(scope="module")
def disposable_part2_databases():
    source_url, reporting_url = _validated_urls()

    source = create_engine(source_url, pool_pre_ping=True)
    reporting = create_engine(reporting_url, pool_pre_ping=True)
    source_created = False
    reporting_created = False
    try:
        try:
            ensure_distinct_postgresql_databases(source, reporting)
        except DatabaseIsolationError as exc:
            pytest.fail(str(exc), pytrace=False)

        source_existing = _objects_present(
            source,
            {
                "sku": "to_regclass('public.sku')",
                "stockentry": "to_regclass('public.stockentry')",
                "stockexit": "to_regclass('public.stockexit')",
                "commercial_order": "to_regclass('public.commercial_order')",
                "physical_count": "to_regclass('public.inventory_physical_count')",
                "threshold_evaluation": "to_regclass('public.inventory_threshold_evaluation')",
                "outbox": "to_regclass('public.inventory_event_outbox')",
            },
        )
        reporting_existing = _objects_present(
            reporting,
            {
                "reporting_schema": "to_regnamespace('reporting')",
                "telemetry": "to_regclass('public.telemetry_events')",
            },
        )
        if source_existing or reporting_existing:
            pytest.fail(
                "disposable PostgreSQL databases contain pre-existing TrackFlow objects",
                pytrace=False,
            )

        source_created = True
        SKU.__table__.create(source)
        StockEntry.__table__.create(source)
        CommercialOrder.__table__.create(source)
        StockExit.__table__.create(source)
        InventoryPhysicalCount.__table__.create(source)
        _apply_sql_file(source, "infra/sql/inventory_event_outbox.sql")
        _apply_sql_file(source, "infra/sql/inventory_business_event_contract.sql")
        # Both files are intentionally reapplied to prove the disposable
        # provisioning contract is idempotent before any producer runs.
        _apply_sql_file(source, "infra/sql/inventory_event_outbox.sql")
        _apply_sql_file(source, "infra/sql/inventory_business_event_contract.sql")

        reporting_created = True
        telemetry_metadata.create_all(reporting)
        _apply_sql_file(reporting, "infra/sql/reporting_weekly_warehouse_client_performance.sql")
        _apply_sql_file(reporting, "infra/sql/reporting_pipeline_control.sql")
        yield source, reporting
    finally:
        if reporting_created:
            with reporting.begin() as connection:
                connection.exec_driver_sql(
                    "DROP TABLE IF EXISTS reporting.pipeline_publication_control"
                )
                connection.exec_driver_sql(
                    "DROP TABLE IF EXISTS reporting.pipeline_publication_runs"
                )
                connection.exec_driver_sql(
                    "DROP TABLE IF EXISTS reporting.weekly_warehouse_client_performance"
                )
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS reporting")
            telemetry_metadata.drop_all(reporting)
        if source_created:
            inventory_outbox_metadata.drop_all(source)
            InventoryThresholdEvaluation.__table__.drop(source, checkfirst=True)
            InventoryPhysicalCount.__table__.drop(source, checkfirst=True)
            StockExit.__table__.drop(source, checkfirst=True)
            CommercialOrder.__table__.drop(source, checkfirst=True)
            StockEntry.__table__.drop(source, checkfirst=True)
            SKU.__table__.drop(source, checkfirst=True)
        source.dispose()
        reporting.dispose()


def test_authentic_outbox_dual_pipeline_positive_and_tamper_block(
    disposable_part2_databases,
    monkeypatch: pytest.MonkeyPatch,
    isolated_auth_db,
):
    source, reporting = disposable_part2_databases
    today = datetime.now(timezone.utc).date()
    current_monday = today - timedelta(days=today.weekday())
    week_start = current_monday - timedelta(days=7)
    event_time = datetime.combine(
        week_start + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return event_time if tz is not None else event_time.replace(tzinfo=None)

    monkeypatch.setattr(inventory_service, "datetime", FrozenDateTime)
    client_a = f"part2-client-a-{uuid4()}"
    client_b = f"part2-client-b-{uuid4()}"
    with Session(source) as session:
        sku_la = SKU(
            name="Part 2 LA SKU",
            sku=f"PART2-LA-{uuid4()}",
            client_name="Part 2 client A",
            client_id=client_a,
            category="electronics",
            warehouse="LA",
            minimum_stock_threshold=3,
        )
        sku_zgz = SKU(
            name="Part 2 ZGZ SKU",
            sku=f"PART2-ZGZ-{uuid4()}",
            client_name="Part 2 client B",
            client_id=client_b,
            category="electronics",
            warehouse="ZGZ",
        )
        session.add(sku_la)
        session.add(sku_zgz)
        session.commit()
        session.refresh(sku_la)
        session.refresh(sku_zgz)
        assert sku_la.id is not None and sku_zgz.id is not None
        sku_la_id = int(sku_la.id)
        sku_zgz_id = int(sku_zgz.id)

        la_entry_1 = inventory_service.create_stock_entry(
            session,
            StockEntryCreate(
                sku_id=sku_la_id,
                quantity=10,
                reference="PART2-LA-INBOUND-1",
                warehouse="LA",
            ),
            user_uuid="part2-test-user",
        )
        order_payload = CommercialOrderCreate(
            order_id=uuid4(),
            client_id=client_a,
            warehouse="LA",
            dispatches=[
                {
                    "dispatch_id": uuid4(),
                    "sku_id": sku_la_id,
                    "quantity": 3,
                    "tracking_number": "PART2-LA-1",
                },
                {
                    "dispatch_id": uuid4(),
                    "sku_id": sku_la_id,
                    "quantity": 4,
                    "tracking_number": "PART2-LA-2",
                },
            ],
        )
        order, dispatches = inventory_service.create_commercial_order(
            session,
            order_payload,
            user_uuid="part2-test-user",
        )
        retry_order, retry_dispatches = inventory_service.create_commercial_order(
            session, order_payload, user_uuid="part2-test-user"
        )
        assert retry_order.id == order.id
        assert {item.dispatch_id for item in retry_dispatches} == {
            item.dispatch_id for item in dispatches
        }
        la_entry_2 = inventory_service.create_stock_entry(
            session,
            StockEntryCreate(
                sku_id=sku_la_id,
                quantity=5,
                reference="PART2-LA-INBOUND-2",
                warehouse="LA",
            ),
            user_uuid="part2-test-user",
        )
        append_payload = CommercialOrderDispatchAppend(
            order_id=order_payload.order_id,
            dispatches=[
                {
                    "dispatch_id": uuid4(),
                    "sku_id": sku_la_id,
                    "quantity": 5,
                    "tracking_number": "PART2-LA-3",
                }
            ],
        )
        _order, appended = inventory_service.append_commercial_order_dispatches(
            session, append_payload, user_uuid="part2-test-user"
        )
        _order, append_retry = inventory_service.append_commercial_order_dispatches(
            session, append_payload, user_uuid="part2-test-user"
        )
        assert {item.dispatch_id for item in appended} == {
            item.dispatch_id for item in append_retry
        }
        la_count_payload = InventoryPhysicalCountCreate(
            count_id=uuid4(),
            sku_id=sku_la_id,
            observed_quantity=2,
            warehouse="LA",
        )
        la_count = inventory_service.confirm_inventory_physical_count(
            session,
            la_count_payload,
            user_uuid="part2-test-user",
        )
        la_count_retry = inventory_service.confirm_inventory_physical_count(
            session, la_count_payload, user_uuid="part2-test-user"
        )
        assert la_count_retry.id == la_count.id

        zgz_entry = inventory_service.create_stock_entry(
            session,
            StockEntryCreate(
                sku_id=sku_zgz_id,
                quantity=4,
                reference="PART2-ZGZ-INBOUND",
                warehouse="ZGZ",
            ),
            user_uuid="part2-test-user",
        )
        matching_payload = InventoryPhysicalCountCreate(
            count_id=uuid4(), sku_id=sku_zgz_id, observed_quantity=4, warehouse="ZGZ"
        )
        matching = inventory_service.confirm_inventory_physical_count(
            session, matching_payload, user_uuid="part2-test-user"
        )
        matching_retry = inventory_service.confirm_inventory_physical_count(
            session, matching_payload, user_uuid="part2-test-user"
        )
        assert matching_retry.id == matching.id
        zgz_count_payload = InventoryPhysicalCountCreate(
            count_id=uuid4(), sku_id=sku_zgz_id, observed_quantity=3, warehouse="ZGZ"
        )
        zgz_count = inventory_service.confirm_inventory_physical_count(
            session, zgz_count_payload, user_uuid="part2-test-user"
        )
        zgz_count_retry = inventory_service.confirm_inventory_physical_count(
            session, zgz_count_payload, user_uuid="part2-test-user"
        )
        assert zgz_count_retry.id == zgz_count.id

        assert inventory_service.get_current_stock(session, sku_la_id, "LA") == 3
        assert inventory_service.get_current_stock(session, sku_zgz_id, "ZGZ") == 4
        assert all(
            item.id is not None
            for item in (la_entry_1, la_entry_2, zgz_entry, order, la_count, zgz_count)
        )
        outbox_rows = session.execute(
            select(inventory_event_outbox).order_by(inventory_event_outbox.c.event_type)
        ).mappings().all()
        assert len(outbox_rows) == 8
        by_type = {
            event_type: [row for row in outbox_rows if row["event_type"] == event_type]
            for event_type in {
                "inbound_order_created",
                "outbound_order_created",
                "stock_threshold_triggered",
                "inventory_discrepancy_detected",
            }
        }
        assert {key: len(value) for key, value in by_type.items()} == {
            "inbound_order_created": 3,
            "outbound_order_created": 1,
            "stock_threshold_triggered": 2,
            "inventory_discrepancy_detected": 2,
        }
        assert {
            row["tags"]["movement_id"] for row in by_type["inbound_order_created"]
        } == {str(la_entry_1.id), str(la_entry_2.id), str(zgz_entry.id)}
        assert {row["tags"]["order_id"] for row in by_type["outbound_order_created"]} == {
            str(order_payload.order_id)
        }
        threshold_movement_ids = {
            row["tags"]["movement_id"]
            for row in by_type["stock_threshold_triggered"]
        }
        assert len(threshold_movement_ids) == 2
        assert threshold_movement_ids <= {str(item.id) for item in appended}
        assert {row["tags"]["count_id"] for row in by_type["inventory_discrepancy_detected"]} == {
            str(la_count_payload.count_id), str(zgz_count_payload.count_id)
        }
        assert {
            (row["source_table"], row["event_type"]) for row in outbox_rows
        } == {
            ("stockentry", "inbound_order_created"),
            ("commercial_order", "outbound_order_created"),
            ("stockexit", "stock_threshold_triggered"),
            ("inventory_physical_count", "inventory_discrepancy_detected"),
        }
        tamper_source_pk = int(la_entry_1.id)

        real_enqueue = inventory_service.enqueue_inventory_event

        def fail_after_enqueue(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            raise RuntimeError("injected rollback after outbox insert")

        monkeypatch.setattr(
            inventory_service, "enqueue_inventory_event", fail_after_enqueue
        )
        with pytest.raises(RuntimeError, match="injected rollback"):
            inventory_service.create_stock_entry(
                session,
                StockEntryCreate(
                    sku_id=sku_la_id,
                    quantity=2,
                    reference="PART2-ROLLBACK",
                    warehouse="LA",
                ),
                user_uuid="part2-test-user",
            )
        assert session.scalar(select(func.count()).select_from(StockEntry)) == 3
        assert session.scalar(
            select(func.count()).select_from(inventory_event_outbox)
        ) == 8
        monkeypatch.setattr(inventory_service, "enqueue_inventory_event", real_enqueue)

    delivered = relay_inventory_outbox(source, reporting)
    assert (delivered.delivered, delivered.failed) == (8, 0)
    with reporting.connect() as connection:
        telemetry = connection.execute(
            select(telemetry_events).order_by(telemetry_events.c.event_type)
        ).mappings().all()
    assert len(telemetry) == 8
    assert {row["id"] for row in telemetry} == {
        row["event_id"] for row in outbox_rows
    }
    assert {row["tags"]["client_id"] for row in telemetry} == {client_a, client_b}

    duplicate_id = outbox_rows[0]["event_id"]
    with source.begin() as connection:
        connection.execute(
            update(inventory_event_outbox)
            .where(inventory_event_outbox.c.event_id == duplicate_id)
            .values(published_at=None)
        )
    retried = relay_inventory_outbox(source, reporting)
    assert (retried.delivered, retried.failed) == (1, 0)
    with reporting.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(telemetry_events)
        ) == 8

    extracted = extract_weekly_inputs(
        reporting,
        week_start,
        inventory_bind=source,
    )
    assert extracted.metadata.snapshot_consistent is False
    assert extracted.metadata.event_count == 8
    assert str(order_payload.order_id) in extracted.commercial_order_evidence.records

    transformed = transform_weekly_performance(
        extracted.events,
        commercial_order_evidence=extracted.commercial_order_evidence,
        movement_week_evidence=extracted.movement_week_evidence,
    )
    assert len(transformed.aggregates) == 2
    aggregates = {(item.warehouse, item.client_id): item for item in transformed.aggregates}
    la = aggregates[("los_angeles", client_a)]
    assert (la.inbound_units_count, la.outbound_orders_count) == (15, 1)
    assert (la.stockout_events_count, la.discrepancy_events_count) == (2, 1)
    assert la.discrepancy_rate == Decimal("1")
    zgz = aggregates[("zaragoza", client_b)]
    assert (zgz.inbound_units_count, zgz.outbound_orders_count) == (4, 0)
    assert (zgz.stockout_events_count, zgz.discrepancy_events_count) == (0, 1)
    assert zgz.discrepancy_rate == Decimal("0")

    # Observed zero counters are values, never evidence that every producer or
    # the commercial dispatch-to-order equivalence has been certified.
    pending = transformed.quality.coverage_pending
    assert len(pending) == 3
    assert any("stock_threshold_triggered" in diagnostic for diagnostic in pending)
    assert any(
        "inventory_discrepancy_detected" in diagnostic for diagnostic in pending
    )
    assert any("dispatch" in diagnostic.lower() for diagnostic in pending)

    with reporting.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM reporting.weekly_warehouse_client_performance")
        ) == 0

    fingerprint = "telemetry=part2;movements=part2;stockexit=part2;rules=part2-v1"
    rules_version = "part2-v1"
    monkeypatch.setenv(DEPENDENCY_FINGERPRINT_ENV, fingerprint)
    monkeypatch.setenv(RULES_VERSION_ENV, rules_version)
    monkeypatch.delenv(COVERAGE_EVIDENCE_FILE_ENV, raising=False)
    monkeypatch.setenv("PREFECT_LOGGING_LEVEL", "ERROR")
    monkeypatch.setattr(app.state, "reporting_engine", reporting, raising=False)
    monkeypatch.setattr(app.state, "pipeline_source_engine", source, raising=False)

    # Real OAuth2 login and JWT verification use only the per-test TinyDB.
    # Login telemetry is irrelevant to this contract and must not touch a
    # third database while the two disposable PostgreSQL binds are audited.
    admin_password = "Part2AdminPassword123"
    admin = UserInDB(
        email=f"part2-admin-{uuid4()}@example.com",
        hashed_password=hash_password(admin_password),
        role=Role.admin,
    )
    isolated_auth_db.users.insert(admin.model_dump(mode="json"))
    monkeypatch.setattr(auth_route, "capture_telemetry_events", lambda _events: None)
    monkeypatch.setattr(
        auth_route, "persist_backend_telemetry_best_effort", lambda _events: None
    )
    assert get_current_user not in app.dependency_overrides
    assert reporting_router.require_admin not in app.dependency_overrides
    assert reporting_router.get_scheduler not in app.dependency_overrides

    client = TestClient(app)
    try:
        unauthorized = client.post(
            "/reporting/pipeline-runs", json={"week_start": week_start.isoformat()}
        )
        assert unauthorized.status_code == 401

        login = client.post(
            "/auth/login",
            data={"username": admin.email, "password": admin_password},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        assert get_current_user(token).role is Role.admin
        headers = {"Authorization": f"Bearer {token}"}

        accepted = client.post(
            "/reporting/pipeline-runs",
            json={"week_start": week_start.isoformat()},
            headers=headers,
        )
        assert accepted.status_code == 202
        assert accepted.json()["published"] is False

        latest = client.get("/reporting/pipeline-runs/latest", headers=headers)
        assert latest.status_code == 200
        latest_payload = latest.json()
        assert latest_payload["run_id"] == accepted.json()["run_id"]
        assert latest_payload["status"] in {"succeeded", "succeeded_noop"}
        # The coordinator observed the business COMMIT, but deliberately does
        # not promote that technical observation to business certification.
        assert latest_payload["commit_result"] == "technically_observed"
        assert latest_payload["prefect_flow_run_id"]
        assert latest_payload["prefect_task_run_id"]

        weekly = client.get(
            "/reporting/weekly-warehouse-client-performance",
            params={"week_start": week_start.isoformat()},
            headers=headers,
        )
        assert weekly.status_code == 200
        assert weekly.json() == {
            "week_start": week_start.isoformat(),
            "entries": [
                {
                    "warehouse": "los_angeles",
                    "client_id": client_a,
                    "inbound_units_count": 15,
                    "outbound_orders_count": 1,
                    "stockout_events_count": 2,
                    "discrepancy_events_count": 1,
                    "discrepancy_rate": 1,
                },
                {
                    "warehouse": "zaragoza",
                    "client_id": client_b,
                    "inbound_units_count": 4,
                    "outbound_orders_count": 0,
                    "stockout_events_count": 0,
                    "discrepancy_events_count": 1,
                    "discrepancy_rate": 0,
                },
            ],
        }

        repeated = client.post(
            "/reporting/pipeline-runs",
            json={"week_start": week_start.isoformat()},
            headers=headers,
        )
        assert repeated.status_code == 202
        repeated_latest = client.get(
            "/reporting/pipeline-runs/latest", headers=headers
        )
        assert repeated_latest.status_code == 200
        assert repeated_latest.json()["run_id"] == repeated.json()["run_id"]
        assert repeated_latest.json()["status"] == "succeeded_noop"

        cli_environment = os.environ.copy()
        cli_environment.pop("DATABASE_URL", None)
        cli_environment.pop(COVERAGE_EVIDENCE_FILE_ENV, None)
        cli_environment[PIPELINE_DATABASE_URL_ENV] = reporting.url.render_as_string(
            hide_password=False
        )
        cli_environment[PIPELINE_SOURCE_DATABASE_URL_ENV] = source.url.render_as_string(
            hide_password=False
        )
        cli_environment[DEPENDENCY_FINGERPRINT_ENV] = fingerprint
        cli_environment[RULES_VERSION_ENV] = rules_version
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pipelines.pipeline",
                "--week-start",
                week_start.isoformat(),
            ],
            cwd=_ROOT,
            env=cli_environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        cli_payloads = []
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "status" in value:
                cli_payloads.append(value)
        assert cli_payloads, completed.stdout
        cli_payload = cli_payloads[-1]
        assert cli_payload["status"] in {"succeeded", "succeeded_noop"}
        assert "production_coverage_not_certified" in cli_payload["limitations"]

        with source.begin() as connection:
            connection.execute(
                update(InventoryThresholdEvaluation)
                .where(
                    InventoryThresholdEvaluation.source_table == "stockentry",
                    InventoryThresholdEvaluation.source_pk == tamper_source_pk,
                )
                .values(after_quantity=999)
            )
        blocked = client.post(
            "/reporting/pipeline-runs",
            json={"week_start": week_start.isoformat()},
            headers=headers,
        )
        assert blocked.status_code == 202
        blocked_latest = client.get(
            "/reporting/pipeline-runs/latest", headers=headers
        )
        assert blocked_latest.status_code == 200
        assert blocked_latest.json()["run_id"] == blocked.json()["run_id"]
        assert blocked_latest.json()["status"] == "blocked_quality"
        assert blocked_latest.json()["block_reason"] == "coverage_insufficient"
        assert blocked_latest.json()["commit_result"] == "not_attempted"
    finally:
        client.close()

    with reporting.connect() as connection:
        published = connection.execute(
            text(
                "SELECT count(*) FROM reporting.weekly_warehouse_client_performance"
            )
        ).scalar_one()
    assert published == 2


def test_concurrent_stock_exits_serialize_one_threshold_crossing(
    disposable_part2_databases,
):
    source, _reporting = disposable_part2_databases
    client_id = f"part2-concurrency-{uuid4()}"
    with Session(source) as session:
        sku = SKU(
            name="Concurrent threshold SKU",
            sku=f"PART2-CONCURRENT-{uuid4()}",
            client_name="Concurrent client",
            client_id=client_id,
            category="electronics",
            warehouse="LA",
            minimum_stock_threshold=5,
        )
        session.add(sku)
        session.commit()
        session.refresh(sku)
        assert sku.id is not None
        sku_id = int(sku.id)
        inventory_service.create_stock_entry(
            session,
            StockEntryCreate(
                sku_id=sku_id,
                quantity=10,
                reference="PART2-CONCURRENT-IN",
                warehouse="LA",
            ),
            user_uuid="part2-concurrent-user",
        )

    barrier = Barrier(2)

    def create_exit(index: int) -> int:
        with Session(source) as session:
            barrier.wait(timeout=10)
            record = inventory_service.create_stock_exit(
                session,
                StockExitCreate(
                    sku_id=sku_id,
                    quantity=3,
                    exit_type="dispatch",
                    tracking_number=f"PART2-CONCURRENT-{index}",
                    warehouse="LA",
                ),
                user_uuid="part2-concurrent-user",
            )
            assert record.id is not None
            return int(record.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        exit_ids = set(executor.map(create_exit, (1, 2)))

    with Session(source) as session:
        assert inventory_service.get_current_stock(session, sku_id, "LA") == 4
        exits = session.exec(select(StockExit).where(StockExit.id.in_(exit_ids))).all()
        assert len(exits) == 2
        threshold_rows = [
            dict(row)
            for row in session.execute(
                select(inventory_event_outbox).where(
                    inventory_event_outbox.c.event_type == "stock_threshold_triggered"
                )
            ).mappings()
            if row["tags"].get("product_id") == str(sku_id)
        ]
        assert len(threshold_rows) == 1
        assert threshold_rows[0]["tags"]["movement_id"] in {
            str(value) for value in exit_ids
        }
        assert threshold_rows[0]["tags"]["quantity"] == 4


def test_part2_postgres_urls_must_be_distinct(monkeypatch: pytest.MonkeyPatch):
    url = "postgresql://test.invalid/disposable"
    monkeypatch.setenv(SOURCE_URL_ENV, url)
    monkeypatch.setenv(REPORTING_URL_ENV, url)
    for name in _PROTECTED_URL_ENVS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(pytest.fail.Exception, match="must be distinct"):
        _validated_urls()


def test_part2_postgres_urls_must_use_postgresql(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SOURCE_URL_ENV, "sqlite:///not-authorized.db")
    monkeypatch.setenv(REPORTING_URL_ENV, "postgresql://test.invalid/disposable")
    for name in _PROTECTED_URL_ENVS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(pytest.fail.Exception, match="must identify PostgreSQL"):
        _validated_urls()
