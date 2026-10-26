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

from data.process.weekly_performance import (
    INBOUND_ORDER_CREATED,
    INVENTORY_DISCREPANCY_DETECTED,
    OFFICIAL_EVENT_TYPES,
    OUTBOUND_ORDER_CREATED,
    MovementWeekEvidence,
    StockExitEvidence,
    StockExitRecord,
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
    WHERE (event_type, tags->>'movement_id') IN ({pairs})
    ORDER BY event_type, tags->>'movement_id', timestamp, id
"""

_STOCKEXIT_SQL = text(
    """
    SELECT e.id AS movement_id,
           e.exit_type,
           e.warehouse,
           e.quantity,
           e.sku_id,
           s.client_id,
           s.id AS product_id
    FROM stockexit AS e
    LEFT JOIN sku AS s ON s.id = e.sku_id
    WHERE e.id IN :movement_ids
    ORDER BY e.id
    """
).bindparams(bindparam("movement_ids", expanding=True))


class WeeklyExtractionError(RuntimeError):
    """Identifiable technical failure; no successful empty result is returned."""

    def __init__(self, phase: str, week_start: date, message: str):
        self.phase = phase
        self.week_start = week_start
        super().__init__(f"Weekly extraction failed during {phase} for {week_start}: {message}")


@dataclass(frozen=True)
class MovementOccurrence:
    """One persisted telemetry occurrence found by the unbounded movement lookup."""

    event_type: str
    movement_id: str
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
    stockexit_requested_ids: tuple[str, ...]
    stockexit_invalid_ids: tuple[str, ...]
    snapshot_isolation: str
    read_only: bool
    snapshot_consistent: bool
    movement_evidence_complete: bool
    stockexit_evidence_complete: bool


@dataclass(frozen=True)
class WeeklyExtractionResult:
    """Raw extracted events and auxiliary evidence for the Phase 1.1 transform."""

    events: tuple[Mapping[str, Any], ...]
    stockexit_evidence: StockExitEvidence
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
    if event_type not in {INBOUND_ORDER_CREATED, OUTBOUND_ORDER_CREATED}:
        return None
    if not isinstance(tags, Mapping):
        return None
    movement_id = tags.get("movement_id")
    if not isinstance(movement_id, str) or not movement_id.strip():
        return None
    return event_type, movement_id


def _stockexit_integer_id(movement_id: str) -> int | None:
    """Map producer-emitted ``str(StockExit.id)`` to its actual integer PK."""
    if not movement_id.isascii() or not movement_id.isdecimal():
        return None
    parsed = int(movement_id)
    if parsed <= 0 or parsed > 2_147_483_647 or str(parsed) != movement_id:
        return None
    return parsed


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


def extract_weekly_inputs(bind: Any, week_start: date) -> WeeklyExtractionResult:
    """Extract one ISO UTC week and complete auxiliary evidence in one snapshot.

    ``bind`` is the application's SQLAlchemy Engine, or a SQLModel Session from
    which its Engine can be obtained. The extractor owns a dedicated connection
    and transaction. PostgreSQL query/connection errors are raised as
    :class:`WeeklyExtractionError` with their phase and original exception chain.
    """
    start, next_start = iso_week_bounds(week_start)
    engine = _engine_from_bind(bind)

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
                                movement_id=key[1],
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
                    parsed_ids: dict[str, int] = {}
                    invalid_outbound_ids: list[str] = []
                    for movement_id in outbound_ids:
                        integer_id = _stockexit_integer_id(movement_id)
                        if integer_id is None:
                            invalid_outbound_ids.append(movement_id)
                        else:
                            parsed_ids[movement_id] = integer_id

                    stockexit_records: dict[str, StockExitRecord] = {}
                    if parsed_ids:
                        stockexit_rows = _execute(
                            connection,
                            _STOCKEXIT_SQL,
                            {"movement_ids": sorted(set(parsed_ids.values()))},
                            phase="stockexit-evidence",
                            week_start=week_start,
                        )
                        ids_by_integer = {value: key for key, value in parsed_ids.items()}
                        for row in stockexit_rows:
                            integer_id = int(row["movement_id"])
                            movement_id = ids_by_integer.get(integer_id)
                            if movement_id is None:
                                continue
                            product_id = row["product_id"]
                            stockexit_records[movement_id] = StockExitRecord(
                                movement_id=movement_id,
                                exit_type=row["exit_type"],
                                warehouse=row["warehouse"],
                                client_id=row["client_id"],
                                product_id=str(product_id) if product_id is not None else None,
                                quantity=row["quantity"],
                            )

                    stockexit_complete = not invalid_outbound_ids
                    stockexit_evidence = StockExitEvidence(
                        records=stockexit_records,
                        lookup_complete=stockexit_complete,
                    )
            except WeeklyExtractionError:
                raise
            except SQLAlchemyError as exc:
                raise WeeklyExtractionError("transaction", week_start, str(exc)) from exc
    except WeeklyExtractionError:
        raise
    except SQLAlchemyError as exc:
        raise WeeklyExtractionError("connection", week_start, str(exc)) from exc

    return WeeklyExtractionResult(
        events=tuple(events),
        stockexit_evidence=stockexit_evidence,
        movement_week_evidence=movement_evidence,
        movement_occurrences=tuple(occurrences),
        metadata=ExtractionMetadata(
            week_start=week_start,
            next_week_start=next_start.date(),
            extracted_at=datetime.now(timezone.utc),
            event_count=len(events),
            movement_occurrence_count=len(occurrences),
            stockexit_requested_ids=tuple(sorted(parsed_ids)),
            stockexit_invalid_ids=tuple(sorted(invalid_outbound_ids)),
            snapshot_isolation="REPEATABLE READ",
            read_only=True,
            snapshot_consistent=True,
            movement_evidence_complete=True,
            stockexit_evidence_complete=stockexit_complete,
        ),
    )
