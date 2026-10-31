"""Conservative operational coverage audit for one closed ISO week."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from pipelines.coordinator import CoverageEvidence
from pipelines.database_identity import ensure_distinct_postgresql_databases
from pipelines.weekly_performance import OFFICIAL_EVENT_TYPES, WeeklyTransformationResult, iso_week_bounds
from services.api.telemetry_storage import map_outbox_event_to_row
from services.api.telemetry_utils import canonical_telemetry_warehouse

OPERATIONAL_COVERAGE_SOURCE = "operational_checkpoints"


@dataclass(frozen=True)
class OperationalSnapshot:
    entries: tuple[Mapping[str, Any], ...]
    orders: tuple[Mapping[str, Any], ...]
    dispatches: tuple[Mapping[str, Any], ...]
    movements: tuple[Mapping[str, Any], ...]
    checkpoints: tuple[Mapping[str, Any], ...]
    counts: tuple[Mapping[str, Any], ...]
    outbox: tuple[Mapping[str, Any], ...]
    telemetry: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class OperationalCoverageAudit:
    evidence: CoverageEvidence
    diagnostics: tuple[str, ...]
    audited_events: tuple[tuple[Any, ...], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.diagnostics and self.evidence.lookup_complete


def _canonical_warehouse(value: Any) -> str | None:
    if value in {"los_angeles", "zaragoza"}:
        return value
    return canonical_telemetry_warehouse(value) if isinstance(value, str) else None


def _rows_by(rows: Sequence[Mapping[str, Any]], *fields: str) -> dict[tuple[Any, ...], list[Mapping[str, Any]]]:
    result: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        result[tuple(row[field] for field in fields)].append(row)
    return result


def _timestamp(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value


def _canonical_event(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row["id"]),
        _timestamp(row["timestamp"]),
        row["service"],
        row["event_type"],
        row["tags"],
    )


def audit_operational_snapshot(snapshot: OperationalSnapshot) -> OperationalCoverageAudit:
    """Audit a fully loaded source/reporting snapshot without inferring absence."""
    diagnostics: list[str] = []
    outbox = _rows_by(snapshot.outbox, "source_table", "source_pk", "event_type")
    checkpoints = _rows_by(snapshot.checkpoints, "source_table", "source_pk")
    movements = {(row["source_table"], row["source_pk"]): row for row in snapshot.movements}
    dispatch_ids = [str(row["dispatch_id"]) for row in snapshot.dispatches]
    if len(dispatch_ids) != len(set(dispatch_ids)):
        diagnostics.append("dispatch_id_duplicate")
    dispatches_by_order = _rows_by(snapshot.dispatches, "order_pk")
    expected_outbox_keys: set[tuple[str, int, str]] = set()

    def exact_outbox(source: str, pk: int, event_type: str) -> Mapping[str, Any] | None:
        rows = outbox.get((source, pk, event_type), [])
        if len(rows) != 1:
            diagnostics.append(f"{event_type}_outbox_count_mismatch")
            return None
        return rows[0]

    for entry in snapshot.entries:
        expected_outbox_keys.add(("stockentry", entry["id"], "inbound_order_created"))
        row = exact_outbox("stockentry", entry["id"], "inbound_order_created")
        if row is not None:
            expected = {
                "warehouse": _canonical_warehouse(entry["warehouse"]),
                "client_id": entry["client_id"],
                "product_id": str(entry["sku_id"]),
                "product_category": entry["category"],
                "quantity": entry["quantity"],
                "movement_id": str(entry["id"]),
            }
            if row["tags"] != expected:
                diagnostics.append("inbound_outbox_content_mismatch")

    dispatch_pairs: list[tuple[str, str]] = []
    for order in snapshot.orders:
        expected_outbox_keys.add(("commercial_order", order["id"], "outbound_order_created"))
        related = dispatches_by_order.get((order["id"],), [])
        if not related:
            diagnostics.append("commercial_order_without_dispatch")
        for dispatch in related:
            dispatch_pairs.append((str(dispatch["dispatch_id"]), str(order["order_id"])))
        row = exact_outbox("commercial_order", order["id"], "outbound_order_created")
        if row is not None and row["tags"] != {
            "order_id": str(order["order_id"]),
            "warehouse": _canonical_warehouse(order["warehouse"]),
            "client_id": order["client_id"],
        }:
            diagnostics.append("outbound_outbox_content_mismatch")

    for key, movement in movements.items():
        rows = checkpoints.get(key, [])
        if len(rows) != 1:
            diagnostics.append("threshold_checkpoint_count_mismatch")
            continue
        checkpoint = rows[0]
        expected_after = (
            checkpoint["before_quantity"] + movement["quantity"]
            if key[0] == "stockentry"
            else checkpoint["before_quantity"] - movement["quantity"]
        )
        crossed = (
            key[0] == "stockexit"
            and checkpoint["threshold_quantity"] is not None
            and checkpoint["before_quantity"] > checkpoint["threshold_quantity"]
            and checkpoint["after_quantity"] <= checkpoint["threshold_quantity"]
        )
        if (
            checkpoint["sku_id"] != movement["sku_id"]
            or checkpoint["client_id"] != movement["client_id"]
            or checkpoint["warehouse"] != movement["warehouse"]
            or checkpoint["after_quantity"] != expected_after
            or checkpoint["crossed"] is not crossed
        ):
            diagnostics.append("threshold_checkpoint_content_mismatch")
        if checkpoint.get("evaluated_at") != movement.get("created_at"):
            diagnostics.append("threshold_checkpoint_timestamp_mismatch")
        threshold_rows = outbox.get((key[0], key[1], "stock_threshold_triggered"), [])
        if crossed:
            expected_outbox_keys.add((key[0], key[1], "stock_threshold_triggered"))
            if len(threshold_rows) != 1 or checkpoint["event_id"] != threshold_rows[0]["event_id"]:
                diagnostics.append("threshold_crossed_event_mismatch")
            elif threshold_rows[0]["tags"] != {
                "warehouse": _canonical_warehouse(movement["warehouse"]),
                "client_id": movement["client_id"],
                "product_id": str(movement["sku_id"]),
                "product_category": movement["category"],
                "quantity": checkpoint["after_quantity"],
                "threshold_quantity": checkpoint["threshold_quantity"],
                "movement_id": str(key[1]),
            }:
                diagnostics.append("threshold_outbox_content_mismatch")
        elif threshold_rows or checkpoint["event_id"] is not None:
            diagnostics.append("threshold_not_crossed_has_event")

    for count in snapshot.counts:
        rows = outbox.get(
            ("inventory_physical_count", count["id"], "inventory_discrepancy_detected"), []
        )
        discrepant = count["expected_quantity"] != count["observed_quantity"]
        if discrepant:
            expected_outbox_keys.add(
                ("inventory_physical_count", count["id"], "inventory_discrepancy_detected")
            )
        if discrepant and len(rows) != 1:
            diagnostics.append("discrepancy_outbox_count_mismatch")
        if not discrepant and rows:
            diagnostics.append("matching_count_has_discrepancy_event")
        if discrepant and len(rows) == 1:
            delta = count["observed_quantity"] - count["expected_quantity"]
            expected_tags = {
                "warehouse": _canonical_warehouse(count["warehouse"]),
                "client_id": count["client_id"],
                "product_id": str(count["sku_id"]),
                "product_category": count["category"],
                "quantity": abs(delta),
                "system_quantity": count["expected_quantity"],
                "counted_quantity": count["observed_quantity"],
                "discrepancy_delta": delta,
                "discrepancy_type": "surplus" if delta > 0 else "shortage",
                "count_id": str(count["count_id"]),
            }
            if rows[0]["tags"] != expected_tags:
                diagnostics.append("discrepancy_outbox_content_mismatch")

    extra_checkpoints = set(checkpoints) - set(movements)
    if extra_checkpoints:
        diagnostics.append("unexpected_threshold_checkpoint")
    actual_outbox_keys = {
        (row["source_table"], row["source_pk"], row["event_type"])
        for row in snapshot.outbox
    }
    if actual_outbox_keys - expected_outbox_keys:
        diagnostics.append("unexpected_outbox")
    if expected_outbox_keys - actual_outbox_keys:
        diagnostics.append("expected_outbox_missing")

    official_telemetry = tuple(
        row
        for row in snapshot.telemetry
        if row.get("service") == "api" and row.get("event_type") in OFFICIAL_EVENT_TYPES
    )
    telemetry_by_id = _rows_by(official_telemetry, "id")
    outbox_ids = {row["event_id"] for row in snapshot.outbox}
    if {row["id"] for row in official_telemetry} - outbox_ids:
        diagnostics.append("official_telemetry_without_outbox")
    for row in snapshot.outbox:
        if row["published_at"] is None:
            diagnostics.append("outbox_pending")
        targets = telemetry_by_id.get((row["event_id"],), [])
        if len(targets) != 1:
            diagnostics.append("telemetry_target_count_mismatch")
            continue
        expected = map_outbox_event_to_row(
            event_id=row["event_id"],
            occurred_at=row["occurred_at"],
            event_type=row["event_type"],
            tags=row["tags"],
        )
        target = targets[0]
        if any(
            target.get(field) != expected.get(field)
            for field in ("id", "service", "event_type", "level", "value", "message", "tags")
        ) or _timestamp(target["timestamp"]) != _timestamp(expected["timestamp"]):
            diagnostics.append("telemetry_target_content_mismatch")

    diagnostics = sorted(set(diagnostics))
    audited_events = (
        tuple(sorted((_canonical_event(row) for row in official_telemetry), key=repr))
        if not diagnostics
        else ()
    )
    evidence = CoverageEvidence(
        confirmed_event_types=(frozenset(OFFICIAL_EVENT_TYPES) if not diagnostics else frozenset()),
        dispatch_order_ids=tuple(sorted(dispatch_pairs)),
        lookup_complete=not diagnostics,
        source=OPERATIONAL_COVERAGE_SOURCE,
    )
    return OperationalCoverageAudit(evidence, tuple(diagnostics), audited_events)


def apply_operational_coverage(
    transformed: WeeklyTransformationResult,
    audit: OperationalCoverageAudit,
    extracted: Any,
) -> WeeklyTransformationResult:
    if not audit.complete:
        return transformed
    extracted_events = tuple(
        sorted((_canonical_event(row) for row in extracted.events), key=repr)
    )
    if extracted_events != audit.audited_events:
        return transformed
    return replace(
        transformed,
        quality=replace(transformed.quality, coverage_pending=()),
    )


def _all(connection: Any, sql: str, parameters: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(connection.execute(text(sql), dict(parameters)).mappings().all())


def audit_operational_coverage(
    source_engine: Any,
    reporting_engine: Any,
    week_start: date,
) -> OperationalCoverageAudit:
    """Load and audit one week from distinct PostgreSQL source/reporting DBs."""
    ensure_distinct_postgresql_databases(source_engine, reporting_engine)
    start, end = iso_week_bounds(week_start)
    parameters = {"start": start, "end": end}
    with source_engine.connect() as source:
        source.begin()
        source.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        entries = _all(source, """
            SELECT e.id, e.sku_id, e.quantity, e.warehouse, e.created_at,
                   s.client_id, s.category
            FROM stockentry e JOIN sku s ON s.id=e.sku_id
            WHERE e.created_at >= :start AND e.created_at < :end
        """, parameters)
        orders = _all(source, """
            SELECT id, order_id, client_id, warehouse, created_at
            FROM commercial_order WHERE created_at >= :start AND created_at < :end
        """, parameters)
        dispatches = _all(source, """
            SELECT id, dispatch_id, commercial_order_id AS order_pk
            FROM stockexit WHERE commercial_order_id IS NOT NULL
        """, parameters)
        movements = _all(source, """
            SELECT 'stockentry' AS source_table, e.id AS source_pk, e.sku_id,
                   e.quantity, e.warehouse, s.client_id, s.category, e.created_at
            FROM stockentry e JOIN sku s ON s.id=e.sku_id
            WHERE e.created_at >= :start AND e.created_at < :end
            UNION ALL
            SELECT 'stockexit', e.id, e.sku_id, e.quantity, e.warehouse, s.client_id, s.category, e.created_at
            FROM stockexit e JOIN sku s ON s.id=e.sku_id
            WHERE e.created_at >= :start AND e.created_at < :end
        """, parameters)
        checkpoints = _all(source, """
            SELECT source_table, source_pk, sku_id, client_id, warehouse,
                   before_quantity, after_quantity, threshold_quantity,
                   crossed, event_id, evaluated_at
            FROM inventory_threshold_evaluation
            WHERE evaluated_at >= :start AND evaluated_at < :end
        """, parameters)
        counts = _all(source, """
            SELECT c.id, c.count_id, c.sku_id, c.client_id, c.warehouse,
                   c.expected_quantity, c.observed_quantity, s.category
            FROM inventory_physical_count c JOIN sku s ON s.id=c.sku_id
            WHERE c.confirmed_at >= :start AND c.confirmed_at < :end
        """, parameters)
        outbox = _all(source, """
            SELECT event_id, occurred_at, source_table, source_pk, event_type,
                   tags, published_at
            FROM inventory_event_outbox
            WHERE occurred_at >= :start AND occurred_at < :end
        """, parameters)
        source.rollback()
    with reporting_engine.connect() as reporting:
        reporting.begin()
        reporting.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        telemetry = _all(reporting, """
            SELECT id, timestamp, service, event_type, level, value, message, tags
            FROM telemetry_events
            WHERE timestamp >= :start AND timestamp < :end
              AND service = 'api'
              AND event_type IN (
                'inbound_order_created', 'outbound_order_created',
                'stock_threshold_triggered', 'inventory_discrepancy_detected'
              )
        """, parameters)
        reporting.rollback()
    return audit_operational_snapshot(
        OperationalSnapshot(entries, orders, dispatches, movements, checkpoints, counts, outbox, telemetry)
    )
