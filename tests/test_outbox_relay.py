"""Unit contracts for the crash-safe inventory outbox relay."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from pipelines.outbox_relay import relay_inventory_outbox


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


class Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return deepcopy(self.row)


class SourceConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement):
        if statement.is_select:
            self.engine.select_sql.append(
                str(statement.compile(dialect=postgresql.dialect()))
            )
            pending = next(
                (row for row in self.engine.rows if row["published_at"] is None),
                None,
            )
            return Result(pending)

        keys = {
            getattr(column, "key", str(column)) for column in statement._values
        }
        row = next(row for row in self.engine.rows if row["published_at"] is None)
        row["attempt_count"] += 1
        row["last_attempt_at"] = NOW
        if "published_at" in keys:
            row["published_at"] = NOW
            row["last_error"] = None
        else:
            compiled = statement.compile(dialect=postgresql.dialect())
            row["last_error"] = next(
                value for key, value in compiled.params.items() if "last_error" in key
            )
        return Result(None)


class SourceBegin:
    def __init__(self, engine):
        self.engine = engine
        self.snapshot = None

    def __enter__(self):
        self.snapshot = deepcopy(self.engine.rows)
        return SourceConnection(self.engine)

    def __exit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self.engine.rows = self.snapshot
        return False


class SourceEngine:
    def __init__(self, rows):
        self.rows = rows
        self.select_sql = []

    def begin(self):
        return SourceBegin(self)


class DestinationConnection:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if self.engine.error is not None:
            raise self.engine.error
        params = statement.compile(dialect=postgresql.dialect()).params
        if statement.is_insert:
            self.engine.insert_sql.append(sql)
            event_id = params["id"]
            if event_id in self.engine.rows:
                return Result(None)
            row = {
                key: params[key]
                for key in (
                    "id",
                    "timestamp",
                    "service",
                    "event_type",
                    "level",
                    "value",
                    "message",
                    "tags",
                )
            }
            self.engine.rows[event_id] = row
            return Result(row)
        return Result(self.engine.rows.get(params["id_1"]))


class DestinationBegin:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return DestinationConnection(self.engine)

    def __exit__(self, *_args):
        return False


class DestinationEngine:
    def __init__(self, error=None, rows=None):
        self.rows = {} if rows is None else rows
        self.insert_sql = []
        self.error = error

    def begin(self):
        return DestinationBegin(self)


def pending_row(event_type="inbound_order_created"):
    source_table, tags = {
        "inbound_order_created": (
            "stockentry",
            {"movement_id": "9", "client_id": "client-1", "quantity": 2},
        ),
        "outbound_order_created": (
            "commercial_order",
            {"order_id": str(uuid4()), "client_id": "client-1"},
        ),
        "stock_threshold_triggered": (
            "stockexit",
            {"movement_id": "9", "client_id": "client-1", "quantity": 0},
        ),
        "inventory_discrepancy_detected": (
            "inventory_physical_count",
            {"count_id": str(uuid4()), "client_id": "client-1", "quantity": 1},
        ),
    }[event_type]
    return {
        "event_id": uuid4(),
        "occurred_at": NOW,
        "source_table": source_table,
        "source_pk": 9,
        "event_type": event_type,
        "tags": tags,
        "published_at": None,
        "attempt_count": 0,
        "last_attempt_at": None,
        "last_error": None,
        "created_at": NOW,
    }


def test_relay_delivers_with_locking_stable_uuid_and_conflict_safety():
    row = pending_row()
    source = SourceEngine([row])
    destination = DestinationEngine()

    result = relay_inventory_outbox(source, destination, clock=lambda: NOW)

    assert (result.delivered, result.failed) == (1, 0)
    assert set(destination.rows) == {row["event_id"]}
    assert source.rows[0]["published_at"] == NOW
    assert source.rows[0]["attempt_count"] == 1
    assert "FOR UPDATE SKIP LOCKED" in source.select_sql[0]
    assert "ON CONFLICT (id) DO NOTHING" in destination.insert_sql[0]


def test_relay_transports_all_four_event_contracts_without_tag_or_uuid_loss():
    rows = [
        pending_row(event_type)
        for event_type in (
            "inbound_order_created",
            "outbound_order_created",
            "stock_threshold_triggered",
            "inventory_discrepancy_detected",
        )
    ]
    source = SourceEngine(rows)
    destination = DestinationEngine()

    result = relay_inventory_outbox(source, destination, limit=4, clock=lambda: NOW)

    assert result == type(result)(4, 0, ())
    assert set(destination.rows) == {row["event_id"] for row in rows}
    for row in rows:
        delivered = destination.rows[row["event_id"]]
        assert delivered["id"] == row["event_id"]
        assert delivered["event_type"] == row["event_type"]
        assert delivered["tags"] == row["tags"]


def test_destination_failure_leaves_pending_and_records_only_safe_code():
    source = SourceEngine([pending_row()])
    destination = DestinationEngine(RuntimeError("postgresql://user:secret@host/db"))

    result = relay_inventory_outbox(source, destination, clock=lambda: NOW)

    assert result.error_codes == ("destination_runtimeerror",)
    assert source.rows[0]["published_at"] is None
    assert source.rows[0]["attempt_count"] == 1
    assert source.rows[0]["last_error"] == "destination_runtimeerror"
    assert "secret" not in source.rows[0]["last_error"]


def test_existing_uuid_with_different_payload_remains_pending():
    row = pending_row()
    destination = DestinationEngine(
        rows={
            row["event_id"]: {
                "id": row["event_id"],
                "timestamp": NOW,
                "service": "api",
                "event_type": "outbound_order_created",
                "level": "info",
                "value": None,
                "message": None,
                "tags": {"movement_id": "different", "client_id": "secret-client"},
            }
        }
    )
    source = SourceEngine([row])

    result = relay_inventory_outbox(source, destination, clock=lambda: NOW)

    assert result == type(result)(0, 1, ("destination_event_conflict",))
    assert source.rows[0]["published_at"] is None
    assert source.rows[0]["attempt_count"] == 1
    assert source.rows[0]["last_error"] == "destination_event_conflict"
    assert "secret-client" not in source.rows[0]["last_error"]


def test_crash_after_destination_commit_retries_without_duplicate():
    source = SourceEngine([pending_row()])
    destination = DestinationEngine()

    with pytest.raises(RuntimeError, match="simulated crash"):
        relay_inventory_outbox(
            source,
            destination,
            clock=lambda: NOW,
            after_destination_commit=lambda _row: (_ for _ in ()).throw(
                RuntimeError("simulated crash")
            ),
        )

    assert source.rows[0]["published_at"] is None
    assert source.rows[0]["attempt_count"] == 0
    assert len(destination.rows) == 1

    result = relay_inventory_outbox(source, destination, clock=lambda: NOW)

    assert result.delivered == 1
    assert len(destination.rows) == 1
    assert source.rows[0]["published_at"] == NOW
