"""Tests for ``inventory_service.get_current_stock``.

Uses an isolated SQLite in-memory database so no real PostgreSQL/Supabase
or TinyDB is affected.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_service import get_current_stock


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="function")
def db_session() -> Session:
    """Provide an isolated SQLite in-memory session for each test.

    All SQLModel tables are created before the test and dropped
    automatically when the session/engine goes out of scope.
    """
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _create_sku(
    session: Session,
    sku_code: str = "SKU-TEST-001",
    name: str = "Test SKU",
    warehouse: str = "LA",
) -> SKU:
    """Helper — create the minimal SKU record needed for FK constraints."""
    sku = SKU(
        name=name,
        sku=sku_code,
        client_name="TestClient",
        category="electronics",
        warehouse=warehouse,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)
    return sku


# ═════════════════════════════════════════════════════════════════════════════
# Tests
# ═════════════════════════════════════════════════════════════════════════════


class TestGetCurrentStock:
    """Suite for ``get_current_stock`` behaviour."""

    # ── 1. No movements ──────────────────────────────────────────────────

    def test_no_movements_returns_zero(self, db_session: Session) -> None:
        """INVENTORY-STOCK-01: SKU with no entries/exits → 0."""
        sku = _create_sku(db_session, sku_code="SKU-NO-MOV")

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == 0

    # ── 2. Single entry ─────────────────────────────────────────────────

    def test_single_entry_returns_entry_quantity(self, db_session: Session) -> None:
        """INVENTORY-STOCK-02: one entry, no exits → entry quantity."""
        sku = _create_sku(db_session, sku_code="SKU-ONE-ENTRY")
        db_session.add(
            StockEntry(sku_id=sku.id, quantity=10, reference="PO-001",
                       warehouse="LA", user_uuid="u1")
        )
        db_session.commit()

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == 10

    # ── 3. Multiple entries ─────────────────────────────────────────────

    def test_multiple_entries_sums_all(self, db_session: Session) -> None:
        """INVENTORY-STOCK-03: 10 + 15 = 25."""
        sku = _create_sku(db_session, sku_code="SKU-MULTI-ENTRY")
        db_session.add_all([
            StockEntry(sku_id=sku.id, quantity=10, reference="PO-001",
                       warehouse="LA", user_uuid="u1"),
            StockEntry(sku_id=sku.id, quantity=15, reference="PO-002",
                       warehouse="LA", user_uuid="u1"),
        ])
        db_session.commit()

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == 25

    # ── 4. Entries and one exit ─────────────────────────────────────────

    def test_entries_minus_exit(self, db_session: Session) -> None:
        """INVENTORY-STOCK-04: 10 + 15 - 4 = 21."""
        sku = _create_sku(db_session, sku_code="SKU-ENTRY-EXIT")
        db_session.add_all([
            StockEntry(sku_id=sku.id, quantity=10, reference="PO-001",
                       warehouse="LA", user_uuid="u1"),
            StockEntry(sku_id=sku.id, quantity=15, reference="PO-002",
                       warehouse="LA", user_uuid="u1"),
        ])
        db_session.add(
            StockExit(sku_id=sku.id, quantity=4, exit_type="dispatch",
                      tracking_number="TRK-001", warehouse="LA", user_uuid="u1")
        )
        db_session.commit()

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == 21

    # ── 5. LA / ZGZ separation (same SKU) ───────────────────────────────

    def test_warehouse_isolation_la_vs_zgz(self, db_session: Session) -> None:
        """INVENTORY-STOCK-05: LA and ZGZ stocks do not mix.

        LA:  entries 20, exits 3  → 17
        ZGZ: entries 50, exits 10 → 40

        The same ``sku_id`` is used for both warehouses.
        """
        sku = _create_sku(db_session, sku_code="SKU-LA-ZGZ")

        # LA movements
        db_session.add_all([
            StockEntry(sku_id=sku.id, quantity=20, reference="PO-LA-01",
                       warehouse="LA", user_uuid="u1"),
            StockExit(sku_id=sku.id, quantity=3, exit_type="dispatch",
                      tracking_number="TRK-LA-01", warehouse="LA", user_uuid="u1"),
        ])
        # ZGZ movements
        db_session.add_all([
            StockEntry(sku_id=sku.id, quantity=50, reference="PO-ZGZ-01",
                       warehouse="ZGZ", user_uuid="u1"),
            StockExit(sku_id=sku.id, quantity=10, exit_type="dispatch",
                      tracking_number="TRK-ZGZ-01", warehouse="ZGZ", user_uuid="u1"),
        ])
        db_session.commit()

        stock_la = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        stock_zgz = get_current_stock(db_session, sku_id=sku.id, warehouse="ZGZ")

        assert stock_la == 17, f"Expected LA=17, got {stock_la}"
        assert stock_zgz == 40, f"Expected ZGZ=40, got {stock_zgz}"

    # ── 6. Multiple exits ────────────────────────────────────────────────

    def test_multiple_exits_all_subtracted(self, db_session: Session) -> None:
        """INVENTORY-STOCK-06: entries minus multiple exits.

        30 - 4 - 6 = 20
        """
        sku = _create_sku(db_session, sku_code="SKU-MULTI-EXIT")
        db_session.add(
            StockEntry(sku_id=sku.id, quantity=30, reference="PO-001",
                       warehouse="LA", user_uuid="u1")
        )
        db_session.add_all([
            StockExit(sku_id=sku.id, quantity=4, exit_type="dispatch",
                      tracking_number="TRK-001", warehouse="LA", user_uuid="u1"),
            StockExit(sku_id=sku.id, quantity=6, exit_type="loss",
                      warehouse="LA", user_uuid="u1"),
        ])
        db_session.commit()

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == 20

    # ── 7. Only exits (no entries) ───────────────────────────────────────

    def test_only_exits_returns_negative_stock(self, db_session: Session) -> None:
        """INVENTORY-STOCK-07: exits with no entries → negative stock."""
        sku = _create_sku(db_session, sku_code="SKU-ONLY-EXIT")
        db_session.add(
            StockExit(sku_id=sku.id, quantity=5, exit_type="dispatch",
                      tracking_number="TRK-001", warehouse="LA", user_uuid="u1")
        )
        db_session.commit()

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        assert stock == -5

    # ── 8. Unknown sku_id returns zero ───────────────────────────────────

    def test_unknown_sku_id_returns_zero(self, db_session: Session) -> None:
        """INVENTORY-STOCK-08: non-existent ``sku_id`` → 0."""
        stock = get_current_stock(db_session, sku_id=99999, warehouse="LA")

        assert stock == 0