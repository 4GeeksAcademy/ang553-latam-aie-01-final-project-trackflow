"""Tests for the transitional canonical inventory client identity foundation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlmodel import create_engine

from services.api.inventory_clients import CANONICAL_CLIENTS, CLIENT_IDS_BY_NAME


_MIGRATION_PATH = Path(__file__).parents[1] / "scripts/migrate_inventory_client_ids.py"
_SPEC = importlib.util.spec_from_file_location("migrate_inventory_client_ids", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def test_canonical_ids_are_fixed_opaque_uuid_v4_values() -> None:
    assert len(CANONICAL_CLIENTS) == 4
    assert all(client.client_id not in client.client_name for client in CANONICAL_CLIENTS)
    assert all(client.client_id.split("-")[2].startswith("4") for client in CANONICAL_CLIENTS)


def test_migration_adds_backfills_and_is_idempotent() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sku (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
                "sku VARCHAR NOT NULL, client_name VARCHAR NOT NULL, "
                "category VARCHAR NOT NULL, warehouse VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sku (id, name, sku, client_name, category, warehouse) "
                "VALUES (1, 'Known', 'KNOWN-1', 'PureStep Footwear', 'fashion', 'LA'), "
                "(2, 'Unknown', 'UNKNOWN-1', 'Unmapped Client', 'fashion', 'LA')"
            )
        )

    report = _MIGRATION.migrate(engine)
    assert report.unknown_clients == [(2, "Unmapped Client")]
    assert report.mismatched_client_ids == []

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, client_id FROM sku ORDER BY id")
        ).all()
    assert rows == [
        (1, CLIENT_IDS_BY_NAME["PureStep Footwear"]),
        (2, None),
    ]

    rerun = _MIGRATION.migrate(engine)
    assert rerun.unknown_clients == report.unknown_clients
    assert rerun.mismatched_client_ids == []


def test_null_known_client_is_backfilled_and_canonical_id_is_noop() -> None:
    engine = create_engine("sqlite://")
    SQL = "CREATE TABLE sku (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, sku VARCHAR NOT NULL, client_name VARCHAR NOT NULL, category VARCHAR NOT NULL, warehouse VARCHAR NOT NULL, client_id VARCHAR NULL)"
    with engine.begin() as connection:
        connection.execute(text(SQL))
        connection.execute(text("INSERT INTO sku VALUES (1, 'Known', 'A', 'PureStep Footwear', 'fashion', 'LA', NULL)"))
        connection.execute(text("INSERT INTO sku VALUES (2, 'Known', 'B', 'UrbanThread', 'fashion', 'LA', :client_id)"), {"client_id": CLIENT_IDS_BY_NAME["UrbanThread"]})

    report = _MIGRATION.backfill_client_ids(engine)
    assert report.unknown_clients == []
    assert report.mismatched_client_ids == []
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, client_id FROM sku ORDER BY id")).all()
    assert rows == [(1, CLIENT_IDS_BY_NAME["PureStep Footwear"]), (2, CLIENT_IDS_BY_NAME["UrbanThread"])]


def test_conflicting_id_is_preserved_and_reported() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sku (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, sku VARCHAR NOT NULL, client_name VARCHAR NOT NULL, category VARCHAR NOT NULL, warehouse VARCHAR NOT NULL, client_id VARCHAR NULL)"))
        connection.execute(text("INSERT INTO sku VALUES (1, 'Conflict', 'C', 'PureStep Footwear', 'fashion', 'LA', '11111111-1111-4111-8111-111111111111')"))

    report = _MIGRATION.backfill_client_ids(engine)
    assert report.unknown_clients == []
    assert len(report.mismatched_client_ids) == 1
    mismatch = report.mismatched_client_ids[0]
    assert mismatch.existing_client_id == "11111111-1111-4111-8111-111111111111"
    assert mismatch.expected_client_id == CLIENT_IDS_BY_NAME["PureStep Footwear"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT client_id FROM sku WHERE id=1")).scalar_one() == mismatch.existing_client_id


def test_unknown_client_preserves_null_or_existing_id() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sku (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, sku VARCHAR NOT NULL, client_name VARCHAR NOT NULL, category VARCHAR NOT NULL, warehouse VARCHAR NOT NULL, client_id VARCHAR NULL)"))
        connection.execute(text("INSERT INTO sku VALUES (1, 'Unknown', 'U1', 'Unmapped', 'fashion', 'LA', NULL)"))
        connection.execute(text("INSERT INTO sku VALUES (2, 'Unknown', 'U2', 'Unmapped', 'fashion', 'LA', '22222222-2222-4222-8222-222222222222')"))

    report = _MIGRATION.backfill_client_ids(engine)
    assert report.unknown_clients == [(1, "Unmapped"), (2, "Unmapped")]
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, client_id FROM sku ORDER BY id")).all()
    assert rows == [(1, None), (2, "22222222-2222-4222-8222-222222222222")]


def test_exit_code_is_nonzero_for_unresolved_rows() -> None:
    clean = _MIGRATION.MigrationReport()
    unresolved = _MIGRATION.MigrationReport(unknown_clients=[(1, "Unknown")])
    mismatch = _MIGRATION.MigrationReport(
        mismatched_client_ids=[_MIGRATION.ClientIdMismatch(1, "Known", "old", "new")]
    )
    assert _MIGRATION.migration_exit_code(clean) == 0
    assert _MIGRATION.migration_exit_code(unresolved) == 2
    assert _MIGRATION.migration_exit_code(mismatch) == 2
