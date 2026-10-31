"""Pure transformations for TrackFlow's weekly warehouse/client KPIs.

The module accepts already-extracted rows and auxiliary stock-exit evidence. It
never opens connections, reads the current client registry, or publishes a
business-certified report. Missing evidence is reported instead of guessed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any
from uuid import UUID

from services.api.telemetry_utils import canonical_telemetry_warehouse

INBOUND_ORDER_CREATED = "inbound_order_created"
OUTBOUND_ORDER_CREATED = "outbound_order_created"
STOCK_THRESHOLD_TRIGGERED = "stock_threshold_triggered"
INVENTORY_DISCREPANCY_DETECTED = "inventory_discrepancy_detected"
OFFICIAL_EVENT_TYPES = frozenset(
    {
        INBOUND_ORDER_CREATED,
        OUTBOUND_ORDER_CREATED,
        STOCK_THRESHOLD_TRIGGERED,
        INVENTORY_DISCREPANCY_DETECTED,
    }
)
EXPECTED_OPERATIONAL_SERVICE = "api"
POSTGRES_INTEGER_MAX = 2_147_483_647
_INTEGER_COUNT_FIELDS = (
    "inbound_units_count",
    "outbound_orders_count",
    "stockout_events_count",
    "discrepancy_events_count",
)


@dataclass(frozen=True, order=True)
class AggregateKey:
    """The unique business grain for a weekly result."""

    warehouse: str
    client_id: str
    week_start: date


@dataclass(frozen=True)
class WeeklyAggregate:
    """One aggregate with exactly the approved business output fields."""

    warehouse: str
    client_id: str
    week_start: date
    inbound_units_count: int
    outbound_orders_count: int
    stockout_events_count: int
    discrepancy_events_count: int
    discrepancy_rate: Decimal


@dataclass(frozen=True)
class QualityIssue:
    """A validation, conflict, or classification issue kept outside aggregates."""

    code: str
    message: str
    event_ids: tuple[str, ...] = ()
    movement_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class MovementWeekEvidence:
    """Completeness-asserted week attribution evidence for one logical movement.

    ``week_starts`` must contain every ISO week found for the movement by the
    auxiliary lookup. ``complete`` is true only when that lookup was complete.
    """

    week_starts: frozenset[date]
    complete: bool


@dataclass(frozen=True)
class CommercialOrderRecord:
    """Authoritative order identity backed by one or more dispatch rows."""

    order_id: str
    warehouse: str
    client_id: str
    dispatch_count: int


@dataclass(frozen=True)
class CommercialOrderEvidence:
    """Explicit commercial-order lookup results and completeness."""

    records: Mapping[str, CommercialOrderRecord]
    lookup_complete: bool


@dataclass(frozen=True)
class QualityReport:
    """Audit classifications that must not be conflated with aggregate values."""

    accepted_event_ids: tuple[str, ...]
    rejected_event_ids: tuple[str, ...]
    duplicate_event_ids_consolidated: tuple[str, ...]
    duplicate_movement_keys_consolidated: tuple[tuple[str, str], ...]
    duplicate_movement_event_ids_consolidated: tuple[str, ...]
    conflict_movement_keys: tuple[tuple[str, str], ...]
    issues: tuple[QualityIssue, ...]
    cross_week_verification_pending: tuple[tuple[str, str], ...]
    coverage_pending: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyTransformationResult:
    """Deterministic result; quality diagnostics remain separate from aggregates."""

    aggregates: tuple[WeeklyAggregate, ...]
    quality: QualityReport


@dataclass(frozen=True)
class _ValidatedEvent:
    row_id: str
    event_type: str
    timestamp: datetime
    week_start: date
    warehouse: str
    client_id: str
    tags: Mapping[str, Any]

    @property
    def movement_key(self) -> tuple[str, str] | None:
        identity_field = {
            INBOUND_ORDER_CREATED: "movement_id",
            OUTBOUND_ORDER_CREATED: "order_id",
            STOCK_THRESHOLD_TRIGGERED: "movement_id",
            INVENTORY_DISCREPANCY_DETECTED: "count_id",
        }.get(self.event_type)
        if identity_field is None:
            return None
        identity = self.tags.get(identity_field)
        return (self.event_type, identity) if isinstance(identity, str) else None


@dataclass(frozen=True)
class PreparedWeeklyEvents:
    """Validated and deduplicated events plus immutable base diagnostics."""

    events: tuple[_ValidatedEvent, ...]
    rejected_event_ids: tuple[str, ...]
    duplicate_event_ids_consolidated: tuple[str, ...]
    duplicate_movement_keys_consolidated: tuple[tuple[str, str], ...]
    duplicate_movement_event_ids_consolidated: tuple[str, ...]
    conflict_movement_keys: tuple[tuple[str, str], ...]
    issues: tuple[QualityIssue, ...]
    cross_week_verification_pending: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MetricContribution:
    """One independently calculated KPI counter at the approved grain."""

    metric_index: int
    values: tuple[tuple[AggregateKey, int, tuple[str, ...]], ...]
    accepted_event_ids: tuple[str, ...] = ()
    rejected_event_ids: tuple[str, ...] = ()
    conflict_movement_keys: tuple[tuple[str, str], ...] = ()
    issues: tuple[QualityIssue, ...] = ()


def _row_id(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            raise ValueError("id must be a UUID") from None
    raise ValueError("id must be a UUID")


def _parse_utc_timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("timestamp is not a valid ISO 8601 datetime") from None
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp is naive or ambiguous; an explicit UTC offset is required")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("timestamp cannot be normalized to UTC") from None


def iso_week_start(timestamp: datetime | str) -> date:
    """Return Monday's UTC date for an aware timestamp or ISO 8601 string."""
    utc_timestamp = _parse_utc_timestamp(timestamp)
    return utc_timestamp.date() - timedelta(days=utc_timestamp.weekday())


