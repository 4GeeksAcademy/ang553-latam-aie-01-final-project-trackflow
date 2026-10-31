"""
TrackFlow Inventory Service — reusable stock calculation and movement logic.

Provides domain-level queries for computing current stock on the fly
without persisting any stock values, and functions for registering
stock entries and exits with safety checks.

Movement operations enforce:

    * SKU existence
    * Warehouse match between SKU and movement
    * Stock sufficiency before StockExit (no negative stock)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, func, select

from services.api.inventory_schemas import (
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    InventoryPhysicalCountCreate,
    StockEntryCreate,
    StockExitCreate,
)
from services.api.cache import invalidate_inventory_cache
from services.api.telemetry_capture import capture_telemetry_events
from services.api.inventory_outbox import enqueue_inventory_event
from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_storage import (
    persist_backend_telemetry_best_effort,
)
from services.api.telemetry_utils import canonical_telemetry_warehouse

logger = logging.getLogger(__name__)


class InventoryDataIntegrityError(RuntimeError):
    """Raised when an inventory movement references a missing SKU."""

    def __init__(self, missing_sku_ids: set[int]) -> None:
        self.missing_sku_ids = frozenset(missing_sku_ids)
        super().__init__(
            "Inventory movements reference missing SKUs: "
            f"{sorted(self.missing_sku_ids)}"
        )


def _lock_idempotency_keys(session: Session, values: list[UUID]) -> None:
    """Serialize UUID commands in PostgreSQL; SQLite unit tests stay local."""
    bind = session.get_bind()
    if getattr(bind.dialect, "name", None) != "postgresql":
        return
    for value in sorted(set(values), key=str):
        key = int.from_bytes(value.bytes[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _lock_skus(session: Session, sku_ids: set[int]) -> dict[int, object]:
    """Lock SKU rows in stable order to serialize stock-derived writes."""
    from services.api.inventory_models import SKU

    ordered = sorted(sku_ids)
    rows = session.exec(
        select(SKU)
        .where(SKU.id.in_(ordered))
        .order_by(SKU.id)
        .with_for_update()
    ).all()
    return {row.id: row for row in rows}


def _event(
    *,
    event_type: str,
    user_uuid: str,
    request_id: str | None,
    occurred_at: datetime,
    tags: dict,
) -> TelemetryEvent:
    return TelemetryEvent(
        eventId=uuid4(),
        timestamp=occurred_at.isoformat().replace("+00:00", "Z"),
        sessionId=None,
        userId=user_uuid,
        event_type=event_type,
        schemaVersion="1.0",
        requestId=request_id,
        properties=tags,
    )


def _threshold_event(
    session: Session,
    *,
    sku: object,
    source_pk: int,
    before: int,
    after: int,
    user_uuid: str,
    request_id: str | None,
    occurred_at: datetime,
) -> TelemetryEvent | None:
    threshold = sku.minimum_stock_threshold
    if threshold is None or not (before > threshold and after <= threshold):
        return None
    warehouse = canonical_telemetry_warehouse(sku.warehouse)
    if warehouse is None or not sku.client_id:
        raise InventoryDataIntegrityError({sku.id})
    tags = {
        "warehouse": warehouse,
        "client_id": sku.client_id,
        "product_id": str(sku.id),
        "product_category": sku.category,
        "quantity": after,
        "threshold_quantity": threshold,
        "movement_id": str(source_pk),
    }
    event = _event(
        event_type="stock_threshold_triggered",
        user_uuid=user_uuid,
        request_id=request_id,
        occurred_at=occurred_at,
        tags=tags,
    )
    enqueue_inventory_event(
        session,
        event_id=event.eventId,
        occurred_at=occurred_at,
        source_table="stockexit",
        source_pk=source_pk,
        event_type=event.event_type,
        tags=tags,
    )
    return event


def _record_threshold_evaluation(
    session: Session,
    *,
    source_table: str,
    source_pk: int,
    sku: object,
    before: int,
    after: int,
    event: TelemetryEvent | None,
    evaluated_at: datetime,
) -> None:
    from services.api.inventory_models import InventoryThresholdEvaluation

    crossed = event is not None
    session.add(
        InventoryThresholdEvaluation(
            source_table=source_table,
            source_pk=source_pk,
            sku_id=sku.id,
            client_id=sku.client_id,
            warehouse=sku.warehouse,
            before_quantity=before,
            after_quantity=after,
            threshold_quantity=sku.minimum_stock_threshold,
            crossed=crossed,
            event_id=None if event is None else event.eventId,
            evaluated_at=evaluated_at,
        )
    )


def _capture_after_commit(events: list[TelemetryEvent]) -> None:
    if not events:
        return
    try:
        capture_telemetry_events(events)
    except Exception:
        logger.warning("Telemetry capture failed after committed inventory operation")


# ═════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═════════════════════════════════════════════════════════════════════════════


def get_sku_or_none(session: Session, sku_id: int) -> object | None:
    """Return the SKU for *sku_id*, or ``None`` if it does not exist.

    Uses a direct primary-key lookup — no full-table scan.
    """
    from services.api.inventory_models import SKU

    return session.get(SKU, sku_id)


# ═════════════════════════════════════════════════════════════════════════════
# Stock calculation (query only — no side effects)
# ═════════════════════════════════════════════════════════════════════════════


def get_current_stocks(
    session: Session,
) -> dict[tuple[int, str], int]:
    """Compute current stock for all (sku_id, warehouse) pairs in bulk.

    This function performs exactly **two** aggregate queries (entries +
    exits) regardless of the number of SKUs, avoiding N+1 behaviour.

    Returns
    -------
    dict[(int, str), int]
        A mapping from ``(sku_id, warehouse)`` to the current stock level.
        SKUs with no movements are **not** present in the returned dict
        (callers should default to 0).
    """
    from services.api.inventory_models import StockEntry, StockExit

    # ── Aggregated entries ───────────────────────────────────────────────
    entry_rows = session.exec(
        select(StockEntry.sku_id, StockEntry.warehouse, func.sum(StockEntry.quantity))
        .group_by(StockEntry.sku_id, StockEntry.warehouse)
    ).all()

    # ── Aggregated exits ─────────────────────────────────────────────────
    exit_rows = session.exec(
        select(StockExit.sku_id, StockExit.warehouse, func.sum(StockExit.quantity))
        .group_by(StockExit.sku_id, StockExit.warehouse)
    ).all()

    # ── Build entry map ──────────────────────────────────────────────────
    entry_map: dict[tuple[int, str], int] = {}
    for row in entry_rows:
        # row is a Row tuple: (sku_id, warehouse, sum)
        key = (row[0], row[1])
        entry_map[key] = row[2]  # type: ignore[assignment]

    # ── Build exit map ───────────────────────────────────────────────────
    exit_map: dict[tuple[int, str], int] = {}
    for row in exit_rows:
        key = (row[0], row[1])
        exit_map[key] = row[2]  # type: ignore[assignment]

    # ── Combine: entries minus exits ─────────────────────────────────────
    # Iterate over the union of keys from both maps.
    all_keys = set(entry_map.keys()) | set(exit_map.keys())
    result: dict[tuple[int, str], int] = {}
    for key in all_keys:
        entries = entry_map.get(key, 0)
        exits = exit_map.get(key, 0)
        result[key] = entries - exits

    return result


def get_current_stock(
    session: Session,
    sku_id: int,
    warehouse: str,
) -> int:
    """Compute the current stock level for a SKU in a given warehouse.

    The stock is calculated as::

        SUM(StockEntry.quantity) - SUM(StockExit.quantity)

    filtered by ``sku_id`` and ``warehouse``.

    Parameters
    ----------
    session : Session
        An active SQLModel session.
    sku_id : int
        The SKU identifier.
    warehouse : str
        Warehouse code (e.g. ``"LA"``, ``"ZGZ"``).

    Returns
    -------
    int
        Current stock level.  Returns ``0`` when there are no movements.

    Notes
    -----
    - All aggregation is performed in the database — no full-table loads.
    - This function has **no side effects**: no writes, no commits,
      no rollbacks.
    """
    from services.api.inventory_models import StockEntry, StockExit

    # ── SUM of all entries for this SKU + warehouse ───────────────────────
    entry_sum: int | None = session.scalar(
        select(func.sum(StockEntry.quantity)).where(
            StockEntry.sku_id == sku_id,
            StockEntry.warehouse == warehouse,
        )
    )

    # ── SUM of all exits for this SKU + warehouse ─────────────────────────
    exit_sum: int | None = session.scalar(
        select(func.sum(StockExit.quantity)).where(
            StockExit.sku_id == sku_id,
            StockExit.warehouse == warehouse,
        )
    )

    # func.sum returns None (SQL NULL) when no rows match.
    entries = entry_sum if entry_sum is not None else 0
    exits = exit_sum if exit_sum is not None else 0

    return entries - exits


# ═════════════════════════════════════════════════════════════════════════════
# StockEntry — inbound movement
# ═════════════════════════════════════════════════════════════════════════════


def create_stock_entry(
    session: Session,
    data: StockEntryCreate,
    user_uuid: str,
    request_id: str | None = None,
):
    """Register an inbound stock movement (StockEntry).

    Parameters
    ----------
    session : Session
        An active SQLModel session.
    data : StockEntryCreate
        Validated request payload (``sku_id``, ``quantity``, ``reference``,
        ``warehouse``).
    user_uuid : str
        UUID of the authenticated user — set by the backend, **not**
        accepted from the client.

    Returns
    -------
    StockEntry
        The newly created and persisted StockEntry record.

    Raises
    ------
    HTTPException
        * ``404`` — if the SKU does not exist.
        * ``400`` — if ``data.warehouse`` does not match the SKU's warehouse.
    """
    from services.api.inventory_models import StockEntry

    # 1. Check SKU exists
    sku = _lock_skus(session, {data.sku_id}).get(data.sku_id)
    if sku is None:
        raise HTTPException(
            status_code=404,
            detail="SKU not found.",
            headers={"X-TrackFlow-Error-Code": "unknown_product"},
        )

    # 2. Check warehouse matches SKU
    movement_warehouse = data.warehouse.value
    if movement_warehouse != sku.warehouse:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Warehouse mismatch for SKU '{sku.sku}'. "
                f"SKU warehouse: {sku.warehouse}, "
                f"movement warehouse: {movement_warehouse}."
            ),
            headers={"X-TrackFlow-Error-Code": "warehouse_mismatch"},
        )

    before = get_current_stock(session, data.sku_id, movement_warehouse)

    # 3. Build the movement and its authentic event in one transaction.
    occurred_at = datetime.now(timezone.utc)
    entry = StockEntry(
        sku_id=data.sku_id,
        quantity=data.quantity,
        reference=data.reference,
        warehouse=movement_warehouse,
        user_uuid=user_uuid,
        created_at=occurred_at,
    )
    event: TelemetryEvent | None = None
    try:
        session.add(entry)
        session.flush()
        if entry.id is None:
            raise RuntimeError("inbound movement identity was not assigned")
        after = before + entry.quantity

        if sku.client_id is None:
            logger.warning("Outbox skipped for legacy inbound stock creation")
        else:
            warehouse = canonical_telemetry_warehouse(entry.warehouse)
            if warehouse is None:
                logger.warning("Outbox skipped for inbound stock creation")
            else:
                event_id = uuid4()
                tags = {
                    "warehouse": warehouse,
                    "client_id": sku.client_id,
                    "product_id": str(sku.id),
                    "product_category": sku.category,
                    "quantity": entry.quantity,
                    "movement_id": str(entry.id),
                }
                event = TelemetryEvent(
                    eventId=event_id,
                    timestamp=occurred_at.isoformat().replace("+00:00", "Z"),
                    sessionId=None,
                    userId=user_uuid,
                    event_type="inbound_order_created",
                    schemaVersion="1.0",
                    requestId=request_id,
                    properties=tags,
                )
                enqueue_inventory_event(
                    session,
                    event_id=event_id,
                    occurred_at=occurred_at,
                    source_table="stockentry",
                    source_pk=entry.id,
                    event_type=event.event_type,
                    tags=tags,
                )
        _record_threshold_evaluation(
            session,
            source_table="stockentry",
            source_pk=entry.id,
            sku=sku,
            before=before,
            after=after,
            event=None,
            evaluated_at=occurred_at,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(entry)

    if event is not None:
        try:
            capture_telemetry_events([event])
        except Exception:
            logger.warning("Telemetry capture failed for inbound stock creation")

    from services.api.cache import invalidate_inventory_cache

    invalidate_inventory_cache(session)

    return entry


# ═════════════════════════════════════════════════════════════════════════════
# StockExit — outbound movement (with stock sufficiency guard)
# ═════════════════════════════════════════════════════════════════════════════


def create_stock_exit(
    session: Session,
    data: StockExitCreate,
    user_uuid: str,
    request_id: str | None = None,
):
    """Register an outbound stock movement (StockExit).

    Protects against negative stock: refuses the exit when requested
    quantity exceeds currently available stock.

    Parameters
    ----------
    session : Session
        An active SQLModel session.
    data : StockExitCreate
        Validated request payload (``sku_id``, ``quantity``, ``exit_type``,
        ``tracking_number``, ``warehouse``).
    user_uuid : str
        UUID of the authenticated user — set by the backend, **not**
        accepted from the client.

    Returns
    -------
    StockExit
        The newly created and persisted StockExit record.

    Raises
    ------
    HTTPException
        * ``404`` — if the SKU does not exist.
        * ``400`` — if ``data.warehouse`` does not match the SKU's warehouse.
        * ``400`` — if requested quantity exceeds available stock.
    """
    from services.api.inventory_models import StockExit

    # 1. Check SKU exists
    sku = _lock_skus(session, {data.sku_id}).get(data.sku_id)
    if sku is None:
        raise HTTPException(
            status_code=404,
            detail="SKU not found.",
            headers={"X-TrackFlow-Error-Code": "unknown_product"},
        )

    # 2. Check warehouse matches SKU
    movement_warehouse = data.warehouse.value
    if movement_warehouse != sku.warehouse:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Warehouse mismatch for SKU '{sku.sku}'. "
                f"SKU warehouse: {sku.warehouse}, "
                f"movement warehouse: {movement_warehouse}."
            ),
            headers={"X-TrackFlow-Error-Code": "warehouse_mismatch"},
        )

    # 3. Calculate available stock and validate sufficiency
    available = get_current_stock(session, sku_id=sku.id, warehouse=movement_warehouse)
    if data.quantity > available:
        if sku.client_id is None:
            logger.warning("Telemetry skipped for insufficient stock")
        else:
            try:
                warehouse = canonical_telemetry_warehouse(movement_warehouse)
                if warehouse is None:
                    logger.warning("Telemetry skipped for insufficient stock")
                else:
                    event = TelemetryEvent(
                        eventId=uuid4(),
                        timestamp=datetime.now(timezone.utc).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        sessionId=None,
                        userId=user_uuid,
                        event_type="inventory_stock_insufficient",
                        schemaVersion="1.0",
                        requestId=request_id,
                        properties={
                            "warehouse": warehouse,
                            "client_id": sku.client_id,
                            "product_id": str(sku.id),
                            "product_category": sku.category,
                            "requested_quantity": data.quantity,
                            "available_quantity": available,
                        },
                    )
                    capture_telemetry_events([event])
                    persist_backend_telemetry_best_effort([event])
            except Exception:
                logger.warning("Telemetry capture failed for insufficient stock")

        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient stock for SKU '{sku.sku}'. "
                f"Available: {available}, requested: {data.quantity}."
            ),
            headers={"X-TrackFlow-Error-Code": "insufficient_stock"},
        )

    # 4. Persist the legacy movement. The commercial order, not an individual
    # dispatch, owns outbound_order_created. This legacy operation therefore
    # never claims that a standalone StockExit is a commercial order.
    occurred_at = datetime.now(timezone.utc)
    exit_record = StockExit(
        sku_id=data.sku_id,
        quantity=data.quantity,
        exit_type=data.exit_type,
        tracking_number=data.tracking_number,
        warehouse=movement_warehouse,
        user_uuid=user_uuid,
        created_at=occurred_at,
    )
    events: list[TelemetryEvent] = []
    try:
        session.add(exit_record)
        session.flush()
        if exit_record.id is None:
            raise RuntimeError("outbound movement identity was not assigned")
        threshold_event = _threshold_event(
            session,
            sku=sku,
            source_pk=exit_record.id,
            before=available,
            after=available - exit_record.quantity,
            user_uuid=user_uuid,
            request_id=request_id,
            occurred_at=occurred_at,
        )
        if threshold_event is not None:
            events.append(threshold_event)
        _record_threshold_evaluation(
            session,
            source_table="stockexit",
            source_pk=exit_record.id,
            sku=sku,
            before=available,
            after=available - exit_record.quantity,
            event=threshold_event,
            evaluated_at=occurred_at,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(exit_record)
    _capture_after_commit(events)

    from services.api.cache import invalidate_inventory_cache

    invalidate_inventory_cache(session)

    return exit_record


def update_sku_threshold(
    session: Session,
    *,
    sku_id: int,
    minimum_stock_threshold: int | None,
):
    """Configure a threshold without emitting a business event."""
    sku = _lock_skus(session, {sku_id}).get(sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU not found.")
    try:
        sku.minimum_stock_threshold = minimum_stock_threshold
        session.add(sku)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(sku)
    invalidate_inventory_cache(session)
    return sku


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def confirm_inventory_physical_count(
    session: Session,
    data: InventoryPhysicalCountCreate,
    *,
    user_uuid: str,
    request_id: str | None = None,
):
    """Persist one idempotent count and discrepancy outbox event atomically."""
    from services.api.inventory_models import InventoryPhysicalCount

    _lock_idempotency_keys(session, [data.count_id])
    existing = session.exec(
        select(InventoryPhysicalCount)
        .where(InventoryPhysicalCount.count_id == data.count_id)
        .with_for_update()
    ).first()
    if existing is not None:
        if (
            existing.sku_id == data.sku_id
            and existing.warehouse == data.warehouse.value
            and existing.observed_quantity == data.observed_quantity
            and existing.user_uuid == user_uuid
        ):
            return existing
        raise _conflict("count_id already exists with different content")

    sku = _lock_skus(session, {data.sku_id}).get(data.sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU not found.")
    if sku.warehouse != data.warehouse.value:
        raise HTTPException(status_code=400, detail="Warehouse mismatch for SKU.")
    if not sku.client_id:
        raise InventoryDataIntegrityError({data.sku_id})
    expected = get_current_stock(session, data.sku_id, data.warehouse.value)
    occurred_at = datetime.now(timezone.utc)
    count = InventoryPhysicalCount(
        count_id=data.count_id,
        sku_id=data.sku_id,
        client_id=sku.client_id,
        warehouse=data.warehouse.value,
        expected_quantity=expected,
        observed_quantity=data.observed_quantity,
        confirmed_at=occurred_at,
        user_uuid=user_uuid,
    )
    events: list[TelemetryEvent] = []
    try:
        session.add(count)
        session.flush()
        if count.id is None:
            raise RuntimeError("physical count identity was not assigned")
        delta = data.observed_quantity - expected
        if delta:
            warehouse = canonical_telemetry_warehouse(data.warehouse.value)
            if warehouse is None:
                raise InventoryDataIntegrityError({data.sku_id})
            tags = {
                "warehouse": warehouse,
                "client_id": sku.client_id,
                "product_id": str(sku.id),
                "product_category": sku.category,
                "quantity": abs(delta),
                "system_quantity": expected,
                "counted_quantity": data.observed_quantity,
                "discrepancy_delta": delta,
                "discrepancy_type": "surplus" if delta > 0 else "shortage",
                "count_id": str(count.count_id),
            }
            event = _event(
                event_type="inventory_discrepancy_detected",
                user_uuid=user_uuid,
                request_id=request_id,
                occurred_at=occurred_at,
                tags=tags,
            )
            enqueue_inventory_event(
                session,
                event_id=event.eventId,
                occurred_at=occurred_at,
                source_table="inventory_physical_count",
                source_pk=count.id,
                event_type=event.event_type,
                tags=tags,
            )
            events.append(event)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(count)
    _capture_after_commit(events)
    return count


def _dispatch_matches(record: object, payload: object, order_id: int) -> bool:
    return (
        record.commercial_order_id == order_id
        and record.sku_id == payload.sku_id
        and record.quantity == payload.quantity
        and record.exit_type == "dispatch"
        and record.tracking_number == payload.tracking_number
    )


def _order_dispatches(session: Session, order_pk: int) -> list[object]:
    from services.api.inventory_models import StockExit

    return list(
        session.exec(
            select(StockExit)
            .where(StockExit.commercial_order_id == order_pk)
            .order_by(StockExit.dispatch_id)
        ).all()
    )


def _validate_order_skus(
    session: Session,
    *,
    dispatches: list[object],
    client_id: str,
    warehouse: str,
) -> dict[int, object]:
    sku_ids = {item.sku_id for item in dispatches}
    skus = _lock_skus(session, sku_ids)
    missing = sku_ids - set(skus)
    if missing:
        raise InventoryDataIntegrityError(missing)
    for sku in skus.values():
        if sku.client_id != client_id or sku.warehouse != warehouse:
            raise HTTPException(
                status_code=400,
                detail="Order client or warehouse does not match its SKU.",
            )
    required: dict[int, int] = defaultdict(int)
    for item in dispatches:
        required[item.sku_id] += item.quantity
    for sku_id, quantity in required.items():
        available = get_current_stock(session, sku_id, warehouse)
        if quantity > available:
            raise HTTPException(status_code=400, detail="Insufficient stock for order.")
    return skus


def _add_order_dispatches(
    session: Session,
    *,
    order: object,
    dispatches: list[object],
    skus: dict[int, object],
    user_uuid: str,
    request_id: str | None,
    occurred_at: datetime,
) -> tuple[list[object], list[TelemetryEvent]]:
    from services.api.inventory_models import StockExit

    running = {
        sku_id: get_current_stock(session, sku_id, order.warehouse)
        for sku_id in skus
    }
    created: list[object] = []
    events: list[TelemetryEvent] = []
    for item in sorted(dispatches, key=lambda value: str(value.dispatch_id)):
        before = running[item.sku_id]
        record = StockExit(
            dispatch_id=item.dispatch_id,
            commercial_order_id=order.id,
            sku_id=item.sku_id,
            quantity=item.quantity,
            exit_type="dispatch",
            tracking_number=item.tracking_number,
            warehouse=order.warehouse,
            user_uuid=user_uuid,
            created_at=occurred_at,
        )
        session.add(record)
        session.flush()
        if record.id is None:
            raise RuntimeError("dispatch identity was not assigned")
        after = before - item.quantity
        running[item.sku_id] = after
        threshold = _threshold_event(
            session,
            sku=skus[item.sku_id],
            source_pk=record.id,
            before=before,
            after=after,
            user_uuid=user_uuid,
            request_id=request_id,
            occurred_at=occurred_at,
        )
        if threshold is not None:
            events.append(threshold)
        _record_threshold_evaluation(
            session,
            source_table="stockexit",
            source_pk=record.id,
            sku=skus[item.sku_id],
            before=before,
            after=after,
            event=threshold,
            evaluated_at=occurred_at,
        )
        created.append(record)
    return created, events


def create_commercial_order(
    session: Session,
    data: CommercialOrderCreate,
    *,
    user_uuid: str,
    request_id: str | None = None,
):
    """Create one order, its initial dispatches, and one outbound event."""
    from services.api.inventory_models import CommercialOrder, StockExit

    dispatch_ids = [item.dispatch_id for item in data.dispatches]
    if len(dispatch_ids) != len(set(dispatch_ids)):
        raise _conflict("dispatch_id values must be unique within a command")
    _lock_idempotency_keys(session, [data.order_id, *dispatch_ids])
    existing = session.exec(
        select(CommercialOrder)
        .where(CommercialOrder.order_id == data.order_id)
        .with_for_update()
    ).first()
    if existing is not None:
        persisted = {item.dispatch_id: item for item in _order_dispatches(session, existing.id)}
        if existing.client_id != data.client_id or existing.warehouse != data.warehouse.value:
            raise _conflict("order_id already exists with different content")
        if set(persisted) == set(dispatch_ids) and all(
            item.dispatch_id in persisted
            and _dispatch_matches(persisted[item.dispatch_id], item, existing.id)
            for item in data.dispatches
        ):
            return existing, list(persisted.values())
        raise _conflict("order_id already exists with different dispatches")

    existing_dispatches = session.exec(
        select(StockExit).where(StockExit.dispatch_id.in_(dispatch_ids))
    ).all()
    if existing_dispatches:
        raise _conflict("dispatch_id already exists")
    skus = _validate_order_skus(
        session,
        dispatches=list(data.dispatches),
        client_id=data.client_id,
        warehouse=data.warehouse.value,
    )
    occurred_at = datetime.now(timezone.utc)
    order = CommercialOrder(
        order_id=data.order_id,
        client_id=data.client_id,
        warehouse=data.warehouse.value,
        created_at=occurred_at,
    )
    events: list[TelemetryEvent] = []
    try:
        session.add(order)
        session.flush()
        if order.id is None:
            raise RuntimeError("commercial order identity was not assigned")
        created, threshold_events = _add_order_dispatches(
            session,
            order=order,
            dispatches=list(data.dispatches),
            skus=skus,
            user_uuid=user_uuid,
            request_id=request_id,
            occurred_at=occurred_at,
        )
        tags = {
            "order_id": str(order.order_id),
            "warehouse": canonical_telemetry_warehouse(order.warehouse),
            "client_id": order.client_id,
        }
        outbound = _event(
            event_type="outbound_order_created",
            user_uuid=user_uuid,
            request_id=request_id,
            occurred_at=occurred_at,
            tags=tags,
        )
        enqueue_inventory_event(
            session,
            event_id=outbound.eventId,
            occurred_at=occurred_at,
            source_table="commercial_order",
            source_pk=order.id,
            event_type=outbound.event_type,
            tags=tags,
        )
        events = [outbound, *threshold_events]
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(order)
    invalidate_inventory_cache(session)
    _capture_after_commit(events)
    return order, created


def append_commercial_order_dispatches(
    session: Session,
    data: CommercialOrderDispatchAppend,
    *,
    user_uuid: str,
    request_id: str | None = None,
):
    """Append new partials, deduplicating exact dispatch-id retries."""
    from services.api.inventory_models import CommercialOrder, StockExit

    dispatch_ids = [item.dispatch_id for item in data.dispatches]
    if len(dispatch_ids) != len(set(dispatch_ids)):
        raise _conflict("dispatch_id values must be unique within a command")
    _lock_idempotency_keys(session, [data.order_id, *dispatch_ids])
    order = session.exec(
        select(CommercialOrder)
        .where(CommercialOrder.order_id == data.order_id)
        .with_for_update()
    ).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Commercial order not found.")
    existing = {
        item.dispatch_id: item
        for item in session.exec(
            select(StockExit).where(StockExit.dispatch_id.in_(dispatch_ids))
        ).all()
    }
    new_items = []
    for item in data.dispatches:
        record = existing.get(item.dispatch_id)
        if record is None:
            new_items.append(item)
        elif not _dispatch_matches(record, item, order.id):
            raise _conflict("dispatch_id already exists with different content")
    if not new_items:
        return order, _order_dispatches(session, order.id)
    skus = _validate_order_skus(
        session,
        dispatches=new_items,
        client_id=order.client_id,
        warehouse=order.warehouse,
    )
    try:
        occurred_at = datetime.now(timezone.utc)
        _created, events = _add_order_dispatches(
            session,
            order=order,
            dispatches=new_items,
            skus=skus,
            user_uuid=user_uuid,
            request_id=request_id,
            occurred_at=occurred_at,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    invalidate_inventory_cache(session)
    _capture_after_commit(events)
    return order, _order_dispatches(session, order.id)


# ═════════════════════════════════════════════════════════════════════════════
# Orders — list all movements (entries + exits) with SKU data, N+1 free
# ═════════════════════════════════════════════════════════════════════════════


def list_orders(session: Session) -> list[dict]:
    """Return all stock movements (entries + exits) with SKU data.

    Strategy to avoid N+1
    ---------------------
    1. Fetch all StockEntry rows in **one** query.
    2. Fetch all StockExit rows in **one** query.
    3. Collect **all** distinct ``sku_id`` values from both sets.
    4. Fetch **all** matching SKU rows in **one** query.
    5. Build an ``sku_map: dict[int, SKU]`` and map SKU data in Python.

    The number of database queries is **constant** (3) regardless of
    the number of movements — no per-movement lookup.

    Returns
    -------
    list[dict]
        Combined list of movements sorted by ``created_at`` ascending,
        with ties broken by ``movement_type`` then ``id``.
        Each dict is shaped like ``InventoryOrderResponse``.
    """
    from services.api.inventory_models import SKU, StockEntry, StockExit

    # 1. Fetch all entries and exits
    entries = session.exec(select(StockEntry)).all()  # type: ignore[arg-type]
    exits = session.exec(select(StockExit)).all()      # type: ignore[arg-type]

    # 2. Collect unique sku_ids
    sku_ids: set[int] = set()
    for e in entries:
        sku_ids.add(e.sku_id)
    for x in exits:
        sku_ids.add(x.sku_id)

    # 3. Bulk load SKUs — exactly ONE query for all SKUs
    sku_map: dict[int, object] = {}
    if sku_ids:
        skus = session.exec(select(SKU).where(SKU.id.in_(sku_ids))).all()  # type: ignore[arg-type]
        sku_map = {sku.id: sku for sku in skus}

    # A missing SKU is a data-integrity failure, not a valid nullable
    # relationship.  Detect it before constructing any movement result.
    missing_sku_ids = sku_ids - set(sku_map)
    if missing_sku_ids:
        raise InventoryDataIntegrityError(missing_sku_ids)

    # 4. Build combined list
    results: list[dict] = []

    for e in entries:
        sku = sku_map[e.sku_id]
        results.append(
            {
                "id": e.id,
                "movement_type": "inbound",
                "sku_id": e.sku_id,
                "quantity": e.quantity,
                "warehouse": e.warehouse,
                "created_at": e.created_at,
                "user_uuid": e.user_uuid,
                "sku": sku,
                "reference": e.reference,
                "exit_type": None,
                "tracking_number": None,
            }
        )

    for x in exits:
        sku = sku_map[x.sku_id]
        results.append(
            {
                "id": x.id,
                "movement_type": "outbound",
                "sku_id": x.sku_id,
                "quantity": x.quantity,
                "warehouse": x.warehouse,
                "created_at": x.created_at,
                "user_uuid": x.user_uuid,
                "sku": sku,
                "reference": None,
                "exit_type": x.exit_type,
                "tracking_number": x.tracking_number,
            }
        )

    # 5. Sort — deterministic: created_at ascending, then movement_type,
    #    then id
    results.sort(key=lambda r: (r["created_at"], r["movement_type"], r["id"]))

    return results
