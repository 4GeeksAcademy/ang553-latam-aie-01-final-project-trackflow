from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from pipelines.pipeline import (
    calculate_discrepancy_metrics_task,
    calculate_inbound_volume_task,
    calculate_outbound_throughput_task,
    calculate_stockout_frequency_task,
    prepare_weekly_events_task,
    transform_week_task,
)
from pipelines.weekly_performance import (
    CommercialOrderEvidence,
    CommercialOrderRecord,
    INVENTORY_DISCREPANCY_DETECTED,
    INBOUND_ORDER_CREATED,
    OUTBOUND_ORDER_CREATED,
    STOCK_THRESHOLD_TRIGGERED,
)

WEEK = date(2026, 1, 5)


def business_event(
    event_type: str,
    *,
    warehouse: str = "los_angeles",
    client_id: str = "client-a",
    quantity: int = 1,
) -> dict[str, object]:
    tags: dict[str, object] = {
        "warehouse": warehouse,
        "client_id": client_id,
        "quantity": quantity,
    }
    if event_type in {INBOUND_ORDER_CREATED, STOCK_THRESHOLD_TRIGGERED}:
        tags["movement_id"] = str(uuid4())
    elif event_type == OUTBOUND_ORDER_CREATED:
        tags["order_id"] = str(uuid4())
    else:
        tags["count_id"] = str(uuid4())
    return {
        "id": str(uuid4()),
        "event_type": event_type,
        "service": "api",
        "timestamp": datetime(2026, 1, 7, 12, tzinfo=timezone.utc),
        "tags": tags,
    }


def extracted(events: list[dict[str, object]], orders=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(week_start=WEEK, event_count=len(events)),
        events=events,
        movement_week_evidence=None,
        commercial_order_evidence=CommercialOrderEvidence(orders or {}, True),
    )


def contribution_value(contribution) -> dict[tuple[str, str], int]:
    return {
        (key.warehouse, key.client_id): value
        for key, value, _event_ids in contribution.values
    }


def test_inbound_task_sums_units_without_mixing_clients_or_warehouses():
    events = [
        business_event(INBOUND_ORDER_CREATED, quantity=4),
        business_event(INBOUND_ORDER_CREATED, quantity=6),
        business_event(
            INBOUND_ORDER_CREATED,
            warehouse="zaragoza",
            client_id="client-b",
            quantity=3,
        ),
    ]
    prepared = prepare_weekly_events_task.fn(extracted(events), WEEK)

    result = calculate_inbound_volume_task.fn(prepared)

    assert contribution_value(result) == {
        ("los_angeles", "client-a"): 10,
        ("zaragoza", "client-b"): 3,
    }


def test_outbound_task_counts_verified_orders_not_dispatch_quantities():
    first = business_event(OUTBOUND_ORDER_CREATED, quantity=99)
    second = business_event(OUTBOUND_ORDER_CREATED, quantity=1)
    orders = {
        row["tags"]["order_id"]: CommercialOrderRecord(
            row["tags"]["order_id"], "los_angeles", "client-a", dispatch_count
        )
        for row, dispatch_count in ((first, 3), (second, 1))
    }
    snapshot = extracted([first, second], orders)
    prepared = prepare_weekly_events_task.fn(snapshot, WEEK)

    result = calculate_outbound_throughput_task.fn(prepared, snapshot)

    assert contribution_value(result) == {("los_angeles", "client-a"): 2}


def test_stockout_and_discrepancy_tasks_are_independent_counters():
    events = [
        business_event(STOCK_THRESHOLD_TRIGGERED, quantity=0),
        business_event(STOCK_THRESHOLD_TRIGGERED, quantity=2),
        business_event(INVENTORY_DISCREPANCY_DETECTED, quantity=5),
    ]
    prepared = prepare_weekly_events_task.fn(extracted(events), WEEK)

    stockouts = calculate_stockout_frequency_task.fn(prepared)
    discrepancies = calculate_discrepancy_metrics_task.fn(prepared)

    assert contribution_value(stockouts) == {("los_angeles", "client-a"): 2}
    assert contribution_value(discrepancies) == {("los_angeles", "client-a"): 1}


def test_invalid_input_is_rejected_before_metric_tasks():
    invalid = business_event(INBOUND_ORDER_CREATED)
    invalid["tags"]["quantity"] = -1

    prepared = prepare_weekly_events_task.fn(extracted([invalid]), WEEK)
    inbound = calculate_inbound_volume_task.fn(prepared)

    assert inbound.values == ()
    assert prepared.rejected_event_ids == (invalid["id"],)
    assert [issue.code for issue in prepared.issues] == ["event_validation_failed"]


def test_composed_discrepancy_rate_has_zero_denominator_semantics():
    discrepancy = business_event(INVENTORY_DISCREPANCY_DETECTED)
    snapshot = extracted([discrepancy])
    prepared = prepare_weekly_events_task.fn(snapshot, WEEK)

    result = transform_week_task.fn(
        snapshot,
        WEEK,
        "dependency-fingerprint",
        "weekly-performance-v1",
        None,
        prepared,
        calculate_inbound_volume_task.fn(prepared),
        calculate_outbound_throughput_task.fn(prepared, snapshot),
        calculate_stockout_frequency_task.fn(prepared),
        calculate_discrepancy_metrics_task.fn(prepared),
    )

    assert result.aggregates[0].outbound_orders_count == 0
    assert result.aggregates[0].discrepancy_events_count == 1
    assert result.aggregates[0].discrepancy_rate == 0
