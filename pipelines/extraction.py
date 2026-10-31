"""Read-only extraction for weekly warehouse/client performance inputs.

This module owns SQL access and extraction metadata only. It does not calculate
business KPIs, mutate source tables, or hide database failures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from pipelines.database_identity import ensure_distinct_postgresql_databases

from pipelines.weekly_performance import (
    INBOUND_ORDER_CREATED,
    INVENTORY_DISCREPANCY_DETECTED,
    OFFICIAL_EVENT_TYPES,
    OUTBOUND_ORDER_CREATED,
    MovementWeekEvidence,
    CommercialOrderEvidence,
    CommercialOrderRecord,
    iso_week_bounds,
    iso_week_start,
)


_EVENTS_SQL = text(
    """
    SELECT id, timestamp, service, event_type, tags
    FROM telemetry_events
    WHERE event_type IN :event_types
      AND timestamp >= :week_start
      AND timestamp < :next_week_start
    ORDER BY timestamp, id
    """
).bindparams(bindparam("event_types", expanding=True))

_MOVEMENT_LOOKUP_SQL_TEMPLATE = """
    SELECT id, timestamp, event_type, tags
    FROM telemetry_events
    WHERE (event_type, CASE
        WHEN event_type = 'outbound_order_created' THEN tags->>'order_id'
        WHEN event_type = 'inventory_discrepancy_detected' THEN tags->>'count_id'
        ELSE tags->>'movement_id'
    END) IN ({pairs})
    ORDER BY event_type, timestamp, id
