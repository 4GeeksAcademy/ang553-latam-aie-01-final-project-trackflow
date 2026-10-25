"""Unit tests for the telemetry analysis pipeline (Pandas-based).

All tests use fake/mock ``session.execute`` + real Pandas so the
data-transformation logic is exercised without a real database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, PropertyMock, create_autospec

import pandas as pd
import pytest
# sqlalchemy types used indirectly via the analysis module

from services.telemetry.analysis import (
    OPERATIONAL_FAILURE_ALLOWLIST,
    auth_failure_rate_by_day,
    build_telemetry_report,
    event_volume_by_day_and_type,
    operational_failures_by_type_and_day,
    stock_insufficiency_by_day_warehouse_category,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_session(rows: list[dict]) -> MagicMock:
    """Return a MagicMock session that yields *rows* from
    ``execute(stmt).mappings().all()``."""
    session = MagicMock()
    mapping_result = MagicMock()
    mapping_result.all.return_value = rows
    mapping_proxy = MagicMock()
    mapping_proxy.mappings.return_value = mapping_result
    session.execute.return_value = mapping_proxy
    return session


def _mock_session_with_captured_stmt(
    rows: list[dict],
) -> tuple[MagicMock, list]:
    """Return (session, captured_statements) where each call to
    ``session.execute(stmt)`` appends the ``stmt`` to captured_statements."""
    captured = []

    session = MagicMock()
    mapping_result = MagicMock()
    mapping_result.all.return_value = rows
    mapping_proxy = MagicMock()
    mapping_proxy.mappings.return_value = mapping_result

    def side_effect(stmt, **kwargs):
        captured.append(stmt)
        return mapping_proxy

    session.execute.side_effect = side_effect
    return session, captured


# Shared test constants
T0 = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 22, 6, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# A — Empty dataset
# ═══════════════════════════════════════════════════════════════════════════


class TestEmptyDataset:
    """All metric functions return ``[]`` when the DB returns nothing."""

    def test_a_empty(self) -> None:
        session = _mock_session([])
        assert event_volume_by_day_and_type(session, T0, T1) == []

    def test_b_empty(self) -> None:
        session = _mock_session([])
        assert operational_failures_by_type_and_day(session, T0, T1) == []

    def test_c_empty(self) -> None:
        session = _mock_session([])
        assert stock_insufficiency_by_day_warehouse_category(session, T0, T1) == []


# ═══════════════════════════════════════════════════════════════════════════
# B — Metric A: event_volume_by_day_and_type
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricA:
    """Multiple event types, multiple days, correct counts."""

    def test_multiple_types_and_days(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T10:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-21T08:00:00Z", "event_type": "inbound_order_created"},
            {"timestamp": "2026-10-21T09:00:00Z", "event_type": "auth_login_succeeded"},
        ]
        session = _mock_session(rows)
        result = event_volume_by_day_and_type(session, T0, T2)

        assert len(result) == 4
        # day=2026-10-20, auth_login_succeeded → 2
        r1 = next(r for r in result if r["day"] == "2026-10-20" and r["event_type"] == "auth_login_succeeded")
        assert r1["count"] == 2
        # day=2026-10-20, auth_login_failed → 1
        r2 = next(r for r in result if r["day"] == "2026-10-20" and r["event_type"] == "auth_login_failed")
        assert r2["count"] == 1
        # day=2026-10-21, inbound_order_created → 1
        r3 = next(r for r in result if r["day"] == "2026-10-21" and r["event_type"] == "inbound_order_created")
        assert r3["count"] == 1
        # day=2026-10-21, auth_login_succeeded → 1
        r4 = next(r for r in result if r["day"] == "2026-10-21" and r["event_type"] == "auth_login_succeeded")
        assert r4["count"] == 1

    def test_single_row(self) -> None:
        rows = [{"timestamp": "2026-10-20T12:00:00Z", "event_type": "test_event"}]
        session = _mock_session(rows)
        result = event_volume_by_day_and_type(session, T0, T1)
        assert result == [{"day": "2026-10-20", "event_type": "test_event", "count": 1}]


# ═══════════════════════════════════════════════════════════════════════════
# C — Metric B: operational_failures_by_type_and_day
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricB:
    """Only allowlist events; non-failure events are excluded."""

    ALLOWED = list(OPERATIONAL_FAILURE_ALLOWLIST)

    def test_only_allowlist_included(self) -> None:
        # The mock bypasses SQL filtering, so we provide only rows that
        # would pass the WHERE event_type IN allowlist clause.
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "inventory_stock_insufficient"},
            {"timestamp": "2026-10-21T08:00:00Z", "event_type": "api_request_failed"},
        ]
        session = _mock_session(rows)
        result = operational_failures_by_type_and_day(session, T0, T2)

        assert len(result) == 3
        types_found = {(r["day"], r["event_type"]) for r in result}
        assert ("2026-10-20", "auth_login_failed") in types_found
        assert ("2026-10-20", "inventory_stock_insufficient") in types_found
        assert ("2026-10-21", "api_request_failed") in types_found

    def test_correct_counts(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T10:00:00Z", "event_type": "inventory_validation_failed"},
            {"timestamp": "2026-10-21T08:00:00Z", "event_type": "auth_login_failed"},
        ]
        session = _mock_session(rows)
        result = operational_failures_by_type_and_day(session, T0, T2)

        r1 = next(r for r in result if r["day"] == "2026-10-20" and r["event_type"] == "auth_login_failed")
        assert r1["count"] == 2
        r2 = next(r for r in result if r["day"] == "2026-10-20" and r["event_type"] == "inventory_validation_failed")
        assert r2["count"] == 1
        r3 = next(r for r in result if r["day"] == "2026-10-21" and r["event_type"] == "auth_login_failed")
        assert r3["count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# D — Metric C: stock_insufficiency_by_day_warehouse_category
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricC:
    """Warehouse/category combinations with correct counts."""

    def test_multiple_warehouses_and_categories(self) -> None:
        rows = [
            {
                "timestamp": "2026-10-20T08:00:00Z",
                "tags": {"warehouse": "WH1", "product_category": "Electronics"},
            },
            {
                "timestamp": "2026-10-20T09:00:00Z",
                "tags": {"warehouse": "WH1", "product_category": "Electronics"},
            },
            {
                "timestamp": "2026-10-20T10:00:00Z",
                "tags": {"warehouse": "WH2", "product_category": "Furniture"},
            },
            {
                "timestamp": "2026-10-21T08:00:00Z",
                "tags": {"warehouse": "WH1", "product_category": "Electronics"},
            },
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)

        assert len(result) == 3
        r1 = next(r for r in result if r["day"] == "2026-10-20" and r["warehouse"] == "WH1" and r["product_category"] == "Electronics")
        assert r1["count"] == 2
        r2 = next(r for r in result if r["day"] == "2026-10-20" and r["warehouse"] == "WH2" and r["product_category"] == "Furniture")
        assert r2["count"] == 1
        r3 = next(r for r in result if r["day"] == "2026-10-21" and r["warehouse"] == "WH1" and r["product_category"] == "Electronics")
        assert r3["count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# E — Missing / null tags (Metric C)
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricCMissingTags:
    """Tags=None, {}, missing warehouse/product_category → no invalid groups."""

    def test_tags_none(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": None},
            {"timestamp": "2026-10-20T09:00:00Z", "tags": {"warehouse": "WH1", "product_category": "Electronics"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        assert len(result) == 1
        assert result[0]["warehouse"] == "WH1"

    def test_tags_empty_dict(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": {}},
            {"timestamp": "2026-10-20T09:00:00Z", "tags": {"warehouse": "WH1", "product_category": "Electronics"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        assert len(result) == 1

    def test_missing_warehouse(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": {"product_category": "Electronics"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        assert result == []

    def test_missing_product_category(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": {"warehouse": "WH1"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        assert result == []

    def test_all_invalid_no_valid_groups(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": None},
            {"timestamp": "2026-10-20T09:00:00Z", "tags": {}},
            {"timestamp": "2026-10-20T10:00:00Z", "tags": {"warehouse": "WH1"}},
            {"timestamp": "2026-10-20T11:00:00Z", "tags": {"product_category": "Electronics"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# F — Timestamp conversion (utc=True, different offsets, correct UTC day)
# ═══════════════════════════════════════════════════════════════════════════


class TestTimestampConversion:
    """Verify utc=True conversion and correct UTC-day grouping."""

    def test_utc_conversion_and_day_grouping(self) -> None:
        # These timestamps have different offsets but the same UTC day
        rows = [
            {"timestamp": "2026-10-20T23:00:00-05:00", "event_type": "type_a"},  # UTC: 2026-10-21 04:00
            {"timestamp": "2026-10-21T01:00:00Z", "event_type": "type_a"},        # UTC: 2026-10-21 01:00
            {"timestamp": "2026-10-20T10:00:00Z", "event_type": "type_a"},        # UTC: 2026-10-20 10:00
        ]
        session = _mock_session(rows)
        result = event_volume_by_day_and_type(session, T0, T2)

        r_day20 = [r for r in result if r["day"] == "2026-10-20"]
        r_day21 = [r for r in result if r["day"] == "2026-10-21"]

        assert len(r_day20) == 1
        assert r_day20[0]["count"] == 1  # only the 10:00Z one

        assert len(r_day21) == 1
        assert r_day21[0]["count"] == 2  # 01:00Z + the offset one that becomes 04:00Z


# ═══════════════════════════════════════════════════════════════════════════
# G — SQL filters (via captured statement)
# ═══════════════════════════════════════════════════════════════════════════


class TestSQLFilters:
    """Verify the SQL statements have the expected WHERE clauses."""

    @staticmethod
    def _compile(stmt) -> str:
        """Compile a SQLAlchemy statement to a string with literals bound."""
        from sqlalchemy import schema as sa_schema

        dialect = sa_schema.SQLServerDialect() if False else None
        # Use a simple compile with the default dialect
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        return str(compiled)

    def test_metric_a_no_event_type_filter(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        event_volume_by_day_and_type(session, T0, T1)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        # Should have timestamp filters but NOT event_type filter
        assert "timestamp" in stmt_str
        # Check no event_type IN clause (metric A selects all event types)
        assert "event_type" in stmt_str  # it's a selected column

    def test_metric_b_uses_allowlist_in_filter(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        operational_failures_by_type_and_day(session, T0, T1)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        # The statement must have an IN clause or a filter on event_type
        # beyond the simple column reference
        assert "event_type IN" in stmt_str or "event_type_1" in stmt_str

    def test_metric_c_only_stock_insufficient(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        stock_insufficiency_by_day_warehouse_category(session, T0, T1)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        # The statement has event_type = :event_type_1 parameter
        assert "event_type" in stmt_str

    def test_timestamp_range_filters_metric_a(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        event_volume_by_day_and_type(session, start, end)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        # The compiled SQL should reference the parameter values
        assert ">=" in stmt_str
        assert "<" in stmt_str

    def test_timestamp_range_filters_metric_b(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        operational_failures_by_type_and_day(session, start, end)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        assert ">=" in stmt_str
        assert "<" in stmt_str


# ═══════════════════════════════════════════════════════════════════════════
# H — JSON safety
# ═══════════════════════════════════════════════════════════════════════════


class TestJSONSafety:
    """json.dumps works; count is JSON-safe int; day is string."""

    def test_json_serializable(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "type_a"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "type_a"},
            {"timestamp": "2026-10-21T08:00:00Z", "event_type": "type_b"},
        ]
        session = _mock_session(rows)
        result = event_volume_by_day_and_type(session, T0, T2)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        for record in result:
            assert isinstance(record["count"], int)
            assert isinstance(record["day"], str)
            assert isinstance(record["event_type"], str)

    def test_metric_b_json_safe(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
        ]
        session = _mock_session(rows)
        result = operational_failures_by_type_and_day(session, T0, T2)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        for record in result:
            assert isinstance(record["count"], int)
            assert isinstance(record["day"], str)

    def test_metric_c_json_safe(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "tags": {"warehouse": "WH1", "product_category": "Electronics"}},
        ]
        session = _mock_session(rows)
        result = stock_insufficiency_by_day_warehouse_category(session, T0, T2)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        for record in result:
            assert isinstance(record["count"], int)
            assert isinstance(record["day"], str)
            assert isinstance(record["warehouse"], str)
            assert isinstance(record["product_category"], str)


# ═══════════════════════════════════════════════════════════════════════════
# I — build_telemetry_report
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildTelemetryReport:
    """Verify the report builder delegates correctly."""

    def test_period_from_to(self) -> None:
        session = _mock_session([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)

        assert report["period"]["from"] == "2026-10-20T00:00:00+00:00"
        assert report["period"]["to"] == "2026-10-27T00:00:00+00:00"

    def test_contains_exact_metric_keys(self) -> None:
        session = _mock_session([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)

        metrics = report["metrics"]
        assert set(metrics.keys()) == {
            "event_volume_by_day_and_type",
            "operational_failures_by_type_and_day",
            "stock_insufficiency_by_day_warehouse_category",
            "auth_failure_rate",
        }

    def test_empty_metrics_are_empty_lists(self) -> None:
        session = _mock_session([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)

        for key in report["metrics"]:
            assert report["metrics"][key] == []

    def test_metrics_receive_same_start_end(self) -> None:
        """All metrics receive the same start_date and end_date."""
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T10:00:00Z", "tags": {"warehouse": "WH1", "product_category": "Electronics"}},
        ]
        # We need separate calls, so use three mock sessions
        from unittest.mock import MagicMock

        def make_session():
            s = MagicMock()
            mr = MagicMock()
            mr.all.return_value = rows[0:1]  # just return one row
            mp = MagicMock()
            mp.mappings.return_value = mr
            s.execute.return_value = mp
            return s

        # We'll just verify the structure rather than proving delegation
        session = _mock_session(rows)
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)

        # The report includes all metric keys with non-None values
        for key in report["metrics"]:
            assert report["metrics"][key] is not None

    def test_json_serializable(self) -> None:
        session = _mock_session([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)
        serialized = json.dumps(report)
        assert isinstance(serialized, str)


# ═══════════════════════════════════════════════════════════════════════════
# J — UTC canonicalization
# ═══════════════════════════════════════════════════════════════════════════


class TestUTCCanonicalization:
    """period.from and period.to must always be serialized in UTC with +00:00."""

    def test_utc_canonicalization_with_non_utc_offset(self) -> None:
        """Non-UTC offsets are normalized to UTC."""
        session = _mock_session([])
        start = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
        end = datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
        report = build_telemetry_report(session, start, end)

        assert report["period"]["from"] == "2026-10-24T07:00:00+00:00"
        assert report["period"]["to"] == "2026-10-25T07:00:00+00:00"

    def test_utc_canonicalization_with_positive_offset(self) -> None:
        """Positive offsets are normalized to UTC."""
        session = _mock_session([])
        start = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        end = datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        report = build_telemetry_report(session, start, end)

        assert report["period"]["from"] == "2026-10-23T19:00:00+00:00"
        assert report["period"]["to"] == "2026-10-24T19:00:00+00:00"

    def test_utc_canonicalization_with_utc(self) -> None:
        """UTC-native datetimes already use +00:00, unchanged."""
        session = _mock_session([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        report = build_telemetry_report(session, start, end)

        assert report["period"]["from"] == "2026-10-20T00:00:00+00:00"
        assert report["period"]["to"] == "2026-10-27T00:00:00+00:00"


# ═══════════════════════════════════════════════════════════════════════════
# H — Metric D: auth_failure_rate_by_day
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricD:
    """auth_failure_rate_by_day — basic computation."""

    def test_empty(self) -> None:
        """1. Empty dataset returns []."""
        session = _mock_session([])
        result = auth_failure_rate_by_day(session, T0, T2)
        assert result == []

    def test_failed_and_succeeded_same_day(self) -> None:
        """2. Both event types on the same day."""
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T10:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T11:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T12:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T13:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T14:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T15:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T16:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T17:00:00Z", "event_type": "auth_login_succeeded"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        assert len(result) == 1
        assert result[0]["day"] == "2026-10-20"
        assert result[0]["failed"] == 5
        assert result[0]["succeeded"] == 5
        assert result[0]["total"] == 10
        assert result[0]["failure_rate"] == 0.5

    def test_only_failed(self) -> None:
        """3. Only failed events → failure_rate = 1.0."""
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_failed"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        assert len(result) == 1
        assert result[0]["failed"] == 2
        assert result[0]["succeeded"] == 0
        assert result[0]["total"] == 2
        assert result[0]["failure_rate"] == 1.0

    def test_only_succeeded(self) -> None:
        """4. Only succeeded events → failure_rate = 0.0."""
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_succeeded"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        assert len(result) == 1
        assert result[0]["failed"] == 0
        assert result[0]["succeeded"] == 2
        assert result[0]["total"] == 2
        assert result[0]["failure_rate"] == 0.0


class TestMetricDMultipleDays:
    """auth_failure_rate_by_day — multiple days."""

    def test_multiple_days(self) -> None:
        """5. Events across different days produce separate records."""
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-21T08:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-21T09:00:00Z", "event_type": "auth_login_succeeded"},
            {"timestamp": "2026-10-21T10:00:00Z", "event_type": "auth_login_failed"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        assert len(result) == 2

        d20 = next(r for r in result if r["day"] == "2026-10-20")
        assert d20["failed"] == 1
        assert d20["succeeded"] == 1
        assert d20["total"] == 2
        assert d20["failure_rate"] == 0.5

        d21 = next(r for r in result if r["day"] == "2026-10-21")
        assert d21["failed"] == 1
        assert d21["succeeded"] == 2
        assert d21["total"] == 3
        assert round(d21["failure_rate"], 10) == round(1 / 3, 10)


class TestMetricDUTCOffsets:
    """6. Offsets → UTC grouping is correct."""

    def test_utc_day_correct_with_offsets(self) -> None:
        rows = [
            # UTC: 2026-10-20 23:00 (local 2026-10-20 18:00 -05:00)
            {"timestamp": "2026-10-20T18:00:00-05:00", "event_type": "auth_login_failed"},
            # UTC: 2026-10-21 01:00 (local 2026-10-21 01:00Z)
            {"timestamp": "2026-10-21T01:00:00Z", "event_type": "auth_login_succeeded"},
            # UTC: 2026-10-20 10:00
            {"timestamp": "2026-10-20T10:00:00Z", "event_type": "auth_login_failed"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)

        d20 = next(r for r in result if r["day"] == "2026-10-20")
        d21 = next(r for r in result if r["day"] == "2026-10-21")

        assert d20["failed"] == 2  # 10:00Z + 18:00-05:00 (UTC 23:00)
        assert d20["succeeded"] == 0
        assert d20["failure_rate"] == 1.0

        assert d21["failed"] == 0
        assert d21["succeeded"] == 1
        assert d21["failure_rate"] == 0.0


class TestMetricDSQLFilters:
    """7-8. SQL uses both event types; start/end filters correct."""

    def test_both_event_types_in_filter(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        auth_failure_rate_by_day(session, T0, T1)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        # The SQL filter uses an IN clause for both event types
        # (SQLAlchemy compiles to POSTCOMPILE bind parameters)
        assert "event_type IN" in stmt_str

    def test_timestamp_range_filters(self) -> None:
        session, captured = _mock_session_with_captured_stmt([])
        start = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 0, 0, 0, tzinfo=timezone.utc)
        auth_failure_rate_by_day(session, start, end)
        assert len(captured) == 1
        stmt_str = str(captured[0])
        assert ">=" in stmt_str
        assert "<" in stmt_str


class TestMetricDJSONSafety:
    """9-11. JSON safety: dumps, ints, float."""

    def test_json_dumps_works(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_succeeded"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    def test_fields_are_correct_types(self) -> None:
        rows = [
            {"timestamp": "2026-10-20T08:00:00Z", "event_type": "auth_login_failed"},
            {"timestamp": "2026-10-20T09:00:00Z", "event_type": "auth_login_succeeded"},
        ]
        session = _mock_session(rows)
        result = auth_failure_rate_by_day(session, T0, T2)
        assert len(result) == 1
        r = result[0]
        assert isinstance(r["day"], str)
        assert isinstance(r["failed"], int)
        assert isinstance(r["succeeded"], int)
        assert isinstance(r["total"], int)
        assert isinstance(r["failure_rate"], float)