def iso_week_bounds(week_start: date) -> tuple[datetime, datetime]:
    """Return the UTC half-open interval for a Monday ``week_start``."""
    if isinstance(week_start, datetime) or not isinstance(week_start, date):
        raise ValueError("week_start must be a date")
    if week_start.weekday() != 0:
        raise ValueError("week_start must be a Monday")
    start = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=7)


def _canonical_warehouse(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("warehouse must be a recognized string")
    if value in {"los_angeles", "zaragoza"}:
        return value
    canonical = canonical_telemetry_warehouse(value)
    if canonical not in {"los_angeles", "zaragoza"}:
        raise ValueError("warehouse is not a documented alias or canonical value")
    return canonical


def _valid_client_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("client_id must be a non-empty string")
    # The telemetry schema and pipeline design require a non-empty domain ID,
    # not a particular UUID version. Preserve historical identifiers verbatim.
    return value


def _integer_quantity(value: Any, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"quantity must be a {qualifier} integer (not boolean)")
    return value


def _postgres_integer_overflows(counts: Mapping[str, Any]) -> tuple[str, ...]:
    """Return count names that cannot be represented by PostgreSQL integer."""
    return tuple(
        name
        for name in _INTEGER_COUNT_FIELDS
        if name in counts
        and (
            isinstance(counts[name], bool)
            or not isinstance(counts[name], int)
            or counts[name] < 0
            or counts[name] > POSTGRES_INTEGER_MAX
        )
    )


def _validate_event(row: Mapping[str, Any]) -> _ValidatedEvent:
    row_id = _row_id(row.get("id"))
    event_type = row.get("event_type")
    if event_type not in OFFICIAL_EVENT_TYPES:
        raise ValueError("event_type is not one of the four official business event types")
    if row.get("service") != EXPECTED_OPERATIONAL_SERVICE:
        raise ValueError("unexpected service provenance; expected api")
    tags = row.get("tags")
    if not isinstance(tags, Mapping):
        raise ValueError("tags must be a JSON object")
    timestamp = _parse_utc_timestamp(row.get("timestamp"))
    warehouse = _canonical_warehouse(tags.get("warehouse"))
    client_id = _valid_client_id(tags.get("client_id"))

    if event_type == INBOUND_ORDER_CREATED:
        movement_id = tags.get("movement_id")
        if not isinstance(movement_id, str) or not movement_id.strip():
            raise ValueError("movement_id must be a non-empty string for inbound movements")
        _integer_quantity(tags.get("quantity"))
    elif event_type == OUTBOUND_ORDER_CREATED:
        order_id = tags.get("order_id")
        try:
            parsed_order_id = UUID(order_id) if isinstance(order_id, str) else None
        except ValueError:
            parsed_order_id = None
        if parsed_order_id is None or parsed_order_id.version != 4:
            raise ValueError("order_id must be a UUID v4 for outbound orders")
    elif event_type == STOCK_THRESHOLD_TRIGGERED:
        movement_id = tags.get("movement_id")
        if not isinstance(movement_id, str) or not movement_id.strip():
            raise ValueError("movement_id must be a non-empty string for threshold events")
        # The event schema defines quantity as nonnegative for threshold events.
        _integer_quantity(tags.get("quantity"), allow_zero=True)
    elif event_type == INVENTORY_DISCREPANCY_DETECTED:
        count_id = tags.get("count_id")
        try:
            parsed_count_id = UUID(count_id) if isinstance(count_id, str) else None
        except ValueError:
            parsed_count_id = None
        if parsed_count_id is None or parsed_count_id.version != 4:
            raise ValueError("count_id must be a UUID v4 for discrepancy events")
        # The discrepancy event schema defines quantity as positive.
        _integer_quantity(tags.get("quantity"))
    product_id = tags.get("product_id")
    if product_id is not None and (
        not isinstance(product_id, (str, int))
        or isinstance(product_id, bool)
        or not str(product_id).strip()
    ):
        raise ValueError("product_id must be a non-empty string or integer when present")

    return _ValidatedEvent(
        row_id=row_id,
        event_type=event_type,
        timestamp=timestamp,
        week_start=timestamp.date() - timedelta(days=timestamp.weekday()),
        warehouse=warehouse,
        client_id=client_id,
        tags=tags,
    )


def _stable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _stable_value(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_stable_value(item) for item in value)
    return value


def _same_persisted_row(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = ("timestamp", "event_type", "service", "tags")
    try:
        left_timestamp = _parse_utc_timestamp(left.get("timestamp"))
        right_timestamp = _parse_utc_timestamp(right.get("timestamp"))
    except ValueError:
        left_timestamp = left.get("timestamp")
        right_timestamp = right.get("timestamp")
    return all(
        (left_timestamp == right_timestamp if field == "timestamp" else _stable_value(left.get(field)) == _stable_value(right.get(field)))
        for field in fields
    )


def _movement_signature(event: _ValidatedEvent) -> tuple[Any, ...]:
    return (
        event.warehouse,
        event.client_id,
        _stable_value(event.tags),
        event.week_start,
    )


def _evidence_for_key(
    evidence: Mapping[tuple[str, str], MovementWeekEvidence] | None,
    key: tuple[str, str],
    observed_weeks: frozenset[date],
) -> tuple[bool, bool]:
    """Return (verification pending, cross-week conflict) for a movement key."""
    item = evidence.get(key) if evidence is not None else None
    if item is None or not item.complete:
        return True, False
    if len(item.week_starts) > 1 or item.week_starts != observed_weeks:
        return False, True
    return False, False


def prepare_weekly_events(
    events: Sequence[Mapping[str, Any]],
    *,
    movement_week_evidence: Mapping[tuple[str, str], MovementWeekEvidence] | None = None,
) -> PreparedWeeklyEvents:
    """Validate and deduplicate rows once before independent KPI calculations."""
    issues: list[QualityIssue] = []
    rejected: set[str] = set()
    duplicate_event_ids: set[str] = set()
    duplicate_movement_keys: set[tuple[str, str]] = set()
    duplicate_movement_event_ids: set[str] = set()
    conflict_movement_keys: set[tuple[str, str]] = set()
    pending_cross_week: set[tuple[str, str]] = set()

    by_row_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in events:
        try:
            by_row_id[_row_id(row.get("id"))].append(row)
        except (AttributeError, ValueError) as exc:
            issues.append(QualityIssue("invalid_row_id", str(exc)))

    unique_rows: list[Mapping[str, Any]] = []
    for row_id in sorted(by_row_id):
        rows = by_row_id[row_id]
        if all(_same_persisted_row(rows[0], row) for row in rows[1:]):
            unique_rows.append(rows[0])
            if len(rows) > 1:
                duplicate_event_ids.add(row_id)
        else:
            rejected.add(row_id)
            issues.append(
                QualityIssue(
                    "telemetry_row_id_conflict",
                    "Repeated telemetry_events.id values have different row contents.",
                    (row_id,),
                )
            )

    validated: list[_ValidatedEvent] = []
    for row in unique_rows:
        try:
            event = _validate_event(row)
        except (AttributeError, TypeError, ValueError) as exc:
            try:
                invalid_row_id = _row_id(row.get("id"))
            except (AttributeError, ValueError):
                invalid_row_id = None
            if invalid_row_id is not None:
                rejected.add(invalid_row_id)
            issues.append(
                QualityIssue(
                    "event_validation_failed",
                    str(exc),
                    (invalid_row_id,) if invalid_row_id is not None else (),
                )
            )
            continue
        validated.append(event)

    # Movement duplicates are compared across all relevant dimensions, including week.
    grouped_movements: dict[tuple[str, str], list[_ValidatedEvent]] = defaultdict(list)
    non_movement_events: list[_ValidatedEvent] = []
    for event in validated:
        key = event.movement_key
        if key is None:
            non_movement_events.append(event)
        else:
            grouped_movements[key].append(event)

    consolidated: list[_ValidatedEvent] = list(non_movement_events)
    for key in sorted(grouped_movements):
        group = sorted(grouped_movements[key], key=lambda event: event.row_id)
        signatures = {_movement_signature(event) for event in group}
        observed_weeks = frozenset(event.week_start for event in group)
        verification_pending, cross_week_conflict = _evidence_for_key(
            movement_week_evidence, key, observed_weeks
        )
        if verification_pending:
            pending_cross_week.add(key)
        if len(signatures) > 1 or cross_week_conflict:
            conflict_movement_keys.add(key)
            rejected.update(event.row_id for event in group)
            reason = (
                "Complete movement evidence attributes this movement to conflicting ISO weeks."
                if cross_week_conflict
                else "Repeated business identities conflict on event tags, warehouse, client, or week."
            )
            issues.append(
                QualityIssue("movement_conflict", reason, tuple(event.row_id for event in group), key)
            )
            continue
        consolidated.append(group[0])
        if len(group) > 1:
            duplicate_movement_keys.add(key)
            duplicate_movement_event_ids.update(event.row_id for event in group[1:])

    return PreparedWeeklyEvents(
        events=tuple(
            sorted(
                consolidated,
                key=lambda item: (
                    item.week_start,
                    item.warehouse,
                    item.client_id,
                    item.row_id,
                ),
            )
        ),
        rejected_event_ids=tuple(sorted(rejected)),
        duplicate_event_ids_consolidated=tuple(sorted(duplicate_event_ids)),
        duplicate_movement_keys_consolidated=tuple(sorted(duplicate_movement_keys)),
        duplicate_movement_event_ids_consolidated=tuple(
            sorted(duplicate_movement_event_ids)
        ),
        conflict_movement_keys=tuple(sorted(conflict_movement_keys)),
        issues=tuple(issues),
        cross_week_verification_pending=tuple(sorted(pending_cross_week)),
    )


def _group_metric_values(
    metric_index: int,
    rows: Sequence[tuple[AggregateKey, int, str]],
    *,
    accepted_event_ids: Sequence[str] = (),
    rejected_event_ids: Sequence[str] = (),
    conflict_movement_keys: Sequence[tuple[str, str]] = (),
    issues: Sequence[QualityIssue] = (),
) -> MetricContribution:
    values: dict[AggregateKey, int] = defaultdict(int)
    contributors: dict[AggregateKey, set[str]] = defaultdict(set)
    for key, value, event_id in rows:
        values[key] += value
        contributors[key].add(event_id)
    return MetricContribution(
        metric_index=metric_index,
        values=tuple(
            (key, values[key], tuple(sorted(contributors[key]))) for key in sorted(values)
        ),
        accepted_event_ids=tuple(sorted(accepted_event_ids)),
        rejected_event_ids=tuple(sorted(rejected_event_ids)),
        conflict_movement_keys=tuple(sorted(conflict_movement_keys)),
        issues=tuple(issues),
    )


def calculate_inbound_volume(prepared: PreparedWeeklyEvents) -> MetricContribution:
    """Calculate inbound units only, using already validated event quantities."""
    rows = []
    accepted = []
    for event in prepared.events:
        if event.event_type != INBOUND_ORDER_CREATED:
            continue
        key = AggregateKey(event.warehouse, event.client_id, event.week_start)
        rows.append((key, _integer_quantity(event.tags.get("quantity")), event.row_id))
        accepted.append(event.row_id)
    return _group_metric_values(0, rows, accepted_event_ids=accepted)


def calculate_outbound_throughput(
    prepared: PreparedWeeklyEvents,
    commercial_order_evidence: CommercialOrderEvidence,
) -> MetricContribution:
    """Count only outbound orders verified by authoritative dispatch evidence."""
    rows = []
    accepted: list[str] = []
    rejected: list[str] = []
    conflicts: list[tuple[str, str]] = []
    issues: list[QualityIssue] = []
    for event in prepared.events:
        if event.event_type != OUTBOUND_ORDER_CREATED:
            continue
        order_id = event.tags["order_id"]
        record = commercial_order_evidence.records.get(order_id)
        if record is None:
            if commercial_order_evidence.lookup_complete:
                issues.append(
                    QualityIssue(
                        "commercial_order_missing",
                        "No matching commercial order with a dispatch was found by the complete auxiliary lookup.",
                        (event.row_id,),
                        event.movement_key,
                    )
                )
            else:
                issues.append(
                    QualityIssue(
                        "outbound_order_verification_unavailable",
                        "Commercial-order verification was unavailable; an order was not presumed.",
                        (event.row_id,),
                        event.movement_key,
                    )
                )
            rejected.append(event.row_id)
            continue
        identity_conflicts = []
        if record.order_id != order_id:
            identity_conflicts.append("order_id")
        try:
            record_warehouse = _canonical_warehouse(record.warehouse)
        except ValueError:
            record_warehouse = None
            identity_conflicts.append("warehouse (invalid in order evidence)")
        if record_warehouse is not None and record_warehouse != event.warehouse:
            identity_conflicts.append("warehouse")
        if record.client_id != event.client_id:
            identity_conflicts.append("client_id")
        if isinstance(record.dispatch_count, bool) or record.dispatch_count < 1:
            identity_conflicts.append("dispatch_count")
        if identity_conflicts:
            issues.append(
                QualityIssue(
                    "commercial_order_identity_conflict",
                    "Commercial-order identity contradicts telemetry: " + ", ".join(identity_conflicts),
                    (event.row_id,),
                    event.movement_key,
                )
            )
            rejected.append(event.row_id)
            if event.movement_key is not None:
                conflicts.append(event.movement_key)
            continue
        key = AggregateKey(event.warehouse, event.client_id, event.week_start)
        rows.append((key, 1, event.row_id))
        accepted.append(event.row_id)
    return _group_metric_values(
        1,
        rows,
        accepted_event_ids=accepted,
        rejected_event_ids=rejected,
        conflict_movement_keys=conflicts,
        issues=issues,
    )


def calculate_stockout_frequency(prepared: PreparedWeeklyEvents) -> MetricContribution:
    """Count threshold-crossing events at warehouse/client/week grain."""
    rows = []
    accepted = []
    for event in prepared.events:
        if event.event_type != STOCK_THRESHOLD_TRIGGERED:
            continue
        key = AggregateKey(event.warehouse, event.client_id, event.week_start)
        rows.append((key, 1, event.row_id))
        accepted.append(event.row_id)
    return _group_metric_values(2, rows, accepted_event_ids=accepted)


def calculate_discrepancy_events(prepared: PreparedWeeklyEvents) -> MetricContribution:
    """Count discrepancy events; the final rate is derived during composition."""
    rows = []
    accepted = []
    for event in prepared.events:
        if event.event_type != INVENTORY_DISCREPANCY_DETECTED:
            continue
        key = AggregateKey(event.warehouse, event.client_id, event.week_start)
        rows.append((key, 1, event.row_id))
        accepted.append(event.row_id)
    return _group_metric_values(3, rows, accepted_event_ids=accepted)


def compose_weekly_performance(
    prepared: PreparedWeeklyEvents,
    contributions: Sequence[MetricContribution],
) -> WeeklyTransformationResult:
    """Compose independently calculated KPI counters and shared diagnostics."""
    if tuple(item.metric_index for item in contributions) != (0, 1, 2, 3):
        raise ValueError("all four KPI contributions are required in canonical order")

    accepted: set[str] = set()
    rejected = set(prepared.rejected_event_ids)
    conflict_movement_keys = set(prepared.conflict_movement_keys)
    issues = list(prepared.issues)
    totals: dict[AggregateKey, list[int]] = {}
    counter_event_ids: dict[AggregateKey, list[set[str]]] = {}
    for contribution in contributions:
        accepted.update(contribution.accepted_event_ids)
        rejected.update(contribution.rejected_event_ids)
        conflict_movement_keys.update(contribution.conflict_movement_keys)
        issues.extend(contribution.issues)
        for key, value, event_ids in contribution.values:
            counters = totals.setdefault(key, [0, 0, 0, 0])
            contributors = counter_event_ids.setdefault(key, [set(), set(), set(), set()])
            counters[contribution.metric_index] = value
            contributors[contribution.metric_index].update(event_ids)

    aggregates: list[WeeklyAggregate] = []
    integer_overflow = False
    for key in sorted(totals):
        inbound, outbound, stockouts, discrepancies = totals[key]
        count_values = dict(zip(_INTEGER_COUNT_FIELDS, totals[key]))
        overflowing_fields = _postgres_integer_overflows(count_values)
        for field_name in overflowing_fields:
            integer_overflow = True
            field_index = _INTEGER_COUNT_FIELDS.index(field_name)
            event_ids = tuple(sorted(counter_event_ids[key][field_index]))
            rejected.update(event_ids)
            accepted.difference_update(event_ids)
            issues.append(
                QualityIssue(
                    "aggregate_integer_overflow",
                    f"{field_name} exceeds PostgreSQL integer range for "
                    f"warehouse={key.warehouse}, client_id={key.client_id}, week_start={key.week_start}; "
                    "the complete weekly result is blocked.",
                    event_ids,
                )
            )
        with localcontext() as context:
            context.prec = 28
            context.rounding = ROUND_HALF_EVEN
            discrepancy_rate = (
                Decimal(0)
                if outbound == 0
                else Decimal(discrepancies) / Decimal(outbound)
            )
        if not overflowing_fields:
            aggregates.append(
                WeeklyAggregate(
                    warehouse=key.warehouse,
                    client_id=key.client_id,
                    week_start=key.week_start,
                    inbound_units_count=inbound,
                    outbound_orders_count=outbound,
                    stockout_events_count=stockouts,
                    discrepancy_events_count=discrepancies,
                    discrepancy_rate=discrepancy_rate,
                )
            )

    # A partial result could be mistaken for a complete weekly publication.
    if integer_overflow:
        aggregates.clear()

    coverage_pending = (
        "Coverage is unverified: stock_threshold_triggered has no confirmed producer/capture guarantee.",
        "Coverage is unverified: inventory_discrepancy_detected has no confirmed producer/capture guarantee.",
        "An outbound order lacks verified commercial-order identity and dispatch evidence.",
    )
    quality = QualityReport(
        accepted_event_ids=tuple(sorted(accepted)),
        rejected_event_ids=tuple(sorted(rejected)),
        duplicate_event_ids_consolidated=prepared.duplicate_event_ids_consolidated,
        duplicate_movement_keys_consolidated=prepared.duplicate_movement_keys_consolidated,
        duplicate_movement_event_ids_consolidated=prepared.duplicate_movement_event_ids_consolidated,
        conflict_movement_keys=tuple(sorted(conflict_movement_keys)),
        issues=tuple(sorted(issues, key=lambda issue: (issue.code, issue.event_ids, issue.message))),
        cross_week_verification_pending=prepared.cross_week_verification_pending,
        coverage_pending=coverage_pending,
    )
    return WeeklyTransformationResult(tuple(aggregates), quality)


def transform_weekly_performance(
    events: Sequence[Mapping[str, Any]],
    *,
    commercial_order_evidence: CommercialOrderEvidence,
    movement_week_evidence: Mapping[tuple[str, str], MovementWeekEvidence] | None = None,
) -> WeeklyTransformationResult:
    """Validate once, calculate four KPI counters independently, then compose."""
    prepared = prepare_weekly_events(
        events,
        movement_week_evidence=movement_week_evidence,
    )
    return compose_weekly_performance(
        prepared,
        (
            calculate_inbound_volume(prepared),
            calculate_outbound_throughput(prepared, commercial_order_evidence),
            calculate_stockout_frequency(prepared),
            calculate_discrepancy_events(prepared),
        ),
    )
