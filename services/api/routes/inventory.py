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

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from services.api.auth_models import UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
from services.api.inventory_models import SKU
from services.api.inventory_schemas import (
    InventoryOrderResponse,
    SKUCreate,
    SKUSummary,
    SKUResponse,
    StockEntryCreate,
    StockEntryResponse,
    StockExitCreate,
    StockExitResponse,
)
from services.api.inventory_service import (
    create_stock_entry,
    create_stock_exit,
    get_current_stock,
    get_current_stocks,
    list_orders,
)

router = APIRouter(prefix="/inventory", tags=["Inventory"])


# ── Helpers ─────────────────────────────────────────────────────────────────


def _sku_to_response(sku: SKU, stock_map: dict[tuple[int, str], int]) -> SKUResponse:
    """Convert a SKU ORM to ``SKUResponse`` using a pre-computed stock map."""
    return SKUResponse(
        id=sku.id,
        name=sku.name,
        sku=sku.sku,
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=stock_map.get((sku.id, sku.warehouse), 0),
    )


# ── GET /inventory/products ─────────────────────────────────────────────────


@router.get("/products", response_model=list[SKUResponse])
def list_products(
    session: Annotated[Session, Depends(get_db)],
) -> list[SKUResponse]:
    """Return all SKUs with current stock computed per warehouse.

    Stock is computed via **two** aggregate queries (entries + exits)
    grouped by (sku_id, warehouse) — this avoids N+1 behaviour while
    maintaining per-warehouse accuracy.
    """
    skus = session.exec(select(SKU)).all()
    stock_map = get_current_stocks(session)
    return [_sku_to_response(sku, stock_map) for sku in skus]


# ── GET /inventory/products/{id} ─────────────────────────────────────────────


@router.get("/products/{id}", response_model=SKUResponse)
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
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=current_stock,
    )


# ── POST /inventory/products ────────────────────────────────────────────────


@router.post("/products", response_model=SKUResponse)
def create_product(
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
        client_name=payload.client_name,
        category=payload.category,
        warehouse=payload.warehouse,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)

    # New SKUs have zero stock — no movements recorded yet.
    return SKUResponse(
        id=sku.id,
        name=sku.name,
        sku=sku.sku,
        client_name=sku.client_name,
        category=sku.category,
        warehouse=sku.warehouse,
        current_stock=0,
    )


# ── POST /inventory/orders/inbound ────────────────────────────────────────────


@router.post("/orders/inbound", response_model=StockEntryResponse, status_code=status.HTTP_201_CREATED)
def create_inbound_order(
    payload: StockEntryCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> StockEntryResponse:
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
    )
    return StockEntryResponse(
        id=entry.id,
        sku_id=entry.sku_id,
        quantity=entry.quantity,
        reference=entry.reference,
        warehouse=entry.warehouse,
        created_at=entry.created_at,
        user_uuid=entry.user_uuid,
    )


# ── POST /inventory/orders/outbound ──────────────────────────────────────────


@router.post("/orders/outbound", response_model=StockExitResponse, status_code=status.HTTP_201_CREATED)
def create_outbound_order(
    payload: StockExitCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> StockExitResponse:
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
    return StockExitResponse(
        id=exit_record.id,
        sku_id=exit_record.sku_id,
        quantity=exit_record.quantity,
        exit_type=exit_record.exit_type,
        tracking_number=exit_record.tracking_number,
        warehouse=exit_record.warehouse,
        created_at=exit_record.created_at,
        user_uuid=exit_record.user_uuid,
    )


# ── GET /inventory/orders ───────────────────────────────────────────────────


@router.get("/orders", response_model=list[InventoryOrderResponse])
def list_orders_endpoint(
    session: Annotated[Session, Depends(get_db)],
) -> list[InventoryOrderResponse]:
    """Return all stock movements (entries + exits) with SKU data.

    Movements are combined in a single list sorted by ``created_at``
    ascending.  Each item includes the associated SKU details.

    This endpoint avoids N+1 lookups by bulk-loading all SKU records
    in a single query and mapping them in Python.

    No authentication required (public, like GET products).
    """
    raw = list_orders(session=session)
    return [
        InventoryOrderResponse(
            id=item["id"],
            movement_type=item["movement_type"],
            sku_id=item["sku_id"],
            quantity=item["quantity"],
            warehouse=item["warehouse"],
            created_at=item["created_at"],
            user_uuid=item["user_uuid"],
            sku=SKUSummary(
                id=item["sku"].id,
                name=item["sku"].name,
                sku=item["sku"].sku,
                client_name=item["sku"].client_name,
                category=item["sku"].category,
                warehouse=item["sku"].warehouse,
            ) if item["sku"] else None,
            reference=item["reference"],
            exit_type=item["exit_type"],
            tracking_number=item["tracking_number"],
        )
        for item in raw
    ]