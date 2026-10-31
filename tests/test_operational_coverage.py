"""Adversarial unit contracts for operational checkpoint coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from pipelines.coordinator import _coverage_issues
from pipelines.operational_coverage import (
    OperationalSnapshot,
    apply_operational_coverage,
    audit_operational_snapshot,
)
from pipelines.weekly_performance import QualityReport, WeeklyTransformationResult
from services.api.telemetry_storage import map_outbox_event_to_row

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def _outbox(source, pk, event_type, tags, event_id=None):
    return {
        "event_id": event_id or uuid4(),
        "occurred_at": NOW,
        "source_table": source,
        "source_pk": pk,
        "event_type": event_type,
        "tags": tags,
        "published_at": NOW,
    }


def valid_snapshot() -> OperationalSnapshot:
    order_id = uuid4()
    count_id = uuid4()
    entry = {
        "id": 1, "sku_id": 10, "quantity": 10, "warehouse": "LA",
        "client_id": "client", "category": "electronics",
    }
    order = {"id": 20, "order_id": order_id, "client_id": "client", "warehouse": "LA"}
    dispatches = (
        {"id": 2, "dispatch_id": uuid4(), "order_pk": 20},
        {"id": 3, "dispatch_id": uuid4(), "order_pk": 20},
    )
    movements = (
        {"source_table": "stockentry", "source_pk": 1, "sku_id": 10, "quantity": 10, "warehouse": "LA", "client_id": "client", "category": "electronics", "created_at": NOW},
        {"source_table": "stockexit", "source_pk": 2, "sku_id": 10, "quantity": 7, "warehouse": "LA", "client_id": "client", "category": "electronics", "created_at": NOW},
    )
    threshold_event_id = uuid4()
    checkpoints = (
        {"source_table": "stockentry", "source_pk": 1, "sku_id": 10, "client_id": "client", "warehouse": "LA", "before_quantity": 0, "after_quantity": 10, "threshold_quantity": 3, "crossed": False, "event_id": None, "evaluated_at": NOW},
        {"source_table": "stockexit", "source_pk": 2, "sku_id": 10, "client_id": "client", "warehouse": "LA", "before_quantity": 10, "after_quantity": 3, "threshold_quantity": 3, "crossed": True, "event_id": threshold_event_id, "evaluated_at": NOW},
    )
    counts = (
        {"id": 30, "count_id": count_id, "sku_id": 10, "client_id": "client", "warehouse": "LA", "category": "electronics", "expected_quantity": 3, "observed_quantity": 2},
        {"id": 31, "count_id": uuid4(), "sku_id": 10, "client_id": "client", "warehouse": "LA", "category": "electronics", "expected_quantity": 3, "observed_quantity": 3},
    )
    outbox = (
        _outbox("stockentry", 1, "inbound_order_created", {"warehouse": "los_angeles", "client_id": "client", "product_id": "10", "product_category": "electronics", "quantity": 10, "movement_id": "1"}),
        _outbox("commercial_order", 20, "outbound_order_created", {"order_id": str(order_id), "warehouse": "los_angeles", "client_id": "client"}),
        _outbox("stockexit", 2, "stock_threshold_triggered", {"warehouse": "los_angeles", "client_id": "client", "product_id": "10", "product_category": "electronics", "quantity": 3, "threshold_quantity": 3, "movement_id": "2"}, threshold_event_id),
        _outbox("inventory_physical_count", 30, "inventory_discrepancy_detected", {"warehouse": "los_angeles", "client_id": "client", "product_id": "10", "product_category": "electronics", "quantity": 1, "system_quantity": 3, "counted_quantity": 2, "discrepancy_delta": -1, "discrepancy_type": "shortage", "count_id": str(count_id)}),
    )
    telemetry = tuple(
        map_outbox_event_to_row(
            event_id=row["event_id"], occurred_at=row["occurred_at"],
            event_type=row["event_type"], tags=row["tags"],
        )
        for row in outbox
    )
    return OperationalSnapshot((entry,), (order,), dispatches, movements, checkpoints, counts, outbox, telemetry)


def test_complete_operational_snapshot_accepts_multiple_dispatches_for_one_order():
    audit = audit_operational_snapshot(valid_snapshot())
    assert audit.complete
    assert len(audit.evidence.dispatch_order_ids) == 2
    assert len({order for _dispatch, order in audit.evidence.dispatch_order_ids}) == 1
    assert _coverage_issues(audit.evidence) == ()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda s: replace(s, checkpoints=s.checkpoints[1:]), "threshold_checkpoint_count_mismatch"),
        (lambda s: replace(s, checkpoints=({**s.checkpoints[0], "after_quantity": 9}, s.checkpoints[1])), "threshold_checkpoint_content_mismatch"),
        (lambda s: replace(s, outbox=s.outbox[1:], telemetry=s.telemetry[1:]), "inbound_order_created_outbox_count_mismatch"),
        (lambda s: replace(s, outbox=({**s.outbox[0], "published_at": None}, *s.outbox[1:])), "outbox_pending"),
        (lambda s: replace(s, telemetry=s.telemetry[1:]), "telemetry_target_count_mismatch"),
        (lambda s: replace(s, telemetry=({**s.telemetry[0], "tags": {"tampered": True}}, *s.telemetry[1:])), "telemetry_target_content_mismatch"),
        (lambda s: replace(s, dispatches=()), "commercial_order_without_dispatch"),
    ],
)
def test_missing_or_tampered_operational_evidence_blocks(mutation, code):
    audit = audit_operational_snapshot(mutation(valid_snapshot()))
    assert not audit.complete
    assert code in audit.diagnostics


def test_matching_count_with_event_and_threshold_event_mismatches_block():
    snapshot = valid_snapshot()
    improper_count = _outbox(
        "inventory_physical_count", 31, "inventory_discrepancy_detected",
        {"count_id": str(snapshot.counts[1]["count_id"])},
    )
    no_cross_threshold = _outbox("stockentry", 1, "stock_threshold_triggered", {"movement_id": "1"})
    telemetry = (*snapshot.telemetry, *(
        map_outbox_event_to_row(event_id=row["event_id"], occurred_at=row["occurred_at"], event_type=row["event_type"], tags=row["tags"])
        for row in (improper_count, no_cross_threshold)
    ))
    audit = audit_operational_snapshot(
        replace(snapshot, outbox=(*snapshot.outbox, improper_count, no_cross_threshold), telemetry=telemetry)
    )
    assert "matching_count_has_discrepancy_event" in audit.diagnostics
    assert "threshold_not_crossed_has_event" in audit.diagnostics


def test_crossed_checkpoint_without_threshold_event_blocks():
    snapshot = valid_snapshot()
    outbox = tuple(row for row in snapshot.outbox if row["event_type"] != "stock_threshold_triggered")
    telemetry = tuple(row for row in snapshot.telemetry if row["event_type"] != "stock_threshold_triggered")
    audit = audit_operational_snapshot(replace(snapshot, outbox=outbox, telemetry=telemetry))
    assert "threshold_crossed_event_mismatch" in audit.diagnostics


def test_extra_checkpoint_outbox_and_official_telemetry_are_rejected():
    snapshot = valid_snapshot()
    extra_checkpoint = {
        **snapshot.checkpoints[0],
        "source_pk": 999,
    }
    extra_outbox = _outbox(
        "stockentry", 999, "inbound_order_created",
        {"warehouse": "los_angeles", "client_id": "client", "movement_id": "999"},
    )
    extra_telemetry = map_outbox_event_to_row(
        event_id=uuid4(), occurred_at=NOW, event_type="inbound_order_created",
        tags={"warehouse": "los_angeles", "client_id": "client", "movement_id": "extra"},
    )
    audit = audit_operational_snapshot(
        replace(
            snapshot,
            checkpoints=(*snapshot.checkpoints, extra_checkpoint),
            outbox=(*snapshot.outbox, extra_outbox),
            telemetry=(*snapshot.telemetry, extra_telemetry),
        )
    )
    assert "unexpected_threshold_checkpoint" in audit.diagnostics
    assert "unexpected_outbox" in audit.diagnostics
    assert "official_telemetry_without_outbox" in audit.diagnostics


def test_complete_audit_only_clears_coverage_pending_and_never_certifies_production():
    quality = QualityReport((), (), (), (), (), (), (), (), ("pending",))
    transformed = WeeklyTransformationResult((), quality)
    snapshot = valid_snapshot()
    audit = audit_operational_snapshot(snapshot)
    extracted = type("Extracted", (), {"events": audit_snapshot_telemetry(snapshot)})()
    applied = apply_operational_coverage(transformed, audit, extracted)
    assert applied.quality.coverage_pending == ()
    assert audit.evidence.source == "operational_checkpoints"


@pytest.mark.parametrize("mutation", ["extra", "missing", "tampered"])
def test_extraction_change_after_audit_does_not_clear_coverage(mutation):
    snapshot = valid_snapshot()
    audit = audit_operational_snapshot(snapshot)
    events = list(audit_snapshot_telemetry(snapshot))
    if mutation == "extra":
        events.append({**events[0], "id": str(uuid4())})
    elif mutation == "missing":
        events.pop()
    else:
        events[0] = {**events[0], "tags": {**events[0]["tags"], "quantity": 999}}
    quality = QualityReport((), (), (), (), (), (), (), (), ("pending",))
    transformed = WeeklyTransformationResult((), quality)
    extracted = type("Extracted", (), {"events": tuple(events)})()
    applied = apply_operational_coverage(transformed, audit, extracted)
    assert applied.quality.coverage_pending == ("pending",)


def audit_snapshot_telemetry(snapshot):
    return tuple(
        {
            "id": str(row["id"]),
            "timestamp": row["timestamp"],
            "service": row["service"],
            "event_type": row["event_type"],
            "tags": row["tags"],
        }
        for row in snapshot.telemetry
    )
