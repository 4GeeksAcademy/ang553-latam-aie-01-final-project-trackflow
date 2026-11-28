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
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response, status
from sqlmodel import Session, select

from services.api.auth_models import UserInDB
from services.api.operational_oidc import get_operational_user
from services.api.access_control import build_access_context
from services.api.cache import (
    invalidate_products_cache,
    inventory_cache_key,
    orders_cache,
    products_cache,
)
from services.api.database import get_db
from services.api.inventory_models import SKU
from services.api.inventory_schemas import (
    AuthorizedTrackingResponse,
    InventoryOrderListItem,
    CommercialOrderCreate,
    CommercialOrderDispatchAppend,
    CommercialOrderMutationResponse,
    DispatchReviewResponse,
    DispatchReviewSummary,
    InventoryPhysicalCountCreate,
    InventoryPhysicalCountResponse,
    SKUCreate,
    SKUResponse,
    SKUThresholdUpdate,
    StockEntryCreate,
    MovementCreatedResponse,
    StockExitCreate,
)
from services.api.inventory_service import (
    InventoryDataIntegrityError,
    InventoryProductNotFound,
    create_stock_entry,
    create_stock_exit,
    append_commercial_order_dispatches,
    confirm_inventory_physical_count,
    create_commercial_order,
    get_current_stock,
    get_current_stocks,
    list_orders,
    lookup_authorized_tracking,
    read_inventory_product,
    require_active_inventory_client,
    review_commercial_order,
    review_commercial_order_append,
    review_standalone_dispatch,
    update_sku_threshold,
)
from services.api.inventory_dispatch_review import (
    PROOF_HEADER,
    audit_confirmation,
    issue_review_proof,
    verify_review_proof,
)
from services.api.telemetry_capture import capture_telemetry_events
from services.api.telemetry_schemas import TelemetryEvent
from services.api.telemetry_utils import canonical_telemetry_warehouse

router = APIRouter(prefix="/inventory", tags=["Inventory"])
logger = logging.getLogger(__name__)

_OP_STANDALONE = "standalone_dispatch"
_OP_ORDER_CREATE = "commercial_order_create"
_OP_ORDER_APPEND = "commercial_order_append"
_PATH_STANDALONE = "/inventory/orders/outbound"
_PATH_ORDER_CREATE = "/inventory/commercial-orders"
_REVIEW_REQUIRED_DETAIL = "A valid dispatch review proof is required."
_COMMERCIAL_ORDER_CONFLICT_RESPONSE = {
    "description": (
        "The commercial order command conflicts with an existing command. "
        "The response does not disclose another tenant's data."
    ),
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["detail"],
                "properties": {"detail": {"type": "string"}},
            },
            "example": {"detail": "Commercial order command conflicts."},
        }
    },
}
_REVIEW_PROOF_PARAMETER = {
    "name": PROOF_HEADER,
    "in": "header",
    "required": True,
    "description": (
        "Short-lived proof returned by the matching dispatch review endpoint."
    ),
    "schema": {"type": "string", "minLength": 1},
}
_REVIEW_REQUIRED_RESPONSE = {
    "description": "Dispatch review proof is missing, invalid, expired, or mismatched.",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["detail"],
                "properties": {
                    "detail": {
                        "type": "string",
                        "enum": [_REVIEW_REQUIRED_DETAIL],
                    }
                },
            },
            "example": {"detail": _REVIEW_REQUIRED_DETAIL},
        }
    },
}
_NO_STORE_RESPONSE = {
    "description": "Review proof and non-sensitive command summary.",
    "headers": {
        "Cache-Control": {
            "description": "Review proofs must not be cached.",
            "schema": {"type": "string", "enum": ["no-store"]},
        }
    },
}


def _review_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        detail=_REVIEW_REQUIRED_DETAIL,
    )


def _verify_review(token: str | None, **kwargs) -> dict:
    try:
        return verify_review_proof(token, **kwargs)
    except ValueError:
        audit_confirmation(kwargs["actor_id"], kwargs["operation"], "rejected")
        raise _review_error() from None


