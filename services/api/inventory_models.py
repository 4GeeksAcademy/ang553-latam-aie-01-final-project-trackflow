"""
TrackFlow Inventory ORM Models — SQLModel.

Persistent models for SKU, StockEntry, and StockExit.

Coexists with TinyDB auth — no SQLModel User table is created.
"""

from datetime import datetime, timezone

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

    id: int | None = Field(default=None, primary_key=True)
    name: str
    sku: str
    client_name: str
    category: str
    warehouse: str

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

    # (FK only — no ORM back-reference)
