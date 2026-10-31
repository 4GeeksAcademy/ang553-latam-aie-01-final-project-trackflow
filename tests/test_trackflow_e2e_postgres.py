"""Controlled technical E2E; synthetic fixtures are not business certification."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from hashlib import sha256
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pipelines.pipeline import PipelineRuntime, main
from pipelines.extraction import extract_weekly_inputs
from pipelines.technical_coverage import (
    apply_verified_technical_coverage,
    authorize_controlled_test_database,
    verify_controlled_test_coverage,
)
from pipelines.weekly_performance import transform_weekly_performance
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.reporting.router import get_reporting_engine, router
from tests.test_pipeline_postgres_integration import (
    FINGERPRINT,
    RULES_VERSION,
    _coverage,
    _isolated_engine,
)

WEEK = date(2002, 1, 7)


def test_cli_prefect_postgres_reporting_positive_and_negative_e2e(capsys, tmp_path):
    engine = _isolated_engine()
    namespace = uuid4().hex
    client_id = f"f6-synthetic-{namespace}"
    event_ids = [uuid4() for _ in range(4)]
    stockexit_id = 960001
    commercial_order_id = 950001
    order_id = uuid4()
    count_id = uuid4()
    sku_id = 970001
    owned_run_ids = []
    control_created = False

    with engine.connect() as connection:
        source_objects = connection.execute(
            text("SELECT to_regclass('stockexit') AS stockexit, to_regclass('sku') AS sku, "
                 "to_regclass('commercial_order') AS commercial_order")
        ).mappings().one()
        preexisting = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM reporting.pipeline_publication_runs WHERE target_week_start=:week) AS runs, "
                "(SELECT count(*) FROM reporting.pipeline_publication_control WHERE week_start=:week) AS control, "
                "(SELECT count(*) FROM reporting.weekly_warehouse_client_performance WHERE week_start=:week) AS kpis"
            ),
            {"week": WEEK},
        ).mappings().one()
    assert source_objects == {"stockexit": None, "sku": None, "commercial_order": None}
    assert preexisting == {"runs": 0, "control": 0, "kpis": 0}

    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE sku (id integer PRIMARY KEY, client_id text NOT NULL)"))
            connection.execute(
                text(
                    "CREATE TABLE commercial_order (id integer PRIMARY KEY, order_id uuid NOT NULL UNIQUE, "
                    "client_id text NOT NULL, warehouse text NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE stockexit (id integer PRIMARY KEY, exit_type text NOT NULL, "
                    "warehouse text NOT NULL, quantity integer NOT NULL, sku_id integer NOT NULL, "
                    "commercial_order_id integer REFERENCES commercial_order(id))"
                )
            )
            connection.execute(
                text("INSERT INTO sku (id, client_id) VALUES (:id, :client_id)"),
                {"id": sku_id, "client_id": client_id},
            )
            connection.execute(
                text(
                    "INSERT INTO commercial_order (id, order_id, client_id, warehouse) "
                    "VALUES (:id, :order_id, :client_id, 'los_angeles')"
                ),
                {"id": commercial_order_id, "order_id": order_id, "client_id": client_id},
            )
            connection.execute(
                text(
                    "INSERT INTO stockexit (id, exit_type, warehouse, quantity, sku_id, commercial_order_id) "
                    "VALUES (:id, 'dispatch', 'los_angeles', 2, :sku_id, :order_pk)"
                ),
                {"id": stockexit_id, "sku_id": sku_id, "order_pk": commercial_order_id},
            )
            events = (
                (event_ids[0], "inbound_order_created", {"warehouse": "los_angeles", "client_id": client_id, "product_id": str(sku_id), "movement_id": f"f6-in-{namespace}", "quantity": 5}),
                (event_ids[1], "outbound_order_created", {"warehouse": "los_angeles", "client_id": client_id, "order_id": str(order_id)}),
                (event_ids[2], "stock_threshold_triggered", {"warehouse": "los_angeles", "client_id": client_id, "product_id": str(sku_id), "quantity": 0, "movement_id": str(stockexit_id)}),
                (event_ids[3], "inventory_discrepancy_detected", {"warehouse": "los_angeles", "client_id": client_id, "product_id": str(sku_id), "quantity": 1, "count_id": str(count_id)}),
            )
            for offset, (event_id, event_type, tags) in enumerate(events):
                connection.execute(
                    text(
                        "INSERT INTO telemetry_events (id, timestamp, service, event_type, tags) "
                        "VALUES (:id, :timestamp, 'api', :event_type, CAST(:tags AS jsonb))"
                    ),
                    {
                        "id": event_id,
                        "timestamp": datetime(2002, 1, 8 + offset, 12, tzinfo=timezone.utc),
                        "event_type": event_type,
                        "tags": json.dumps(tags),
                    },
                )

        extracted = extract_weekly_inputs(engine, WEEK)
        transformed = transform_weekly_performance(
            extracted.events,
            commercial_order_evidence=extracted.commercial_order_evidence,
            movement_week_evidence=extracted.movement_week_evidence,
        )
        data_hash = sha256(repr(tuple(sorted(str(item) for item in event_ids))).encode()).hexdigest()
        fingerprint = (
            f"telemetry={data_hash};movements={data_hash};"
            f"stockexit={stockexit_id};rules={RULES_VERSION}"
        )
        database_capability = authorize_controlled_test_database(
            engine, container_name="trackflow-pg-f2-933cf5b"
        )
        copied_capability = replace(database_capability)
        with __import__("pytest").raises(ValueError, match="capability"):
            verify_controlled_test_coverage(
                engine,
                extracted,
                transformed,
                week_start=WEEK,
                dependency_fingerprint=fingerprint,
                rules_version=RULES_VERSION,
                database_capability=copied_capability,
            )
        technical = verify_controlled_test_coverage(
            engine,
            extracted,
            transformed,
            week_start=WEEK,
            dependency_fingerprint=fingerprint,
            rules_version=RULES_VERSION,
            database_capability=database_capability,
        )
        for mutation in (
            {"identity": "0" * 64},
            {"outbound_order_client_pairs": (("forged-order", client_id),)},
            {"content_fingerprint": "0" * 64},
            {"provenance": "claimed_trusted_origin"},
            {"scope": "production"},
        ):
            with __import__("pytest").raises(ValueError, match="attestation"):
                apply_verified_technical_coverage(
                    extracted,
                    transformed,
                    replace(technical, **mutation),
                    week_start=WEEK,
                    dependency_fingerprint=fingerprint,
                    rules_version=RULES_VERSION,
                    engine=engine,
                )
        extra_diagnostic = "Coverage remains pending: independent producer audit absent."
        transformed_with_extra = replace(
            transformed,
            quality=replace(
                transformed.quality,
                coverage_pending=(*transformed.quality.coverage_pending, extra_diagnostic),
            ),
        )
        extra_evidence = verify_controlled_test_coverage(
            engine,
            extracted,
            transformed_with_extra,
            week_start=WEEK,
            dependency_fingerprint=fingerprint,
            rules_version=RULES_VERSION,
            database_capability=database_capability,
        )
        extra_applied = apply_verified_technical_coverage(
            extracted,
            transformed_with_extra,
            extra_evidence,
            week_start=WEEK,
            dependency_fingerprint=fingerprint,
            rules_version=RULES_VERSION,
            engine=engine,
        )
        assert extra_applied.quality.coverage_pending == (extra_diagnostic,)
        tampered_event = dict(extracted.events[0])
        tampered_event["tags"] = {**tampered_event["tags"], "quantity": 999}
        tampered_extracted = replace(
            extracted,
            events=(tampered_event, *extracted.events[1:]),
        )
        with __import__("pytest").raises(ValueError, match="attestation"):
            apply_verified_technical_coverage(
                tampered_extracted,
                transformed,
                technical,
                week_start=WEEK,
                dependency_fingerprint=fingerprint,
                rules_version=RULES_VERSION,
                engine=engine,
            )
        coverage_file = tmp_path / "coverage.json"
        coverage_file.write_text(json.dumps({
            "confirmed_event_types": ["stock_threshold_triggered", "inventory_discrepancy_detected"],
            "dispatch_order_ids": [[str(stockexit_id), f"order-{namespace}"]],
            "lookup_complete": True,
            "source": "synthetic_fixture",
        }), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({
            "TRACKFLOW_PIPELINE_DATABASE_URL": os.environ["TRACKFLOW_PUBLICATION_TEST_DATABASE_URL"],
            "TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT": fingerprint,
            "TRACKFLOW_PIPELINE_RULES_VERSION": RULES_VERSION,
            "TRACKFLOW_COVERAGE_EVIDENCE_FILE": str(coverage_file),
            "PREFECT_LOGGING_LEVEL": "ERROR",
        })
        negative = subprocess.run(
            ["uv", "run", "python", "-m", "pipelines.pipeline", "--week-start", WEEK.isoformat()],
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert negative.returncode == 2
        assert json.loads(negative.stdout)["status"] == "blocked_quality"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM reporting.pipeline_publication_control WHERE week_start=:week"), {"week": WEEK}).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM reporting.weekly_warehouse_client_performance WHERE week_start=:week"), {"week": WEEK}).scalar_one() == 0
            owned_run_ids.append(connection.execute(
                text("SELECT run_id FROM reporting.pipeline_publication_runs WHERE target_week_start=:week AND status='blocked_quality'"),
                {"week": WEEK},
            ).scalar_one())
        invalid_runtime = PipelineRuntime(
            engine=engine,
            coverage=_coverage(),
            dependency_fingerprint=fingerprint,
            rules_version=RULES_VERSION,
            trigger_type="f6-invalid-attestation",
            technical_coverage=replace(technical, content_fingerprint="0" * 64),
            clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        )
        invalid_code = main(
            ["--week-start", WEEK.isoformat()],
            now=datetime(2026, 10, 29, tzinfo=timezone.utc),
            runtime_factory=lambda: invalid_runtime,
        )
        invalid = json.loads(capsys.readouterr().out)
        owned_run_ids.append(UUID(invalid["run_id"]))
        assert invalid_code == 2
        assert invalid["status"] == "blocked_quality"
        with engine.connect() as connection:
            invalid_row = connection.execute(
                text(
                    "SELECT status, block_reason, commit_result FROM reporting.pipeline_publication_runs "
                    "WHERE run_id=:run_id"
                ),
                {"run_id": invalid["run_id"]},
            ).mappings().one()
            assert invalid_row == {
                "status": "blocked_quality",
                "block_reason": "technical_coverage_invalid",
                "commit_result": "not_attempted",
            }
            assert connection.execute(text("SELECT count(*) FROM reporting.pipeline_publication_control WHERE week_start=:week"), {"week": WEEK}).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM reporting.weekly_warehouse_client_performance WHERE week_start=:week"), {"week": WEEK}).scalar_one() == 0
        runtime = PipelineRuntime(
            engine=engine,
            coverage=_coverage(),
            dependency_fingerprint=fingerprint,
            rules_version=RULES_VERSION,
            trigger_type="f6-synthetic-e2e",
            technical_coverage=technical,
            clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        )
        first_code = main(
            ["--week-start", WEEK.isoformat()],
            now=datetime(2026, 10, 29, tzinfo=timezone.utc),
            runtime_factory=lambda: runtime,
        )
        first = json.loads(capsys.readouterr().out)
        owned_run_ids.append(UUID(first["run_id"]))
        with engine.connect() as connection:
            initial_row = connection.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start=:week_start AND client_id=:client_id"
                ),
                {"week_start": WEEK, "client_id": client_id},
            ).mappings().one()
            control_created = connection.execute(
                text("SELECT count(*) FROM reporting.pipeline_publication_control WHERE week_start=:week"),
                {"week": WEEK},
            ).scalar_one() == 1
        second_code = main(
            ["--week-start", WEEK.isoformat()],
            now=datetime(2026, 10, 29, tzinfo=timezone.utc),
            runtime_factory=lambda: runtime,
        )
        second = json.loads(capsys.readouterr().out)
        owned_run_ids.append(UUID(second["run_id"]))
        assert first_code == second_code == 0
        assert first["status"] == "succeeded"
        assert first["counts"] == {"inserted": 1, "updated": 0, "unchanged": 0}
        assert second["status"] == "succeeded_noop"
        assert second["counts"] == {"inserted": 0, "updated": 0, "unchanged": 1}
        assert first["limitations"] == second["limitations"] == ["production_coverage_not_certified"]

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_user] = lambda: UserInDB(
            email="manager@example.com", hashed_password="hash", role=Role.manager
        )
        app.dependency_overrides[get_reporting_engine] = lambda: engine
        response = TestClient(app).get(
            f"/reporting/weekly-warehouse-client-performance?week_start={WEEK.isoformat()}"
        )
        assert response.status_code == 200
        assert response.json()["entries"][0] == {
            "warehouse": "los_angeles",
            "client_id": client_id,
            "inbound_units_count": 5,
            "outbound_orders_count": 1,
            "stockout_events_count": 1,
            "discrepancy_events_count": 1,
            "discrepancy_rate": 1,
        }
        with engine.connect() as connection:
            runs = connection.execute(
                text(
                    "SELECT status, records_processed, aggregates_calculated, commit_result, "
                    "prefect_flow_run_id, prefect_task_run_id FROM reporting.pipeline_publication_runs "
                    "WHERE target_week_start = :week_start ORDER BY started_at"
                ),
                {"week_start": WEEK},
            ).mappings().all()
            final_row = connection.execute(
                text(
                    "SELECT id, computed_at FROM reporting.weekly_warehouse_client_performance "
                    "WHERE week_start = :week_start AND client_id = :client_id"
                ),
                {"week_start": WEEK, "client_id": client_id},
            ).mappings().one()
            control = connection.execute(
                text("SELECT count(*) FROM reporting.pipeline_publication_control WHERE week_start = :week_start"),
                {"week_start": WEEK},
            ).scalar_one()
        assert sorted(row["status"] for row in runs) == [
            "blocked_quality", "blocked_quality", "succeeded", "succeeded_noop"
        ]
        assert all(row["records_processed"] == 4 for row in runs)
        assert all(row["aggregates_calculated"] == 1 for row in runs)
        assert {
            row["status"]: row["commit_result"] for row in runs
        } == {
            "blocked_quality": "not_attempted",
            "succeeded": "technically_observed",
            "succeeded_noop": "technically_observed",
        }
        assert all(row["prefect_flow_run_id"] and row["prefect_task_run_id"] for row in runs)
        assert final_row == initial_row
        assert control == 1
        assert technical.production_certified is False
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM reporting.weekly_warehouse_client_performance WHERE week_start = :week_start AND client_id = :client_id"),
                {"week_start": WEEK, "client_id": client_id},
            )
            if control_created:
                connection.execute(text("DELETE FROM reporting.pipeline_publication_control WHERE week_start = :week_start"), {"week_start": WEEK})
            if owned_run_ids:
                connection.execute(text("DELETE FROM reporting.pipeline_publication_runs WHERE run_id = ANY(:run_ids)"), {"run_ids": owned_run_ids})
            connection.execute(text("DELETE FROM telemetry_events WHERE id = ANY(:ids)"), {"ids": event_ids})
            connection.execute(text("DROP TABLE IF EXISTS stockexit"))
            connection.execute(text("DROP TABLE IF EXISTS commercial_order"))
            connection.execute(text("DROP TABLE IF EXISTS sku"))
        engine.dispose()
