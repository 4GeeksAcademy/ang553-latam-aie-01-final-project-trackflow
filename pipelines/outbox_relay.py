"""Idempotent relay from the operational inventory outbox to telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from services.api.inventory_outbox import inventory_event_outbox
from services.api.telemetry_storage import (
    map_outbox_event_to_row,
    telemetry_events,
)
from pipelines.database_identity import ensure_distinct_postgresql_databases


@dataclass(frozen=True)
class RelayResult:
    delivered: int
    failed: int
    error_codes: tuple[str, ...] = ()


def _safe_error_code(exc: Exception) -> str:
    """Return a bounded diagnostic containing no exception message or secret."""
    return f"destination_{type(exc).__name__.lower()}"[:120]


class _DestinationEventConflict(RuntimeError):
    pass


_DESTINATION_COLUMNS = (
    telemetry_events.c.id,
    telemetry_events.c.timestamp,
    telemetry_events.c.service,
    telemetry_events.c.event_type,
    telemetry_events.c.level,
    telemetry_events.c.value,
    telemetry_events.c.message,
    telemetry_events.c.tags,
)


def _canonical_destination(row: dict[str, Any]) -> tuple[Any, ...]:
    timestamp = row["timestamp"]
    if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc)
    tags = row.get("tags") or {}
    return (
        row["id"],
        timestamp,
        row["service"],
        row["event_type"],
        row.get("level"),
        row.get("value"),
        row.get("message"),
        tags,
    )


def _persist_destination_event(destination: Any, expected: dict[str, Any]) -> None:
    inserted = destination.execute(
        postgresql_insert(telemetry_events)
        .values(expected)
        .on_conflict_do_nothing(index_elements=[telemetry_events.c.id])
        .returning(*_DESTINATION_COLUMNS)
    ).mappings().first()
    actual = inserted
    if actual is None:
        actual = destination.execute(
            select(*_DESTINATION_COLUMNS).where(
                telemetry_events.c.id == expected["id"]
            )
        ).mappings().first()
    if actual is None or _canonical_destination(dict(actual)) != _canonical_destination(
        expected
    ):
        raise _DestinationEventConflict


def relay_inventory_outbox(
    source_engine: Any,
    reporting_engine: Any,
    *,
    limit: int = 100,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    after_destination_commit: Callable[[dict[str, Any]], None] | None = None,
) -> RelayResult:
    """Relay pending rows with destination idempotency and source acknowledgements.

    Each source row remains locked until its destination transaction commits and
    the source acknowledgement commits.  A crash between those commits rolls
    back only the acknowledgement; retrying is safe because the destination
    insert uses ``ON CONFLICT (id) DO NOTHING``.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("relay limit must be a positive integer")
    ensure_distinct_postgresql_databases(source_engine, reporting_engine)

    delivered = 0
    failed = 0
    error_codes: list[str] = []

    for _ in range(limit):
        with source_engine.begin() as source:
            row = source.execute(
                select(inventory_event_outbox)
                .where(inventory_event_outbox.c.published_at.is_(None))
                .order_by(
                    inventory_event_outbox.c.occurred_at,
                    inventory_event_outbox.c.event_id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).mappings().first()
            if row is None:
                break
            item = dict(row)
            try:
                destination_row = map_outbox_event_to_row(
                    event_id=item["event_id"],
                    occurred_at=item["occurred_at"],
                    event_type=item["event_type"],
                    tags=item["tags"],
                )
                with reporting_engine.begin() as destination:
                    _persist_destination_event(destination, destination_row)
            except _DestinationEventConflict:
                code = "destination_event_conflict"
                source.execute(
                    update(inventory_event_outbox)
                    .where(inventory_event_outbox.c.event_id == item["event_id"])
                    .values(
                        attempt_count=inventory_event_outbox.c.attempt_count + 1,
                        last_attempt_at=clock(),
                        last_error=code,
                    )
                )
                failed += 1
                error_codes.append(code)
                break
            except Exception as exc:
                code = _safe_error_code(exc)
                source.execute(
                    update(inventory_event_outbox)
                    .where(inventory_event_outbox.c.event_id == item["event_id"])
                    .values(
                        attempt_count=inventory_event_outbox.c.attempt_count + 1,
                        last_attempt_at=clock(),
                        last_error=code,
                    )
                )
                failed += 1
                error_codes.append(code)
                break

            if after_destination_commit is not None:
                after_destination_commit(item)

            source.execute(
                update(inventory_event_outbox)
                .where(
                    inventory_event_outbox.c.event_id == item["event_id"],
                    inventory_event_outbox.c.published_at.is_(None),
                )
                .values(
                    published_at=clock(),
                    attempt_count=inventory_event_outbox.c.attempt_count + 1,
                    last_attempt_at=clock(),
                    last_error=None,
                )
            )
            delivered += 1

    return RelayResult(delivered, failed, tuple(error_codes))
