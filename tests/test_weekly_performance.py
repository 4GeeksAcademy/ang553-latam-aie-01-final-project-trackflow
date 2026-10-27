"""Deterministic synthetic fixtures for weekly performance transforms.

All event rows below are fabricated test data, not TrackFlow production records.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from pipelines.weekly_performance import (
    INBOUND_ORDER_CREATED,
    INVENTORY_DISCREPANCY_DETECTED,
    OUTBOUND_ORDER_CREATED,
    POSTGRES_INTEGER_MAX,
    STOCK_THRESHOLD_TRIGGERED,
    _postgres_integer_overflows,
    MovementWeekEvidence,
    StockExitEvidence,
    StockExitRecord,
    iso_week_bounds,
    iso_week_start,
    transform_weekly_performance,
)

CLIENT_A = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
CLIENT_B_HISTORICAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def event(
    suffix: int,
    event_type: str = INBOUND_ORDER_CREATED,
    *,
    timestamp: str = "2026-10-20T12:00:00Z",
    warehouse: str = "los_angeles",
    client_id: str = CLIENT_A,
    quantity=2,
    movement_id: str | None = None,
    product_id: str = "42",
    service: str = "api",
    **extra_tags,
):
    """Construct a deterministic synthetic telemetry row."""
    tags = {"warehouse": warehouse, "client_id": client_id, "product_id": product_id}
    if event_type in {INBOUND_ORDER_CREATED, OUTBOUND_ORDER_CREATED}:
        tags.update({"movement_id": movement_id or f"move-{suffix}", "quantity": quantity})
    elif event_type in {STOCK_THRESHOLD_TRIGGERED, INVENTORY_DISCREPANCY_DETECTED}:
        tags["quantity"] = quantity
    tags.update(extra_tags)
    return {
        "id": str(UUID(int=suffix)),
        "timestamp": timestamp,
        "event_type": event_type,
        "service": service,
        "tags": tags,
    }


def transform(rows, *, exits=None, weeks=None, lookup_complete=True):
    return transform_weekly_performance(
        rows,
        stockexit_evidence=StockExitEvidence(exits or {}, lookup_complete),
        movement_week_evidence=weeks,
    )


@pytest.mark.parametrize(
    "counter_name",
    [
        "inbound_units_count",
        "outbound_orders_count",
        "stockout_events_count",
        "discrepancy_events_count",
    ],
)
def test_postgres_integer_counter_validator_accepts_limit_and_rejects_overflow(counter_name):
    assert _postgres_integer_overflows({counter_name: POSTGRES_INTEGER_MAX}) == ()
    assert _postgres_integer_overflows({counter_name: POSTGRES_INTEGER_MAX + 1}) == (counter_name,)


def test_inbound_quantity_at_postgres_integer_limit_is_accepted():
    row = event(1, quantity=POSTGRES_INTEGER_MAX)
    result = transform([row])
    assert result.aggregates[0].inbound_units_count == POSTGRES_INTEGER_MAX
    assert not any(issue.code == "aggregate_integer_overflow" for issue in result.quality.issues)


def test_individual_inbound_quantity_over_limit_blocks_weekly_result():
    row = event(1, quantity=POSTGRES_INTEGER_MAX + 1)
    result = transform([row])
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (row["id"],)
    issue = next(issue for issue in result.quality.issues if issue.code == "aggregate_integer_overflow")
    assert "inbound_units_count" in issue.message
    assert issue.event_ids == (row["id"],)


def test_inbound_sum_over_limit_blocks_even_other_valid_grains():
    rows = [
        event(1, quantity=POSTGRES_INTEGER_MAX),
        event(2, quantity=1),
        event(3, client_id=CLIENT_B_HISTORICAL, quantity=4),
    ]
    result = transform(rows)
    assert result.aggregates == ()
    issue = next(issue for issue in result.quality.issues if issue.code == "aggregate_integer_overflow")
    assert "inbound_units_count" in issue.message
    assert f"client_id={CLIENT_A}" in issue.message
    assert set(issue.event_ids) == {rows[0]["id"], rows[1]["id"]}
    assert set(result.quality.rejected_event_ids) == {rows[0]["id"], rows[1]["id"]}


def test_overflow_diagnostics_are_deterministic_for_reordered_input():
    rows = [
        event(1, quantity=POSTGRES_INTEGER_MAX),
        event(2, quantity=1),
        event(3, client_id=CLIENT_B_HISTORICAL, quantity=4),
    ]
    assert transform(rows) == transform(list(reversed(rows)))


def test_aggregates_multiple_inbound_events_by_units():
    result = transform([event(1, quantity=5), event(2, quantity=7)])
    assert len(result.aggregates) == 1
    aggregate = result.aggregates[0]
    assert aggregate.inbound_units_count == 12
    assert aggregate.outbound_orders_count == 0


@pytest.mark.parametrize("quantity", [-1, 0, True, False, 1.25, None, "2"])
def test_rejects_invalid_inbound_quantities(quantity):
    row = event(1, quantity=quantity)
    result = transform([row])
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (row["id"],)
    assert any("quantity" in issue.message for issue in result.quality.issues)


@pytest.mark.parametrize(
    ("event_type", "quantity", "accepted"),
    [
        (OUTBOUND_ORDER_CREATED, 0, False),
        (OUTBOUND_ORDER_CREATED, 2, True),
        (STOCK_THRESHOLD_TRIGGERED, 0, True),
        (STOCK_THRESHOLD_TRIGGERED, -1, False),
        (INVENTORY_DISCREPANCY_DETECTED, 0, False),
        (INVENTORY_DISCREPANCY_DETECTED, 2, True),
    ],
)
def test_quantity_rules_follow_each_event_schema(event_type, quantity, accepted):
    row = event(1, event_type, quantity=quantity)
    exits = (
        {row["tags"]["movement_id"]: StockExitRecord(row["tags"]["movement_id"], "dispatch", "los_angeles", CLIENT_A, "42", quantity)}
        if event_type == OUTBOUND_ORDER_CREATED and accepted
        else None
    )
    result = transform([row], exits=exits)
    if accepted:
        assert result.quality.accepted_event_ids == (row["id"],)
        assert result.quality.rejected_event_ids == ()
    else:
        assert result.quality.accepted_event_ids == ()
        assert result.quality.rejected_event_ids == (row["id"],)


@pytest.mark.parametrize(
    "event_type", [STOCK_THRESHOLD_TRIGGERED, INVENTORY_DISCREPANCY_DETECTED]
)
def test_non_movement_event_quantity_is_required(event_type):
    row = event(1, event_type)
    del row["tags"]["quantity"]
    result = transform([row])
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (row["id"],)
    assert "quantity" in result.quality.issues[0].message


def test_separates_clients_and_accepts_historical_client_not_in_current_registry():
    result = transform(
        [event(1, client_id=CLIENT_A), event(2, client_id=CLIENT_B_HISTORICAL, quantity=4)]
    )
    assert [item.client_id for item in result.aggregates] == [CLIENT_A, CLIENT_B_HISTORICAL]
    assert [item.inbound_units_count for item in result.aggregates] == [2, 4]


def test_accepts_non_uuid_historical_client_id_per_domain_contract():
    legacy_id = "legacy-client:warehouse-7"
    result = transform([event(1, client_id=legacy_id)])
    assert result.aggregates[0].client_id == legacy_id
    assert result.quality.accepted_event_ids == (event(1, client_id=legacy_id)["id"],)


def test_separates_warehouses_and_normalizes_documented_aliases():
    result = transform(
        [event(1, warehouse="LA"), event(2, warehouse="Zaragoza"), event(3, warehouse="ZGZ")]
    )
    assert [(item.warehouse, item.inbound_units_count) for item in result.aggregates] == [
        ("los_angeles", 2),
        ("zaragoza", 4),
    ]


def test_unknown_warehouse_is_rejected_not_coerced():
    row = event(1, warehouse="Los Angele")
    result = transform([row])
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (row["id"],)


def test_week_iso_utc_with_offset_and_sunday_monday_boundary():
    assert iso_week_start("2026-10-19T00:30:00+02:00") == date(2026, 10, 12)
    assert iso_week_start("2026-10-18T23:30:00-07:00") == date(2026, 10, 19)


def test_exact_monday_utc_boundary_uses_new_week():
    assert iso_week_start("2026-10-19T00:00:00Z") == date(2026, 10, 19)
    start, next_start = iso_week_bounds(date(2026, 10, 19))
    assert start == datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert next_start == datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_iso_year_transition_assigns_monday_and_iso_week_year_correctly():
    assert iso_week_start("2021-01-01T12:00:00Z") == date(2020, 12, 28)
    assert iso_week_start("2021-01-04T00:00:00Z") == date(2021, 1, 4)


def test_consistent_inbound_duplicates_contribute_once_and_report_both_layers():
    first = event(1, quantity=5, movement_id="in-1")
    duplicate_persisted_row = dict(first)
    duplicate_movement = event(2, quantity=5, movement_id="in-1")
    result = transform([first, duplicate_persisted_row, duplicate_movement])
    assert result.aggregates[0].inbound_units_count == 5
    assert result.quality.duplicate_event_ids_consolidated == (first["id"],)
    assert result.quality.duplicate_movement_keys_consolidated == ((INBOUND_ORDER_CREATED, "in-1"),)
    assert result.quality.duplicate_movement_event_ids_consolidated == (duplicate_movement["id"],)


def test_consistent_outbound_duplicates_count_one_dispatch():
    first = event(1, OUTBOUND_ORDER_CREATED, movement_id="out-1", quantity=9)
    duplicate = event(2, OUTBOUND_ORDER_CREATED, movement_id="out-1", quantity=9)
    exits = {"out-1": StockExitRecord("out-1", "dispatch", "los_angeles", CLIENT_A, "42")}
    result = transform([first, duplicate], exits=exits)
    assert result.aggregates[0].outbound_orders_count == 1
    assert result.quality.duplicate_movement_keys_consolidated == ((OUTBOUND_ORDER_CREATED, "out-1"),)
    assert result.quality.duplicate_movement_event_ids_consolidated == (duplicate["id"],)


def test_movement_id_conflicts_on_quantity_and_identity_are_not_arbitrated():
    first = event(1, movement_id="conflict", quantity=3)
    second = event(2, movement_id="conflict", quantity=4, client_id=CLIENT_B_HISTORICAL)
    result = transform([first, second])
    assert result.aggregates == ()
    assert result.quality.conflict_movement_keys == ((INBOUND_ORDER_CREATED, "conflict"),)
    assert set(result.quality.rejected_event_ids) == {first["id"], second["id"]}


def test_complete_cross_week_evidence_detects_conflict():
    first = event(1, timestamp="2026-10-18T23:59:59Z", movement_id="cross", quantity=3)
    second = event(2, timestamp="2026-10-19T00:00:00Z", movement_id="cross", quantity=3)
    key = (INBOUND_ORDER_CREATED, "cross")
    result = transform(
        [first, second],
        weeks={key: MovementWeekEvidence(frozenset({date(2026, 10, 12), date(2026, 10, 19)}), True)},
    )
    assert result.aggregates == ()
    assert result.quality.conflict_movement_keys == (key,)
    assert not result.quality.cross_week_verification_pending


def test_cross_week_deduplication_without_complete_evidence_is_pending_not_guaranteed():
    first = event(1, timestamp="2026-10-18T23:59:59Z", movement_id="cross", quantity=3)
    second = event(2, timestamp="2026-10-19T00:00:00Z", movement_id="cross", quantity=3)
    result = transform([first, second])
    assert result.aggregates == ()
    assert result.quality.conflict_movement_keys == ((INBOUND_ORDER_CREATED, "cross"),)
    assert result.quality.cross_week_verification_pending == ((INBOUND_ORDER_CREATED, "cross"),)


def test_excludes_loss_and_counts_confirmed_dispatch_only():
    loss = event(1, OUTBOUND_ORDER_CREATED, movement_id="loss")
    dispatch = event(2, OUTBOUND_ORDER_CREATED, movement_id="dispatch")
    exits = {
        "loss": StockExitRecord("loss", "loss", "los_angeles", CLIENT_A, "42"),
        "dispatch": StockExitRecord("dispatch", "dispatch", "los_angeles", CLIENT_A, "42"),
    }
    result = transform([loss, dispatch], exits=exits)
    assert len(result.aggregates) == 1
    assert result.aggregates[0].outbound_orders_count == 1
    assert result.quality.accepted_event_ids == tuple(sorted((loss["id"], dispatch["id"])))


def test_unavailable_classification_does_not_presume_dispatch():
    row = event(1, OUTBOUND_ORDER_CREATED, movement_id="not-known")
    result = transform([row], lookup_complete=False)
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (row["id"],)
    assert result.quality.issues[0].code == "outbound_classification_unavailable"


def test_conflicting_stockexit_client_id_is_distinct_from_unverified():
    row = event(1, OUTBOUND_ORDER_CREATED, movement_id="conflicting-client")
    result = transform(
        [row],
        exits={
            "conflicting-client": StockExitRecord(
                "conflicting-client", "dispatch", "los_angeles", CLIENT_B_HISTORICAL, "42", 2
            )
        },
    )

    assert result.aggregates == ()
    assert result.quality.conflict_movement_keys == ((OUTBOUND_ORDER_CREATED, "conflicting-client"),)
    assert result.quality.issues[0].code == "stockexit_identity_conflict"


def test_null_stockexit_client_id_is_unverified_not_a_verified_dispatch():
    row = event(1, OUTBOUND_ORDER_CREATED, movement_id="unverified-client")
    result = transform(
        [row],
        exits={
            "unverified-client": StockExitRecord(
                "unverified-client", "dispatch", "los_angeles", None, "42", 2
            )
        },
    )

    assert result.aggregates == ()
    assert result.quality.accepted_event_ids == ()
    assert result.quality.rejected_event_ids == (row["id"],)
    assert result.quality.conflict_movement_keys == ()
    assert result.quality.issues[0].code == "stockexit_identity_unverified"
    assert result.quality.issues[0].event_ids == (row["id"],)


def test_complete_lookup_missing_movement_and_unknown_exit_type_are_issues():
    absent = event(1, OUTBOUND_ORDER_CREATED, movement_id="absent")
    unknown = event(2, OUTBOUND_ORDER_CREATED, movement_id="unknown")
    result = transform(
        [absent, unknown],
        exits={"unknown": StockExitRecord("unknown", "refund", "los_angeles", CLIENT_A, "42", 2)},
    )
    assert result.aggregates == ()
    assert {issue.code for issue in result.quality.issues} == {
        "stockexit_movement_missing",
        "stockexit_type_unknown",
    }


def test_stockout_and_discrepancy_counts_allow_same_properties_on_distinct_rows():
    stockout_a = event(1, STOCK_THRESHOLD_TRIGGERED)
    stockout_b = event(2, STOCK_THRESHOLD_TRIGGERED)
    discrepancy_a = event(3, INVENTORY_DISCREPANCY_DETECTED)
    discrepancy_b = event(4, INVENTORY_DISCREPANCY_DETECTED)
    result = transform([stockout_a, stockout_b, discrepancy_a, discrepancy_b])
    aggregate = result.aggregates[0]
    assert aggregate.stockout_events_count == 2
    assert aggregate.discrepancy_events_count == 2
    assert len(result.quality.accepted_event_ids) == 4


def test_discrepancy_rate_is_zero_for_zero_outbound_denominator():
    result = transform([event(1, INVENTORY_DISCREPANCY_DETECTED)])
    assert result.aggregates[0].outbound_orders_count == 0
    assert result.aggregates[0].discrepancy_rate == Decimal(0)


def test_discrepancy_rate_is_decimal_ratio_without_business_rounding():
    rows = [event(1, INVENTORY_DISCREPANCY_DETECTED)]
    exits = {}
    for index in range(2, 6):
        movement_id = f"dispatch-{index}"
        rows.append(event(index, OUTBOUND_ORDER_CREATED, movement_id=movement_id))
        exits[movement_id] = StockExitRecord(movement_id, "dispatch", "los_angeles", CLIENT_A, "42", 2)
    result = transform(rows, exits=exits)
    assert result.aggregates[0].discrepancy_rate == Decimal("0.25")
    assert result.aggregates[0].discrepancy_rate.as_tuple().exponent == -2


def test_invalid_timestamps_are_rejected_with_event_identity():
    for timestamp in ("not-a-timestamp", "2026-10-20T12:00:00", None):
        row = event(7, timestamp=timestamp)
        result = transform([row])
        assert result.aggregates == ()
        assert result.quality.rejected_event_ids == (row["id"],)
        assert result.quality.issues[0].event_ids == (row["id"],)


def test_invalid_client_and_provenance_are_rejected():
    invalid_client = event(1, client_id="   ")
    wrong_service = event(2, service="backoffice")
    result = transform([invalid_client, wrong_service])
    assert result.aggregates == ()
    assert set(result.quality.rejected_event_ids) == {invalid_client["id"], wrong_service["id"]}


def test_duplicate_persisted_id_with_different_contents_is_conflict():
    first = event(1)
    conflicting_row = dict(first)
    conflicting_row["tags"] = dict(first["tags"], quantity=99)
    result = transform([first, conflicting_row])
    assert result.aggregates == ()
    assert result.quality.rejected_event_ids == (first["id"],)
    assert result.quality.issues[0].code == "telemetry_row_id_conflict"


def test_result_is_independent_of_input_order():
    rows = [event(1, quantity=1), event(2, quantity=2), event(3, STOCK_THRESHOLD_TRIGGERED)]
    assert transform(rows) == transform(list(reversed(rows)))


def test_repeated_transformation_is_deterministic():
    rows = [event(1, quantity=3), event(2, STOCK_THRESHOLD_TRIGGERED)]
    assert transform(rows) == transform(rows)


def test_output_aggregate_has_exactly_business_fields():
    result = transform([event(1)])
    assert tuple(result.aggregates[0].__dataclass_fields__) == (
        "warehouse",
        "client_id",
        "week_start",
        "inbound_units_count",
        "outbound_orders_count",
        "stockout_events_count",
        "discrepancy_events_count",
        "discrepancy_rate",
    )
    assert len(result.quality.coverage_pending) >= 2
