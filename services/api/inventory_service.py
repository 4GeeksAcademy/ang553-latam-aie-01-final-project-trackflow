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

from fastapi import HTTPException
from sqlmodel import Session, func, select

from services.api.inventory_schemas import StockEntryCreate, StockExitCreate


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
    sku = get_sku_or_none(session, data.sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU not found.")

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
        )

    # 3. Build and persist
    entry = StockEntry(
        sku_id=data.sku_id,
        quantity=data.quantity,
        reference=data.reference,
        warehouse=movement_warehouse,
        user_uuid=user_uuid,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)

    return entry


# ═════════════════════════════════════════════════════════════════════════════
# StockExit — outbound movement (with stock sufficiency guard)
# ═════════════════════════════════════════════════════════════════════════════


def create_stock_exit(
    session: Session,
    data: StockExitCreate,
    user_uuid: str,
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
    sku = get_sku_or_none(session, data.sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU not found.")

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
        )

    # 3. Calculate available stock and validate sufficiency
    available = get_current_stock(session, sku_id=sku.id, warehouse=movement_warehouse)
    if data.quantity > available:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient stock for SKU '{sku.sku}'. "
                f"Available: {available}, requested: {data.quantity}."
            ),
        )

    # 4. Build and persist
    exit_record = StockExit(
        sku_id=data.sku_id,
        quantity=data.quantity,
        exit_type=data.exit_type,
        tracking_number=data.tracking_number,
        warehouse=movement_warehouse,
        user_uuid=user_uuid,
    )
    session.add(exit_record)
    session.commit()
    session.refresh(exit_record)

    return exit_record


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

    # 4. Build combined list
    results: list[dict] = []

    for e in entries:
        sku = sku_map.get(e.sku_id)
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
        sku = sku_map.get(x.sku_id)
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