"""

_COMMERCIAL_ORDER_SQL = text(
    """
    SELECT CAST(o.order_id AS text) AS order_id,
           o.warehouse,
           o.client_id,
           COUNT(e.id) AS dispatch_count
    FROM commercial_order AS o
    JOIN stockexit AS e
      ON e.commercial_order_id = o.id
     AND e.exit_type = 'dispatch'
    WHERE CAST(o.order_id AS text) IN :order_ids
    GROUP BY o.order_id, o.warehouse, o.client_id
    ORDER BY o.order_id
    """
).bindparams(bindparam("order_ids", expanding=True))


class WeeklyExtractionError(RuntimeError):
    """Identifiable technical failure; no successful empty result is returned."""

    def __init__(self, phase: str, week_start: date, message: str):
        self.phase = phase
        self.week_start = week_start
        super().__init__(f"Weekly extraction failed during {phase} for {week_start}: {message}")


@dataclass(frozen=True)
class MovementOccurrence:
    """One occurrence found by the unbounded business-identity lookup."""

    event_type: str
    identity: str
    telemetry_id: str
    timestamp: datetime
    week_start: date


@dataclass(frozen=True)
class ExtractionMetadata:
    """Operational metadata describing the completed, coherent read."""

    week_start: date
    next_week_start: date
    extracted_at: datetime
    event_count: int
    movement_occurrence_count: int
    commercial_order_requested_ids: tuple[str, ...]
    commercial_order_invalid_ids: tuple[str, ...]
    snapshot_isolation: str
    read_only: bool
    snapshot_consistent: bool
    movement_evidence_complete: bool
    commercial_order_evidence_complete: bool


@dataclass(frozen=True)
class WeeklyExtractionResult:
    """Raw extracted events and auxiliary evidence for the Phase 1.1 transform."""

    events: tuple[Mapping[str, Any], ...]
    commercial_order_evidence: CommercialOrderEvidence
    movement_week_evidence: Mapping[tuple[str, str], MovementWeekEvidence]
    movement_occurrences: tuple[MovementOccurrence, ...]
    metadata: ExtractionMetadata


def _engine_from_bind(bind: Any) -> Any:
    # Accept an Engine or a SQLModel Session by its public interfaces, while
    # allowing lightweight engine doubles in isolated tests.
    engine = bind.get_bind() if not hasattr(bind, "connect") and hasattr(bind, "get_bind") else bind
    if not hasattr(engine, "connect"):
        raise TypeError("bind must be a SQLAlchemy Engine or SQLModel Session bound to an Engine")
    return engine


def _row_id(value: Any) -> str:
    return str(value) if isinstance(value, UUID) else str(value)


def _movement_key(event_type: Any, tags: Any) -> tuple[str, str] | None:
    if not isinstance(tags, Mapping):
        return None
    identity_field = {
        INBOUND_ORDER_CREATED: "movement_id",
        OUTBOUND_ORDER_CREATED: "order_id",
        "stock_threshold_triggered": "movement_id",
        INVENTORY_DISCREPANCY_DETECTED: "count_id",
    }.get(event_type)
    if identity_field is None:
        return None
    identity = tags.get(identity_field)
    if not isinstance(identity, str) or not identity.strip():
        return None
    return event_type, identity


def _commercial_order_id(order_id: str) -> str | None:
    """Return one canonical UUIDv4 order identity or reject it conservatively."""
    try:
        parsed = UUID(order_id)
    except (AttributeError, ValueError):
        return None
    if parsed.version != 4 or str(parsed) != order_id:
        return None
    return order_id


def _as_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("database returned a non-datetime telemetry timestamp")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database returned a naive telemetry timestamp")
    return value.astimezone(timezone.utc)


def _execute(connection: Any, statement: Any, parameters: Mapping[str, Any], *, phase: str, week_start: date):
    try:
        return connection.execute(statement, dict(parameters)).mappings().all()
    except SQLAlchemyError as exc:
        raise WeeklyExtractionError(phase, week_start, str(exc)) from exc


def _movement_lookup_statement(
    keys: tuple[tuple[str, str], ...],
) -> tuple[Any, dict[str, str]]:
    """Build bound predicates; only generated placeholder names enter SQL text."""
    parameters: dict[str, str] = {}
    pairs: list[str] = []
    for index, (event_type, movement_id) in enumerate(keys):
        event_name = f"event_type_{index}"
        movement_name = f"movement_id_{index}"
        pairs.append(f"(:{event_name}, :{movement_name})")
        parameters[event_name] = event_type
        parameters[movement_name] = movement_id
    return text(_MOVEMENT_LOOKUP_SQL_TEMPLATE.format(pairs=", ".join(pairs))), parameters


def _commercial_order_records(
    connection: Any,
    order_ids: tuple[str, ...],
    *,
    week_start: date,
) -> dict[str, CommercialOrderRecord]:
    if not order_ids:
        return {}
    rows = _execute(
        connection,
        _COMMERCIAL_ORDER_SQL,
        {"order_ids": sorted(set(order_ids))},
        phase="commercial-order-evidence",
        week_start=week_start,
    )
    records: dict[str, CommercialOrderRecord] = {}
    for row in rows:
        order_id = str(row["order_id"])
        records[order_id] = CommercialOrderRecord(
            order_id=order_id,
            warehouse=row["warehouse"],
            client_id=row["client_id"],
            dispatch_count=int(row["dispatch_count"]),
        )
    return records


def extract_weekly_inputs(
    bind: Any,
    week_start: date,
    *,
    inventory_bind: Any | None = None,
) -> WeeklyExtractionResult:
    """Extract one ISO UTC week and complete auxiliary evidence in one snapshot.

    ``bind`` supplies telemetry and movement-occurrence evidence.  An optional
    ``inventory_bind`` supplies authoritative commercial-order/dispatch evidence from the
    operational database.  Omitting it preserves the historical single-engine
    snapshot. PostgreSQL errors are raised as :class:`WeeklyExtractionError`.
    """
    start, next_start = iso_week_bounds(week_start)
    engine = _engine_from_bind(bind)
    inventory_engine = (
        engine if inventory_bind is None else _engine_from_bind(inventory_bind)
    )
    single_snapshot = inventory_engine is engine
    if not single_snapshot:
        ensure_distinct_postgresql_databases(inventory_engine, engine)

    try:
        connection_context = engine.connect()
    except SQLAlchemyError as exc:
        raise WeeklyExtractionError("connect", week_start, str(exc)) from exc

    try:
        with connection_context as connection:
            dialect_name = getattr(getattr(connection, "dialect", None), "name", None)
            if dialect_name != "postgresql":
                raise WeeklyExtractionError(
                    "snapshot",
                    week_start,
                    f"PostgreSQL is required for JSONB extraction and read-only repeatable-read snapshots; got {dialect_name!r}",
                )
            try:
                transaction_context = connection.begin()
                with transaction_context:
                    try:
                        connection.exec_driver_sql(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                    except SQLAlchemyError as exc:
                        raise WeeklyExtractionError("snapshot", week_start, str(exc)) from exc

                    event_rows = _execute(
                        connection,
                        _EVENTS_SQL,
                        {
                            "event_types": sorted(OFFICIAL_EVENT_TYPES),
                            "week_start": start,
                            "next_week_start": next_start,
                        },
                        phase="weekly-events",
                        week_start=week_start,
                    )

                    events: list[Mapping[str, Any]] = []
                    movement_keys: set[tuple[str, str]] = set()
                    for row in event_rows:
                        event = {
                            "id": _row_id(row["id"]),
                            "timestamp": row["timestamp"],
                            "service": row["service"],
                            "event_type": row["event_type"],
                            "tags": row["tags"],
                        }
                        events.append(event)
                        key = _movement_key(event["event_type"], event["tags"])
                        if key is not None:
                            movement_keys.add(key)

                    sorted_movement_keys = tuple(sorted(movement_keys))
                    occurrences: list[MovementOccurrence] = []
                    movement_evidence: dict[tuple[str, str], MovementWeekEvidence] = {}
                    if sorted_movement_keys:
                        lookup_statement, lookup_parameters = _movement_lookup_statement(
                            sorted_movement_keys
                        )
                        movement_rows = _execute(
                            connection,
                            lookup_statement,
                            lookup_parameters,
                            phase="movement-week-evidence",
                            week_start=week_start,
                        )
                        weeks_by_key: dict[tuple[str, str], set[date]] = {
                            key: set() for key in sorted_movement_keys
                        }
                        for row in movement_rows:
                            key = _movement_key(row["event_type"], row["tags"])
                            if key not in weeks_by_key:
                                continue
                            try:
                                timestamp = _as_datetime(row["timestamp"])
                            except (TypeError, ValueError) as exc:
                                raise WeeklyExtractionError(
                                    "movement-week-evidence", week_start, str(exc)
                                ) from exc
                            occurrence = MovementOccurrence(
                                event_type=key[0],
                                identity=key[1],
                                telemetry_id=_row_id(row["id"]),
                                timestamp=timestamp,
                                week_start=iso_week_start(timestamp),
                            )
                            occurrences.append(occurrence)
                            weeks_by_key[key].add(occurrence.week_start)
                        movement_evidence = {
                            key: MovementWeekEvidence(frozenset(weeks), True)
                            for key, weeks in weeks_by_key.items()
                        }

                    outbound_ids = sorted(
                        {
                            key[1]
                            for key in movement_keys
                            if key[0] == OUTBOUND_ORDER_CREATED
                        }
                    )
                    parsed_ids: list[str] = []
                    invalid_outbound_ids: list[str] = []
                    for order_id in outbound_ids:
                        parsed_order_id = _commercial_order_id(order_id)
                        if parsed_order_id is None:
                            invalid_outbound_ids.append(order_id)
                        else:
                            parsed_ids.append(parsed_order_id)

                    commercial_order_records = (
                        _commercial_order_records(
                            connection, tuple(parsed_ids), week_start=week_start
                        )
                        if single_snapshot
                        else {}
                    )
            except WeeklyExtractionError:
                raise
            except SQLAlchemyError as exc:
                raise WeeklyExtractionError("transaction", week_start, str(exc)) from exc
    except WeeklyExtractionError:
        raise
    except SQLAlchemyError as exc:
        raise WeeklyExtractionError("connection", week_start, str(exc)) from exc

    if parsed_ids and not single_snapshot:
        try:
            with inventory_engine.connect() as inventory_connection:
                with inventory_connection.begin():
                    inventory_connection.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    commercial_order_records = _commercial_order_records(
                        inventory_connection, tuple(parsed_ids), week_start=week_start
                    )
        except WeeklyExtractionError:
            raise
        except SQLAlchemyError as exc:
            raise WeeklyExtractionError(
                "commercial-order-source", week_start, str(exc)
            ) from exc

    commercial_order_complete = not invalid_outbound_ids
    commercial_order_evidence = CommercialOrderEvidence(
        records=commercial_order_records,
        lookup_complete=commercial_order_complete,
    )

    return WeeklyExtractionResult(
        events=tuple(events),
        commercial_order_evidence=commercial_order_evidence,
        movement_week_evidence=movement_evidence,
        movement_occurrences=tuple(occurrences),
        metadata=ExtractionMetadata(
            week_start=week_start,
            next_week_start=next_start.date(),
            extracted_at=datetime.now(timezone.utc),
            event_count=len(events),
            movement_occurrence_count=len(occurrences),
            commercial_order_requested_ids=tuple(sorted(parsed_ids)),
            commercial_order_invalid_ids=tuple(sorted(invalid_outbound_ids)),
            snapshot_isolation="REPEATABLE READ",
            read_only=True,
            snapshot_consistent=single_snapshot,
            movement_evidence_complete=True,
            commercial_order_evidence_complete=commercial_order_complete,
        ),
    )
