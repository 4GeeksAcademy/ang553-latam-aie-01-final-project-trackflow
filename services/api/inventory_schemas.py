"""
TrackFlow Inventory Schemas — Pydantic v2.

Request/response schemas for the inventory API, separated from the ORM models
defined in ``inventory_models.py``.

Validates domain constraints:
    - category in {fashion, electronics, cosmetics}
    - warehouse in {LA, ZGZ}
    - exit_type in {dispatch, loss}
    - quantity > 0
    - tracking_number required for dispatch, forbidden for loss
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from services.api.inventory_clients import validate_client_pair


# ── Domain enumerations ─────────────────────────────────────────────────────


class Category(str, Enum):
    """Valid product categories in TrackFlow."""

    FASHION = "fashion"
    ELECTRONICS = "electronics"
    COSMETICS = "cosmetics"


class Warehouse(str, Enum):
    """Valid warehouse locations in TrackFlow."""

    LA = "LA"
    ZGZ = "ZGZ"


class ExitType(str, Enum):
    """Valid stock exit types in TrackFlow."""

    DISPATCH = "dispatch"
    LOSS = "loss"


# ── SKU schemas ─────────────────────────────────────────────────────────────


class SKUCreate(BaseModel):
    """Payload for creating a new SKU.

    The client provides product metadata only.
    ``id`` and ``current_stock`` are never accepted from the client.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    sku: str
    client_id: str
    client_name: str
    category: Category
    warehouse: Warehouse
    minimum_stock_threshold: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def _validate_client_identity(self) -> SKUCreate:
        """Require a known, authoritative ID matching the display name."""
        validate_client_pair(self.client_id, self.client_name)
        return self


class SKUResponse(BaseModel):
    """Public representation of a SKU, including computed stock.

    ``current_stock`` is populated by the backend (not a persisted column).
    Compatible with ``from_attributes`` for ORM-to-schema conversion.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sku: str
    client_id: str | None
    client_name: str
    category: Category
    warehouse: Warehouse
    minimum_stock_threshold: int | None
    current_stock: int


class SKUThresholdUpdate(BaseModel):
    """Configure or disable a SKU threshold after product creation."""

    model_config = ConfigDict(extra="forbid")

    minimum_stock_threshold: int | None = Field(ge=0, strict=True)


# ── StockEntry schemas ───────────────────────────────────────────────────────


class StockEntryCreate(BaseModel):
    """Payload for recording a stock-in movement.

    ``user_uuid`` is **not** accepted from the client — it is set by
    the backend from the authenticated user's identity.
    """

    model_config = ConfigDict(extra="forbid")

    sku_id: int
    quantity: int = Field(gt=0)
    reference: str
    warehouse: Warehouse


class StockEntryResponse(BaseModel):
    """Public representation of a stock-in movement."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_id: int
    quantity: int
    reference: str
    warehouse: Warehouse
    created_at: datetime
    user_uuid: str


class MovementCreatedResponse(BaseModel):
    """Minimal response returned after creating a stock movement."""

    id: int


# ── StockExit schemas ────────────────────────────────────────────────────────


class StockExitCreate(BaseModel):
    """Payload for recording a stock-out movement.

    ``tracking_number`` is required when ``exit_type`` is ``dispatch``
    and must be ``None`` when ``exit_type`` is ``loss``.

    ``user_uuid`` is **not** accepted from the client — it is set by
    the backend from the authenticated user's identity.
    """

    model_config = ConfigDict(extra="forbid")

    sku_id: int
    quantity: int = Field(gt=0)
    exit_type: ExitType
    tracking_number: Optional[str] = None
    warehouse: Warehouse

    @model_validator(mode="after")
    def _validate_tracking_number(self) -> StockExitCreate:
        """Enforce the tracking-number contract per exit type.

        - ``dispatch`` requires a non-null tracking number.
        - ``loss`` requires a null tracking number.
        """
        if self.exit_type == ExitType.DISPATCH:
            if not self.tracking_number:
                raise ValueError(
                    "tracking_number is required when exit_type is 'dispatch'"
                )
        elif self.exit_type == ExitType.LOSS:
            if self.tracking_number is not None:
                raise ValueError(
                    "tracking_number must be None when exit_type is 'loss'"
                )
        return self