def _review_response(
    *, actor_id: str, access, tenant_id: str, operation: str,
    endpoint: str, payload, warehouse, sku_ids: list[int],
    total_quantity: int, dispatch_count: int, command_id: UUID | None,
    idempotency_key: UUID | None = None,
) -> DispatchReviewResponse:
    proof, expires_at = issue_review_proof(
        actor_id=actor_id, access=access, tenant_id=tenant_id,
        operation=operation, endpoint=endpoint, payload=payload,
        idempotency_key=idempotency_key,
    )
    return DispatchReviewResponse(
        review_proof=proof,
        expires_at=expires_at,
        summary=DispatchReviewSummary(
            operation=operation,
            tenant_id=tenant_id,
            warehouse=warehouse,
            sku_ids=sorted(sku_ids),
            total_quantity=total_quantity,
            dispatch_count=dispatch_count,
            command_id=command_id,
        ),
    )


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
        minimum_stock_threshold=sku.minimum_stock_threshold,
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
)
def list_products(
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> list[SKUResponse]:
    """Return all SKUs with current stock computed per warehouse.

    Stock is computed via **two** aggregate queries (entries + exits)
    grouped by (sku_id, warehouse) — this avoids N+1 behaviour while
    maintaining per-warehouse accuracy.
    """
    access = build_access_context(session, current_user)
    cache_key = inventory_cache_key("products", session, access.cache_scope)
    cached = products_cache.get(cache_key)
    if cached is not None:
        return cached

    statement = select(SKU)
    if not access.global_access:
        statement = statement.where(SKU.client_id.in_(access.allowed_client_ids))
    skus = session.exec(statement).all()
    stock_map = get_current_stocks(
        session,
        None if access.global_access else frozenset(sku.id for sku in skus),
    )
    result = [_sku_to_response(sku, stock_map) for sku in skus]
    products_cache.set(cache_key, result)
    return result


# ── GET /inventory/products/{id} ─────────────────────────────────────────────


@router.get(
    "/products/{id}",
    response_model=SKUResponse,
)
def get_product(
    id: int,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> SKUResponse:
    """Return a single SKU by primary key, with computed current_stock.

    Uses ``get_current_stock()`` (single-SKU aggregation) — no bulk
    overhead needed for a single product.

    Raises ``404`` if the SKU does not exist.
    """
    access = build_access_context(session, current_user)
    try:
        return read_inventory_product(session, product_id=id, access=access)
    except (InventoryProductNotFound, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SKU not found.",
        ) from None


# ── POST /inventory/products ────────────────────────────────────────────────


@router.post("/products", response_model=SKUResponse)
def create_product(
    request: Request,
    payload: SKUCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> SKUResponse:
    """Create a new SKU.

    Requires authentication.  The new SKU starts with ``current_stock=0``
    because no movements exist yet — stock is never persisted directly.
    """
    access = build_access_context(session, current_user)
    client = require_active_inventory_client(
        session, client_id=payload.client_id, access=access
    )
    if client["display_name"] != payload.client_name:
        raise HTTPException(status_code=422, detail="Client identity does not match.")
    sku = SKU(
        name=payload.name,
        sku=payload.sku,
        client_id=payload.client_id,
        client_name=payload.client_name,
        category=payload.category,
        warehouse=payload.warehouse,
        minimum_stock_threshold=payload.minimum_stock_threshold,
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
        minimum_stock_threshold=sku.minimum_stock_threshold,
        current_stock=0,
    )


@router.patch("/products/{id}/threshold", response_model=SKUResponse)
def configure_product_threshold(
    id: int,
    payload: SKUThresholdUpdate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> SKUResponse:
    """Configure or disable the threshold; configuration emits no event."""
    sku = update_sku_threshold(
        session,
        sku_id=id,
        minimum_stock_threshold=payload.minimum_stock_threshold,
        access=build_access_context(session, current_user),
    )
    stock = get_current_stock(session, sku.id, sku.warehouse)
    return _sku_to_response(sku, {(sku.id, sku.warehouse): stock})


@router.post(
    "/physical-counts",
    response_model=InventoryPhysicalCountResponse,
    status_code=status.HTTP_201_CREATED,
)
def confirm_physical_count(
    request: Request,
    payload: InventoryPhysicalCountCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> InventoryPhysicalCountResponse:
    return InventoryPhysicalCountResponse.model_validate(
        confirm_inventory_physical_count(
            session,
            payload,
            user_uuid=current_user.id,
            access=build_access_context(session, current_user),
            request_id=getattr(request.state, "request_id", None),
        )
    )


def _order_mutation_response(order, dispatches) -> CommercialOrderMutationResponse:
    return CommercialOrderMutationResponse(
        id=order.id,
        order_id=order.order_id,
        dispatch_ids=sorted(
            (item.dispatch_id for item in dispatches),
            key=str,
        ),
    )


@router.post(
    "/dispatch-reviews/commercial-orders",
    response_model=DispatchReviewResponse,
    responses={200: _NO_STORE_RESPONSE},
)
def review_order_with_dispatches(
    response: Response,
    payload: CommercialOrderCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> DispatchReviewResponse:
    access = build_access_context(session, current_user)
    review_commercial_order(session, payload, access)
    response.headers["Cache-Control"] = "no-store"
    return _review_response(
        actor_id=current_user.id, access=access, tenant_id=payload.client_id,
        operation=_OP_ORDER_CREATE, endpoint=_PATH_ORDER_CREATE,
        payload=payload, warehouse=payload.warehouse,
        sku_ids=[item.sku_id for item in payload.dispatches],
        total_quantity=sum(item.quantity for item in payload.dispatches),
        dispatch_count=len(payload.dispatches),
        command_id=payload.order_id,
    )


@router.post(
    "/dispatch-reviews/commercial-orders/{order_id}/dispatches",
    response_model=DispatchReviewResponse,
    responses={200: _NO_STORE_RESPONSE},
)
def review_order_dispatch_append(
    order_id: UUID,
    response: Response,
    payload: CommercialOrderDispatchAppend,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> DispatchReviewResponse:
    if order_id != payload.order_id:
        raise HTTPException(status_code=409, detail="order_id path and payload mismatch")
    access = build_access_context(session, current_user)
    order, _skus = review_commercial_order_append(session, payload, access)
    response.headers["Cache-Control"] = "no-store"
    endpoint = f"/inventory/commercial-orders/{order_id}/dispatches"
    return _review_response(
        actor_id=current_user.id, access=access, tenant_id=order.client_id,
        operation=_OP_ORDER_APPEND, endpoint=endpoint,
        payload=payload, warehouse=order.warehouse,
        sku_ids=[item.sku_id for item in payload.dispatches],
        total_quantity=sum(item.quantity for item in payload.dispatches),
        dispatch_count=len(payload.dispatches),
        command_id=payload.order_id,
    )


@router.post(
    "/commercial-orders",
    response_model=CommercialOrderMutationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: _COMMERCIAL_ORDER_CONFLICT_RESPONSE, 428: _REVIEW_REQUIRED_RESPONSE},
    openapi_extra={"parameters": [_REVIEW_PROOF_PARAMETER]},
)
def create_order_with_dispatches(
    request: Request,
    payload: CommercialOrderCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
    review_proof: Annotated[
        str | None, Header(alias=PROOF_HEADER, include_in_schema=False)
    ] = None,
) -> CommercialOrderMutationResponse:
    access = build_access_context(session, current_user)
    review_commercial_order(session, payload, access)
    review_claims = _verify_review(
        review_proof, actor_id=current_user.id, access=access,
        tenant_id=payload.client_id, operation=_OP_ORDER_CREATE,
        endpoint=_PATH_ORDER_CREATE, payload=payload,
    )
    try:
        order, dispatches = create_commercial_order(
            session,
            payload,
            user_uuid=current_user.id,
            access=access,
            request_id=getattr(request.state, "request_id", None),
        )
    except Exception:
        audit_confirmation(
            current_user.id, _OP_ORDER_CREATE, "failed",
            confirmation_id=review_claims["jti"],
        )
        raise
    audit_confirmation(
        current_user.id, _OP_ORDER_CREATE, "confirmed",
        confirmation_id=review_claims["jti"],
    )
    return _order_mutation_response(order, dispatches)


@router.post(
    "/commercial-orders/{order_id}/dispatches",
    response_model=CommercialOrderMutationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={428: _REVIEW_REQUIRED_RESPONSE},
    openapi_extra={"parameters": [_REVIEW_PROOF_PARAMETER]},
)
def append_order_dispatches(
    order_id: UUID,
    request: Request,
    payload: CommercialOrderDispatchAppend,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
    review_proof: Annotated[
        str | None, Header(alias=PROOF_HEADER, include_in_schema=False)
    ] = None,
) -> CommercialOrderMutationResponse:
    if order_id != payload.order_id:
        raise HTTPException(status_code=409, detail="order_id path and payload mismatch")
    access = build_access_context(session, current_user)
    review_order, _skus = review_commercial_order_append(session, payload, access)
    endpoint = f"/inventory/commercial-orders/{order_id}/dispatches"
    review_claims = _verify_review(
        review_proof, actor_id=current_user.id, access=access,
        tenant_id=review_order.client_id, operation=_OP_ORDER_APPEND,
        endpoint=endpoint, payload=payload,
    )
    try:
        order, dispatches = append_commercial_order_dispatches(
            session,
            payload,
            user_uuid=current_user.id,
            access=access,
            request_id=getattr(request.state, "request_id", None),
            expected_tenant_id=review_order.client_id,
        )
    except Exception:
        audit_confirmation(
            current_user.id, _OP_ORDER_APPEND, "failed",
            confirmation_id=review_claims["jti"],
        )
        raise
    audit_confirmation(
        current_user.id, _OP_ORDER_APPEND, "confirmed",
        confirmation_id=review_claims["jti"],
    )
    return _order_mutation_response(order, dispatches)


# ── POST /inventory/orders/inbound ────────────────────────────────────────────


@router.post("/orders/inbound", response_model=MovementCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_inbound_order(
    request: Request,
    payload: StockEntryCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
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
        access=build_access_context(session, current_user),
        request_id=getattr(request.state, "request_id", None),
    )
    return MovementCreatedResponse(id=entry.id)


# ── POST /inventory/orders/outbound ──────────────────────────────────────────


@router.post(
    "/dispatch-reviews/outbound",
    response_model=DispatchReviewResponse,
    responses={200: _NO_STORE_RESPONSE},
)
def review_outbound_order(
    response: Response,
    payload: StockExitCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
    idempotency_key: Annotated[
        UUID,
        Header(
            alias="Idempotency-Key",
            description="Required stable UUID for the standalone dispatch attempt.",
        ),
    ],
) -> DispatchReviewResponse:
    access = build_access_context(session, current_user)
    sku = review_standalone_dispatch(session, payload, access)
    response.headers["Cache-Control"] = "no-store"
    return _review_response(
        actor_id=current_user.id, access=access, tenant_id=sku.client_id,
        operation=_OP_STANDALONE, endpoint=_PATH_STANDALONE,
        payload=payload, warehouse=payload.warehouse,
        sku_ids=[payload.sku_id], total_quantity=payload.quantity,
        dispatch_count=1,
        command_id=idempotency_key, idempotency_key=idempotency_key,
    )


@router.post(
    "/orders/outbound",
    response_model=MovementCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={428: {
        **_REVIEW_REQUIRED_RESPONSE,
        "description": (
            "For dispatch, the Idempotency-Key is missing or the review proof "
            "is missing, invalid, expired, or mismatched."
        ),
    }},
    openapi_extra={"parameters": [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": False,
            "description": (
                "Required UUID when exit_type is dispatch; omit for loss. "
                "Reuse it for retries of the same confirmed attempt."
            ),
            "schema": {"type": "string", "format": "uuid"},
        },
        {
            **_REVIEW_PROOF_PARAMETER,
            "required": False,
            "description": (
                "Required when exit_type is dispatch; unused for loss. Must come "
                "from POST /inventory/dispatch-reviews/outbound."
            ),
        },
    ]},
)
def create_outbound_order(
    request: Request,
    payload: StockExitCreate,
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
    idempotency_key: Annotated[
        UUID | None, Header(alias="Idempotency-Key", include_in_schema=False)
    ] = None,
    review_proof: Annotated[
        str | None, Header(alias=PROOF_HEADER, include_in_schema=False)
    ] = None,
) -> MovementCreatedResponse:
    """Register an outbound stock movement (stock exit).

    Delegates all business logic (SKU existence, warehouse mismatch,
    stock sufficiency, persistence) to ``create_stock_exit()`` — no
    duplication of domain rules in the router.

    The ``user_uuid`` is populated from the authenticated user's ID,
    **not** from the client payload.
    """
    access = build_access_context(session, current_user)
    reviewed_tenant_id = None
    if payload.exit_type.value == "dispatch":
        sku = review_standalone_dispatch(session, payload, access)
        reviewed_tenant_id = sku.client_id
        if idempotency_key is None:
            raise _review_error()
        review_claims = _verify_review(
            review_proof, actor_id=current_user.id, access=access,
            tenant_id=sku.client_id, operation=_OP_STANDALONE,
            endpoint=_PATH_STANDALONE, payload=payload,
            idempotency_key=idempotency_key,
        )
    try:
        exit_record = create_stock_exit(
            session=session,
            data=payload,
            user_uuid=current_user.id,
            access=access,
            request_id=getattr(request.state, "request_id", None),
            idempotency_key=idempotency_key,
            expected_tenant_id=reviewed_tenant_id,
        )
    except Exception:
        if payload.exit_type.value == "dispatch":
            audit_confirmation(
                current_user.id, _OP_STANDALONE, "failed",
                confirmation_id=review_claims["jti"],
            )
        raise
    if payload.exit_type.value == "dispatch":
        audit_confirmation(
            current_user.id, _OP_STANDALONE, "confirmed",
            confirmation_id=review_claims["jti"],
        )
    return MovementCreatedResponse(id=exit_record.id)


# ── GET /inventory/orders ───────────────────────────────────────────────────


@router.get(
    "/tracking/{tracking_number}",
    response_model=AuthorizedTrackingResponse,
)
def get_authorized_tracking(
    tracking_number: Annotated[
        str,
        Path(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$",
        ),
    ],
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> AuthorizedTrackingResponse:
    """Return only a tenant-authorized recorded dispatch projection."""
    return lookup_authorized_tracking(
        session,
        tracking_number=tracking_number,
        access=build_access_context(session, current_user),
    )


# ── GET /inventory/orders ───────────────────────────────────────────────────


@router.get(
    "/orders",
    response_model=list[InventoryOrderListItem],
)
def list_orders_endpoint(
    session: Annotated[Session, Depends(get_db)],
    current_user: Annotated[UserInDB, Depends(get_operational_user)],
) -> list[InventoryOrderListItem]:
    """Return all stock movements (entries + exits) with SKU data.

    Movements are combined in a single list sorted by ``created_at``
    ascending.  Each item includes the associated SKU details.

    This endpoint avoids N+1 lookups by bulk-loading all SKU records
    in a single query and mapping them in Python.

    Inventory reads require authentication.
    """
    access = build_access_context(session, current_user)
    cache_key = inventory_cache_key("orders", session, access.cache_scope)
    cached = orders_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = list_orders(session=session, access=access)
    except InventoryDataIntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Inventory data integrity error.",
        ) from exc

    result = _order_items_to_response(raw)
    orders_cache.set(cache_key, result)
    return result
