"""Synthetic unit tests for read-only weekly extraction contracts.

These tests exercise statement construction and extractor orchestration with a
DB-API-like fake. They do not verify PostgreSQL execution semantics.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from pipelines.extraction import (
    WeeklyExtractionError,
    extract_weekly_inputs,
)
from pipelines.weekly_performance import (
    INBOUND_ORDER_CREATED,
    INVENTORY_DISCREPANCY_DETECTED,
    OFFICIAL_EVENT_TYPES,
    OUTBOUND_ORDER_CREATED,
    STOCK_THRESHOLD_TRIGGERED,
    MovementWeekEvidence,
    StockExitRecord,
    transform_weekly_performance,
)

WEEK = date(2026, 10, 19)
START = datetime(2026, 10, 19, tzinfo=timezone.utc)
NEXT = datetime(2026, 10, 26, tzinfo=timezone.utc)
CLIENT = "historical-client-9"


def telemetry_row(
    suffix: int,
    event_type: str,
    timestamp: datetime,
    movement_id: str | None = None,
    *,
    quantity: int = 3,
):
    tags = {
        "warehouse": "los_angeles",
        "client_id": CLIENT,
        "product_id": "42",
        "quantity": quantity,
    }
    if movement_id is not None:
        tags["movement_id"] = movement_id
    return {
        "id": UUID(int=suffix),
        "timestamp": timestamp,
        "service": "api",
        "event_type": event_type,
        "tags": tags,
    }


class FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class FakeTransaction(AbstractContextManager):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeConnection(AbstractContextManager):
    dialect = type("Dialect", (), {"name": "postgresql"})()

    def __init__(self, *, events=(), stockexits=(), query_error=None):
        self.events = list(events)
        self.stockexits = list(stockexits)
        self.query_error = query_error
        self.executed = []
        self.transaction_sql = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return FakeTransaction()

    def exec_driver_sql(self, statement):
        self.transaction_sql.append(statement)

    def execute(self, statement, parameters):
        sql = str(statement)
        self.executed.append((sql, dict(parameters)))
        if self.query_error is not None:
            raise self.query_error

        if "FROM stockexit AS e" in sql:
            allowed_ids = set(parameters["movement_ids"])
            rows = [row for row in self.stockexits if row["movement_id"] in allowed_ids]
        elif "tags->>'movement_id'" in sql:
            keys = {
                (parameters[f"event_type_{i}"], parameters[f"movement_id_{i}"])
                for i in range(len(parameters) // 2)
            }
            rows = [
                row
                for row in self.events
                if (row["event_type"], row["tags"].get("movement_id")) in keys
            ]
        else:
            event_types = set(parameters["event_types"])
            rows = [
                row
                for row in self.events
                if row["event_type"] in event_types
                and parameters["week_start"] <= row["timestamp"] < parameters["next_week_start"]
            ]
        return FakeMappings(rows)


class FakeEngine:
    def __init__(self, connection=None, connect_error=None):
        self.connection = connection or FakeConnection()
        self.connect_error = connect_error

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


def test_query_selects_only_official_types_with_bound_week_parameters():
    rows = [
        telemetry_row(i, event_type, START + timedelta(hours=i))
        for i, event_type in enumerate(sorted(OFFICIAL_EVENT_TYPES), 1)
    ]
    rows.append(telemetry_row(20, "inventory_stock_insufficient", START))
    connection = FakeConnection(events=rows)

    result = extract_weekly_inputs(FakeEngine(connection), WEEK)

    assert len(result.events) == 4
    sql, parameters = connection.executed[0]
    assert "event_type IN" in sql
    assert parameters == {
        "event_types": sorted(OFFICIAL_EVENT_TYPES),
        "week_start": START,
        "next_week_start": NEXT,
    }
    assert "inventory_stock_insufficient" not in sql


def test_week_boundaries_are_half_open_start_included_next_monday_excluded():
    rows = [
        telemetry_row(1, STOCK_THRESHOLD_TRIGGERED, START),
        telemetry_row(2, STOCK_THRESHOLD_TRIGGERED, NEXT),
        telemetry_row(3, STOCK_THRESHOLD_TRIGGERED, START - timedelta(microseconds=1)),
    ]
    result = extract_weekly_inputs(FakeEngine(FakeConnection(events=rows)), WEEK)
    assert [row["id"] for row in result.events] == [str(UUID(int=1))]
    assert result.metadata.week_start == WEEK
    assert result.metadata.next_week_start == date(2026, 10, 26)


def test_empty_extraction_returns_empty_inputs_and_complete_evidence():
    result = extract_weekly_inputs(FakeEngine(), WEEK)
    assert result.events == ()
    assert result.stockexit_evidence.records == {}
    assert result.stockexit_evidence.lookup_complete is True
    assert result.movement_week_evidence == {}
    assert result.metadata.event_count == 0
    assert result.metadata.snapshot_consistent is True


def test_connection_error_is_identifiable_and_not_a_valid_empty_result():
    failure = OperationalError("connect", {}, RuntimeError("offline"))
    with pytest.raises(WeeklyExtractionError, match="connect") as caught:
        extract_weekly_inputs(FakeEngine(connect_error=failure), WEEK)
    assert caught.value.__cause__ is failure


def test_query_error_propagates_as_identifiable_extraction_error():
    failure = SQLAlchemyError("query failed")
    with pytest.raises(WeeklyExtractionError, match="weekly-events") as caught:
        extract_weekly_inputs(
            FakeEngine(FakeConnection(query_error=failure)), WEEK
        )
    assert caught.value.__cause__ is failure


def test_programming_error_is_not_mislabeled_as_database_failure():
    failure = TypeError("unexpected row contract")
    with pytest.raises(TypeError, match="unexpected row contract") as caught:
        extract_weekly_inputs(
            FakeEngine(FakeConnection(query_error=failure)), WEEK
        )
    assert caught.value is failure


def test_outbound_events_classify_dispatch_loss_and_missing_movement():
    rows = [
        telemetry_row(1, OUTBOUND_ORDER_CREATED, START, "10"),
        telemetry_row(2, OUTBOUND_ORDER_CREATED, START + timedelta(minutes=1), "11"),
        telemetry_row(3, OUTBOUND_ORDER_CREATED, START + timedelta(minutes=2), "12"),
    ]
    exits = [
        {
            "movement_id": 10,
            "exit_type": "dispatch",
            "warehouse": "LA",
            "quantity": 3,
            "sku_id": 42,
            "client_id": None,
            "product_id": 42,
        },
        {
            "movement_id": 11,
            "exit_type": "loss",
            "warehouse": "LA",
            "quantity": 3,
            "sku_id": 42,
            "client_id": None,
            "product_id": 42,
        },
    ]
    result = extract_weekly_inputs(FakeEngine(FakeConnection(events=rows, stockexits=exits)), WEEK)

    assert result.stockexit_evidence.records == {
        "10": StockExitRecord("10", "dispatch", "LA", None, "42", 3),
        "11": StockExitRecord("11", "loss", "LA", None, "42", 3),
    }
    assert result.stockexit_evidence.lookup_complete is True
    transformed = transform_weekly_performance(
        result.events,
        stockexit_evidence=result.stockexit_evidence,
        movement_week_evidence=result.movement_week_evidence,
    )
    assert transformed.aggregates == ()
    assert transformed.quality.accepted_event_ids == ()
    assert set(transformed.quality.rejected_event_ids) == {str(row["id"]) for row in rows}
    assert any(issue.code == "stockexit_identity_unverified" for issue in transformed.quality.issues)
    assert any(issue.code == "stockexit_movement_missing" for issue in transformed.quality.issues)


def test_null_sku_client_id_from_extraction_is_not_a_verified_dispatch():
    row = telemetry_row(13, OUTBOUND_ORDER_CREATED, START, "13")
    stockexit = {
        "movement_id": 13,
        "exit_type": "dispatch",
        "warehouse": "LA",
        "quantity": 3,
        "sku_id": 42,
        "client_id": None,
        "product_id": 42,
    }
    result = extract_weekly_inputs(
        FakeEngine(FakeConnection(events=[row], stockexits=[stockexit])), WEEK
    )

    transformed = transform_weekly_performance(
        result.events,
        stockexit_evidence=result.stockexit_evidence,
        movement_week_evidence=result.movement_week_evidence,
    )
    assert transformed.aggregates == ()
    assert transformed.quality.rejected_event_ids == (str(row["id"]),)
    assert transformed.quality.issues[0].code == "stockexit_identity_unverified"


def test_invalid_stockexit_movement_ids_are_not_coerced_and_mark_evidence_incomplete():
    row = telemetry_row(1, OUTBOUND_ORDER_CREATED, START, "001; DROP TABLE stockexit")
    connection = FakeConnection(events=[row])
    result = extract_weekly_inputs(FakeEngine(connection), WEEK)

    assert result.stockexit_evidence.records == {}
    assert result.stockexit_evidence.lookup_complete is False
    assert result.metadata.stockexit_invalid_ids == ("001; DROP TABLE stockexit",)
    assert not any("FROM stockexit" in sql for sql, _ in connection.executed)
    transformed = transform_weekly_performance(
        result.events,
        stockexit_evidence=result.stockexit_evidence,
        movement_week_evidence=result.movement_week_evidence,
    )
    assert any(issue.code == "outbound_classification_unavailable" for issue in transformed.quality.issues)


def test_multiple_valid_stockexit_ids_are_fetched_in_one_parameterized_query():
    rows = [
        telemetry_row(1, OUTBOUND_ORDER_CREATED, START, "10"),
        telemetry_row(2, OUTBOUND_ORDER_CREATED, START, "11"),
    ]
    exits = [
        {
            "movement_id": movement_id,
            "exit_type": exit_type,
            "warehouse": "LA",
            "quantity": 3,
            "sku_id": 42,
            "client_id": "cid",
            "product_id": 42,
        }
        for movement_id, exit_type in ((10, "dispatch"), (11, "loss"))
    ]
    connection = FakeConnection(events=rows, stockexits=exits)
    result = extract_weekly_inputs(FakeEngine(connection), WEEK)

    stock_queries = [(sql, params) for sql, params in connection.executed if "FROM stockexit AS e" in sql]
    assert len(stock_queries) == 1
    assert stock_queries[0][1] == {"movement_ids": [10, 11]}
    assert set(result.stockexit_evidence.records) == {"10", "11"}
    assert result.stockexit_evidence.records["10"].client_id == "cid"


def test_duplicate_movement_across_weeks_preserves_timestamps_and_evidence():
    rows = [
        telemetry_row(1, INBOUND_ORDER_CREATED, START + timedelta(hours=2), "same"),
        telemetry_row(2, INBOUND_ORDER_CREATED, NEXT + timedelta(hours=2), "same"),
        telemetry_row(3, OUTBOUND_ORDER_CREATED, START + timedelta(hours=3), "same"),
    ]
    result = extract_weekly_inputs(FakeEngine(FakeConnection(events=rows)), WEEK)

    inbound_key = (INBOUND_ORDER_CREATED, "same")
    outbound_key = (OUTBOUND_ORDER_CREATED, "same")
    assert result.movement_week_evidence[inbound_key] == MovementWeekEvidence(
        frozenset({WEEK, date(2026, 10, 26)}), True
    )
    assert result.movement_week_evidence[outbound_key] == MovementWeekEvidence(
        frozenset({WEEK}), True
    )
    assert len(result.movement_occurrences) == 3
    assert {item.timestamp for item in result.movement_occurrences} == {
        START + timedelta(hours=2),
        NEXT + timedelta(hours=2),
        START + timedelta(hours=3),
    }


def test_movement_ids_are_parameters_not_interpolated_into_sql():
    malicious = "x' OR 1=1 --"
    row = telemetry_row(1, INBOUND_ORDER_CREATED, START, malicious)
    connection = FakeConnection(events=[row])
    result = extract_weekly_inputs(FakeEngine(connection), WEEK)

    movement_sql, movement_parameters = connection.executed[1]
    assert malicious not in movement_sql
    assert malicious in movement_parameters.values()
    assert result.movement_week_evidence[(INBOUND_ORDER_CREATED, malicious)].complete


def test_auxiliary_queries_share_one_read_only_repeatable_read_snapshot():
    rows = [telemetry_row(1, OUTBOUND_ORDER_CREATED, START, "10")]
    connection = FakeConnection(events=rows)
    result = extract_weekly_inputs(FakeEngine(connection), WEEK)
    assert connection.transaction_sql == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    ]
    assert result.metadata.snapshot_isolation == "REPEATABLE READ"
    assert result.metadata.read_only is True
    assert result.metadata.snapshot_consistent is True
    assert len(connection.executed) == 3


def test_non_postgresql_bind_is_rejected_instead_of_claiming_snapshot():
    connection = FakeConnection()
    connection.dialect = type("Dialect", (), {"name": "sqlite"})()
    with pytest.raises(WeeklyExtractionError, match="PostgreSQL is required"):
        extract_weekly_inputs(FakeEngine(connection), WEEK)


def test_all_extractor_selects_are_read_only_and_phase_one_contract_compatible():
    rows = [
        telemetry_row(1, INBOUND_ORDER_CREATED, START, "in-1"),
        telemetry_row(2, STOCK_THRESHOLD_TRIGGERED, START),
        telemetry_row(3, INVENTORY_DISCREPANCY_DETECTED, START),
    ]
    connection = FakeConnection(events=rows)
    result = extract_weekly_inputs(FakeEngine(connection), WEEK)

    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in connection.executed)
    assert result.metadata.event_count == len(result.events)
    transformed = transform_weekly_performance(
        result.events,
        stockexit_evidence=result.stockexit_evidence,
        movement_week_evidence=result.movement_week_evidence,
    )
    assert transformed.aggregates[0].inbound_units_count == 3
    assert transformed.aggregates[0].stockout_events_count == 1
    assert transformed.aggregates[0].discrepancy_events_count == 1


def test_non_monday_week_is_rejected_before_opening_connection():
    engine = FakeEngine()
    with pytest.raises(ValueError, match="Monday"):
        extract_weekly_inputs(engine, date(2026, 10, 20))
    assert engine.connection.executed == []
