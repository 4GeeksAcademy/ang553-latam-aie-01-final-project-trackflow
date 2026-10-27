"""Static contract checks for the weekly reporting PostgreSQL DDL.

These tests inspect the SQL text only; they do not execute PostgreSQL DDL or
verify server-side constraint/default behavior.
"""

from pathlib import Path
import re
from dataclasses import fields

from pipelines.weekly_performance import WeeklyAggregate


ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "infra/sql/reporting_weekly_warehouse_client_performance.sql"

EXPECTED_COLUMNS = {
    "id": "uuid",
    "warehouse": "text",
    "client_id": "text",
    "week_start": "date",
    "inbound_units_count": "integer",
    "outbound_orders_count": "integer",
    "stockout_events_count": "integer",
    "discrepancy_events_count": "integer",
    "discrepancy_rate": "numeric",
    "computed_at": "timestamptz",
}


def _table_definition() -> tuple[str, str]:
    sql = SQL_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
        r"reporting\.weekly_warehouse_client_performance\s*\((.*?)\)\s*;",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, "reporting table declaration is missing"
    return sql, match.group(1)


def test_creates_reporting_schema_and_official_table():
    sql, _ = _table_definition()
    assert re.search(r"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+reporting\s*;", sql, re.I)


def test_declares_exact_columns_and_postgresql_types():
    _, body = _table_definition()
    declarations = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        match = re.match(r"([a-z_]+)\s+([a-z]+(?:\s+precision)?)\b", line, re.I)
        if match:
            declarations[match.group(1)] = " ".join(match.group(2).lower().split())
    assert declarations == EXPECTED_COLUMNS


def test_declares_uuid_primary_key_and_server_default():
    _, body = _table_definition()
    assert re.search(r"\bid\s+uuid\s+PRIMARY\s+KEY\s+DEFAULT\s+gen_random_uuid\s*\(\s*\)", body, re.I)


def test_preserves_required_counter_rate_and_timestamp_defaults():
    _, body = _table_definition()
    for column in (
        "inbound_units_count",
        "outbound_orders_count",
        "stockout_events_count",
        "discrepancy_events_count",
    ):
        assert re.search(rf"\b{column}\s+integer\s+NOT\s+NULL\s+DEFAULT\s+0\b", body, re.I)
    assert re.search(r"\bdiscrepancy_rate\s+numeric\s+NOT\s+NULL\s+DEFAULT\s+0\b", body, re.I)
    assert re.search(r"\bcomputed_at\s+timestamptz\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)", body, re.I)


def test_declares_only_the_official_unique_grain():
    _, body = _table_definition()
    assert re.search(
        r"\bUNIQUE\s*\(\s*warehouse\s*,\s*client_id\s*,\s*week_start\s*\)",
        body,
        re.I,
    )


def test_schema_contains_no_unapproved_or_destructive_operations():
    sql, body = _table_definition()
    column_names = set()
    for line in body.splitlines():
        match = re.match(r"\s*([a-z_]+)\s+(?:uuid|text|date|integer|numeric|timestamptz)\b", line, re.I)
        if match:
            column_names.add(match.group(1))
    assert column_names == set(EXPECTED_COLUMNS)
    executable_sql = re.sub(r"--[^\n]*", "", sql)
    assert not re.search(r"\b(?:DROP\s+TABLE|TRUNCATE\b|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+telemetry_events|DELETE\s+FROM\s+telemetry_events)\b", executable_sql, re.I)
    assert not re.search(r"\b(?:CREATE|ALTER)\s+TABLE\s+(?!IF\s+NOT\s+EXISTS\s+reporting\.weekly_warehouse_client_performance\b)", executable_sql, re.I)
    assert "telemetry_events" not in executable_sql.lower()


def test_schema_matches_all_weekly_aggregate_fields():
    _, body = _table_definition()
    # The SQL table additionally has generated id and computed_at metadata.
    aggregate_fields = {field.name for field in fields(WeeklyAggregate)}
    assert len(aggregate_fields) == 8
    declared_columns = {
        name
        for name in EXPECTED_COLUMNS
        if re.search(rf"\b{re.escape(name)}\s+", body, re.I)
    }
    assert aggregate_fields <= declared_columns
