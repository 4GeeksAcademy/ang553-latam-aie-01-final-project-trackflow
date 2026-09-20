"""
TrackFlow Inventory routes — SKU (product) endpoints.

Provides:
    - GET  /inventory/products              — list all SKUs with computed stock
    - GET  /inventory/products/{id}         — single SKU with computed stock
    - POST /inventory/products              — create a new SKU (auth required)
    - POST /inventory/orders/inbound        — register an inbound stock movement
    - POST /inventory/orders/outbound       — register an outbound stock movement

Stock is always calculated on the fly via ``get_current_stock()`` — never
persisted as a column.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from services.api.auth_models import UserInDB
from services.api.auth_security import get_current_user
from services.api.cache import (
    invalidate_products_cache,
    inventory_cache_key,
    orders_cache,
    products_cache,
)
from services.api.database import get_db
from services.api.inventory_models import SKU
from services.api.inventory_schemas import (
    InventoryOrderListItem,
    SKUCreate,
    SKUResponse,
    StockEntryCreate,
    MovementCreatedResponse,
    StockExitCreate,
)
from services.api.inventory_service import (
    InventoryDataIntegrityError,
    create_stock_entry,
    create_stock_exit,
    get_current_stock,
    get_current_stocks,
    list_orders,
)
from services.api.telemetry_capture import capture_telemetry_events
from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_utils import canonical_telemetry_warehouse

router = APIRouter(prefix="/inventory", tags=["Inventory"])
logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _sku_to_response(sku: SKU, stock_map: dict[tuple[int, str], int]) -> SKUResponse:
    """Convert a SKU ORM to ``SKUResponse`` using a pre-computed stock map."""
    return SKUResponse(
        id=sku.id,
        name=sku.name,
        sku=sku.sku,
        client_id=sku.client_id,
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=stock_map.get((sku.id, sku.warehouse), 0),
    )


def _order_items_to_response(raw: list[dict]) -> list[InventoryOrderListItem]:
    """Convert service rows into session-independent HTTP projections."""
    return [
        InventoryOrderListItem(
            id=item["id"],
            movement_type=item["movement_type"],
            quantity=item["quantity"],
            warehouse=item["warehouse"],
            created_at=item["created_at"],
            user_uuid=item["user_uuid"],
            sku_name=item["sku"].name,
            sku_code=item["sku"].sku,
            reference=item["reference"],
            exit_type=item["exit_type"],
            tracking_number=item["tracking_number"],
        )
        for item in raw
    ]




# ── GET /inventory/products ─────────────────────────────────────────────────


@router.get(
    "/products",
    response_model=list[SKUResponse],
    dependencies=[Depends(get_current_user)],
)
def list_products(
    session: Annotated[Session, Depends(get_db)],
) -> list[SKUResponse]:
    """Return all SKUs with current stock computed per warehouse.

    Stock is computed via **two** aggregate queries (entries + exits)
    grouped by (sku_id, warehouse) — this avoids N+1 behaviour while
    maintaining per-warehouse accuracy.
    """
    cache_key = inventory_cache_key("products", session)
    cached = products_cache.get(cache_key)
    if cached is not None:
        return cached

    skus = session.exec(select(SKU)).all()
    stock_map = get_current_stocks(session)
    result = [_sku_to_response(sku, stock_map) for sku in skus]
    products_cache.set(cache_key, result)
    return result


# ── GET /inventory/products/{id} ─────────────────────────────────────────────


@router.get(
    "/products/{id}",
    response_model=SKUResponse,
    dependencies=[Depends(get_current_user)],
)
def get_product(
    id: int,
    session: Annotated[Session, Depends(get_db)],
) -> SKUResponse:
    """Return a single SKU by primary key, with computed current_stock.

    Uses ``get_current_stock()`` (single-SKU aggregation) — no bulk
    overhead needed for a single product.

    Raises ``404`` if the SKU does not exist.
    """
    sku = session.get(SKU, id)
    if sku is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SKU not found.",
        )
    current_stock = get_current_stock(
        session,
        sku_id=sku.id,
        warehouse=sku.warehouse,
    )
    return SKUResponse(
        id=sku.id,
        name=sku.name,
        sku=sku.sku,
        client_id=sku.client_id,
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=current_stock,
    )


# ── POST /inventory/products ────────────────────────────────────────────────


@router.post("/products", response_model=SKUResponse)
def create_product(
    request: Request,
    payload: SKUCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> SKUResponse:
    """Create a new SKU.

    Requires authentication.  The new SKU starts with ``current_stock=0``
    because no movements exist yet — stock is never persisted directly.
    """
    sku = SKU(
        name=payload.name,
        sku=payload.sku,
        client_id=payload.client_id,
        client_name=payload.client_name,
        category=payload.category,
        warehouse=payload.warehouse,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)

    if sku.client_id is None:
        logger.warning("Telemetry skipped for inventory product creation")
    else:
        try:
            warehouse = canonical_telemetry_warehouse(sku.warehouse)
            if warehouse is None:
                logger.warning("Telemetry skipped for inventory product creation")
            else:
                event = TelemetryEvent(
                    eventId=uuid4(),
                    timestamp=datetime.now(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    sessionId=None,
                    userId=current_user.id,
                    event_type="inventory_product_created",
                    schemaVersion="1.0",
                    requestId=getattr(request.state, "request_id", None),
                    properties={
                        "warehouse": warehouse,
                        "client_id": sku.client_id,
                        "product_id": str(sku.id),
                        "product_category": sku.category,
                    },
                )
                capture_telemetry_events([event])
        except Exception:
            logger.warning("Telemetry capture failed for inventory product creation")

    invalidate_products_cache(session)

    # New SKUs have zero stock — no movements recorded yet.
    return SKUResponse(
        id=sku.id,
        name=sku.name,
        sku=sku.sku,
        client_id=sku.client_id,
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=0,
    )


# ── POST /inventory/orders/inbound ────────────────────────────────────────────


@router.post("/orders/inbound", response_model=MovementCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_inbound_order(
    request: Request,
    payload: StockEntryCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> MovementCreatedResponse:
    """Register an inbound stock movement (stock entry).

    Delegates all business logic (SKU existence, warehouse mismatch,
    persistence) to ``create_stock_entry()`` — no duplication of
    domain rules in the router.

    The ``user_uuid`` is populated from the authenticated user's ID,
    **not** from the client payload.
    """
    entry = create_stock_entry(
        session=session,
        data=payload,
        user_uuid=current_user.id,
        request_id=getattr(request.state, "request_id", None),
    )
    return MovementCreatedResponse(id=entry.id)


# ── POST /inventory/orders/outbound ──────────────────────────────────────────


@router.post("/orders/outbound", response_model=MovementCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_outbound_order(
    payload: StockExitCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> MovementCreatedResponse:
    """Register an outbound stock movement (stock exit).

    Delegates all business logic (SKU existence, warehouse mismatch,
    stock sufficiency, persistence) to ``create_stock_exit()`` — no
    duplication of domain rules in the router.

    The ``user_uuid`` is populated from the authenticated user's ID,
    **not** from the client payload.
    """
    exit_record = create_stock_exit(
        session=session,
        data=payload,
        user_uuid=current_user.id,
    )
    return MovementCreatedResponse(id=exit_record.id)


# ── GET /inventory/orders ───────────────────────────────────────────────────


@router.get(
    "/orders",
    response_model=list[InventoryOrderListItem],
    dependencies=[Depends(get_current_user)],
)
def list_orders_endpoint(
    session: Annotated[Session, Depends(get_db)],
) -> list[InventoryOrderListItem]:
    """Return all stock movements (entries + exits) with SKU data.

    Movements are combined in a single list sorted by ``created_at``
    ascending.  Each item includes the associated SKU details.

    This endpoint avoids N+1 lookups by bulk-loading all SKU records
    in a single query and mapping them in Python.

    Inventory reads require authentication.
    """
    cache_key = inventory_cache_key("orders", session)
    cached = orders_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = list_orders(session=session)
    except InventoryDataIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Inventory data integrity error.",
        ) from exc

    result = _order_items_to_response(raw)
    orders_cache.set(cache_key, result)
    return result