class StockExitResponse(BaseModel):
    """Public representation of a stock-out movement."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_id: int
    quantity: int
    exit_type: ExitType
    tracking_number: Optional[str] = None
    warehouse: Warehouse
    created_at: datetime
    user_uuid: str
    commercial_order_id: int | None = None


class CommercialOrderDispatchCreate(BaseModel):
    """One dispatch partial associated with a stable commercial order."""

    model_config = ConfigDict(extra="forbid")

    dispatch_id: UUID4
    sku_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)
    tracking_number: str = Field(min_length=1)


class CommercialOrderCreate(BaseModel):
    """Idempotent order command with at least one initial dispatch partial."""

    model_config = ConfigDict(extra="forbid")

    order_id: UUID4
    client_id: str = Field(min_length=1)
    warehouse: Warehouse
    dispatches: list[CommercialOrderDispatchCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_dispatch_ids(self) -> CommercialOrderCreate:
        ids = [item.dispatch_id for item in self.dispatches]
        if len(ids) != len(set(ids)):
            raise ValueError("dispatch_id values must be unique within a command")
        return self


class CommercialOrderDispatchAppend(BaseModel):
    """Append partial dispatches without creating another order event."""

    model_config = ConfigDict(extra="forbid")

    order_id: UUID4
    dispatches: list[CommercialOrderDispatchCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_dispatch_ids(self) -> CommercialOrderDispatchAppend:
        ids = [item.dispatch_id for item in self.dispatches]
        if len(ids) != len(set(ids)):
            raise ValueError("dispatch_id values must be unique within a command")
        return self


class CommercialOrderResponse(BaseModel):
    """Persisted commercial identity shared by one or more dispatch exits."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: UUID
    client_id: str
    warehouse: Warehouse
    created_at: datetime


class CommercialOrderMutationResponse(BaseModel):
    """Stable order identity and the complete persisted dispatch-id set."""

    id: int
    order_id: UUID
    dispatch_ids: list[UUID]


class InventoryPhysicalCountCreate(BaseModel):
    """One observed count; expected stock is derived by the service."""

    model_config = ConfigDict(extra="forbid")

    count_id: UUID4
    sku_id: int
    observed_quantity: int = Field(ge=0, strict=True)
    warehouse: Warehouse


class InventoryPhysicalCountResponse(BaseModel):
    """Durable physical-count evidence with stable public UUID."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    count_id: UUID
    sku_id: int
    client_id: str
    warehouse: Warehouse
    expected_quantity: int
    observed_quantity: int
    confirmed_at: datetime
    user_uuid: str


# ── Order (combined) schemas ───────────────────────────────────────────────


class SKUSummary(BaseModel):
    """Lightweight SKU representation included inside order responses.

    This intentionally excludes ``current_stock`` — stock should not
    be computed per-movement.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sku: str
    client_name: str
    category: Category
    warehouse: Warehouse


class InventoryOrderResponse(BaseModel):
    """Unified response for a stock movement (inbound or outbound).

    Fields
    ------
    id : int
        Primary key of the movement record.
    movement_type : str
        ``"inbound"`` for StockEntry, ``"outbound"`` for StockExit.
    sku_id : int
        Foreign key to the associated SKU.
    quantity : int
        Number of units moved.
    warehouse : Warehouse
        Warehouse where the movement occurred.
    created_at : datetime
        Timestamp of the movement.
    user_uuid : str
        UUID of the user who recorded the movement.
    sku : SKUSummary
        Lightweight data of the associated SKU.
    reference : str | None
        Purchase order / inbound reference (inbound only).
    exit_type : str | None
        Type of exit (outbound only).
    tracking_number : str | None
        Carrier tracking number (outbound only).
    """

    id: int
    movement_type: str  # "inbound" | "outbound"
    sku_id: int
    quantity: int
    warehouse: Warehouse
    created_at: datetime
    user_uuid: str
    sku: SKUSummary
    reference: Optional[str] = None
    exit_type: Optional[str] = None


class InventoryOrderListItem(BaseModel):
    """Flat, consumer-focused representation of an inventory movement."""

    id: int
    movement_type: str
    quantity: int
    warehouse: Warehouse
    created_at: datetime
    user_uuid: str
    sku_name: str
    sku_code: str
    reference: Optional[str] = None
    exit_type: Optional[str] = None
    tracking_number: Optional[str] = None
