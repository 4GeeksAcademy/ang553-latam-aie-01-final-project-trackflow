"""
TrackFlow Inventory ORM Models — SQLModel.

Persistent models for inventory, commercial orders, and physical counts.

Coexists with TinyDB auth — no SQLModel User table is created.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utc_now() -> datetime:
    """Return the current UTC datetime.

    Used as ``default_factory`` for ``created_at`` fields so that
    the timestamp is evaluated at row-insert time, not at import time.
    """
    return datetime.now(timezone.utc)


# ── SKU ──────────────────────────────────────────────────────────────────────


class SKU(SQLModel, table=True):
    """A unique product/SKU identified by its code.

    Persisted fields
    ----------------
    id : int
        Auto-increment primary key.
    name : str
        Human-readable product name.
    sku : str
        Unique SKU code.
    client_name : str
        Client/brand that owns this SKU.
    client_id : str | None
        Stable internal identifier for the owning client. Nullable temporarily
        while legacy rows are backfilled.
    minimum_stock_threshold : int | None
        Non-negative alert threshold. ``None`` disables threshold events.
    category : str
        Product category (fashion, electronics, cosmetics).
    warehouse : str
        Warehouse where this SKU is stored (LA, ZGZ).

    Note
    ----
    ``current_stock`` is **not** stored as a column.
    Stock is computed on the fly as:

        SUM(StockEntry.quantity) - SUM(StockExit.quantity)

    filtered by SKU and warehouse.
    """

    __tablename__: str = "sku"

    __table_args__ = (
        CheckConstraint(
            "minimum_stock_threshold IS NULL OR minimum_stock_threshold >= 0",
            name="ck_sku_minimum_stock_threshold_nonnegative",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str
    sku: str
    client_name: str
    category: str
    warehouse: str
    client_id: str | None = None
    minimum_stock_threshold: int | None = Field(default=None)

    # (stock is calculated, not stored — see inventory_service)


# ── StockEntry ───────────────────────────────────────────────────────────────


class StockEntry(SQLModel, table=True):
    """A stock-in movement (inbound receipt).

    Persisted fields
    ----------------
    id : int
        Auto-increment primary key.
    sku_id : int
        Foreign key -> SKU.id.
    quantity : int
        Number of units received.
    reference : str
        Purchase order / inbound reference.
    warehouse : str
        Warehouse where the stock was received.
    created_at : datetime
        Automatically set to UTC now on creation.
    user_uuid : str
        UUID of the TinyDB user who recorded the entry.
        No SQL FK — TinyDB manages users.
    """

    __tablename__: str = "stockentry"

    id: int | None = Field(default=None, primary_key=True)
    sku_id: int = Field(foreign_key="sku.id")
    quantity: int
    reference: str
    warehouse: str
    created_at: datetime = Field(default_factory=_utc_now)
    user_uuid: str

    # (FK only — no ORM back-reference)


# ── StockExit ────────────────────────────────────────────────────────────────


class StockExit(SQLModel, table=True):
    """A stock-out movement (dispatch, loss, etc.).

    Persisted fields
    ----------------
    id : int
        Auto-increment primary key.
    sku_id : int
        Foreign key -> SKU.id.
    quantity : int
        Number of units dispatched.
    exit_type : str
        Type of exit (dispatch, loss, etc.).
    tracking_number : str | None
        Carrier tracking number (nullable for losses).
    warehouse : str
        Warehouse from which stock exited.
    created_at : datetime
        Automatically set to UTC now on creation.
    user_uuid : str
        UUID of the TinyDB user who recorded the exit.
        No SQL FK — TinyDB manages users.
    commercial_order_id : int | None
        Optional migration-stage FK to the commercial order. When present, the
        exit must be a dispatch; one order may own multiple dispatches.
    """

    __tablename__: str = "stockexit"

    id: int | None = Field(default=None, primary_key=True)
    sku_id: int = Field(foreign_key="sku.id")
    quantity: int
    exit_type: str
    tracking_number: str | None = Field(default=None)
    warehouse: str
    created_at: datetime = Field(default_factory=_utc_now)
    user_uuid: str
    commercial_order_id: int | None = Field(
        default=None,
        foreign_key="commercial_order.id",
    )
    dispatch_id: UUID | None = Field(default=None)

    __table_args__ = (
        CheckConstraint(
            "commercial_order_id IS NULL OR exit_type = 'dispatch'",
            name="ck_stockexit_order_only_for_dispatch",
        ),
        UniqueConstraint("dispatch_id", name="uq_stockexit_dispatch_id"),
        CheckConstraint(
            "commercial_order_id IS NULL OR dispatch_id IS NOT NULL",
            name="ck_stockexit_order_requires_dispatch_id",
        ),
    )

    # (FK only — no ORM back-reference)


class CommercialOrder(SQLModel, table=True):
    """Commercial order identity; one order may own multiple dispatch exits."""

    __tablename__: str = "commercial_order"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_commercial_order_order_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    order_id: UUID = Field(default_factory=uuid4, nullable=False)
    client_id: str
    warehouse: str
    created_at: datetime = Field(default_factory=_utc_now)


class InventoryPhysicalCount(SQLModel, table=True):
    """Confirmed physical count; recording it never adjusts computed stock."""

    __tablename__: str = "inventory_physical_count"
    __table_args__ = (
        UniqueConstraint(
            "count_id",
            name="uq_inventory_physical_count_count_id",
        ),
        CheckConstraint(
            "expected_quantity >= 0",
            name="ck_inventory_physical_count_expected_nonnegative",
        ),
        CheckConstraint(
            "observed_quantity >= 0",
            name="ck_inventory_physical_count_observed_nonnegative",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    count_id: UUID = Field(default_factory=uuid4, nullable=False)
    sku_id: int = Field(foreign_key="sku.id")
    client_id: str
    warehouse: str
    expected_quantity: int
    observed_quantity: int
    confirmed_at: datetime = Field(default_factory=_utc_now)
    user_uuid: str


class InventoryThresholdEvaluation(SQLModel, table=True):
    """Durable threshold decision for every inbound or outbound movement."""

    __tablename__: str = "inventory_threshold_evaluation"
    __table_args__ = (
        UniqueConstraint(
            "source_table", "source_pk", name="uq_threshold_evaluation_source"
        ),
        CheckConstraint(
            "source_table IN ('stockentry', 'stockexit')",
            name="ck_threshold_evaluation_source_table",
        ),
        CheckConstraint(
            "before_quantity >= 0 AND after_quantity >= 0",
            name="ck_threshold_evaluation_quantities_nonnegative",
        ),
        CheckConstraint(
            "threshold_quantity IS NULL OR threshold_quantity >= 0",
            name="ck_threshold_evaluation_threshold_nonnegative",
        ),
        CheckConstraint(
            "(crossed = true AND event_id IS NOT NULL) OR "
            "(crossed = false AND event_id IS NULL)",
            name="ck_threshold_evaluation_crossed_event",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    source_table: str
    source_pk: int
    sku_id: int = Field(foreign_key="sku.id")
    client_id: str | None = None
    warehouse: str
    before_quantity: int
    after_quantity: int
    threshold_quantity: int | None = None
    crossed: bool
    event_id: UUID | None = None
    evaluated_at: datetime = Field(default_factory=_utc_now)
