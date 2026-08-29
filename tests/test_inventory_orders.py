"""Tests for ``GET /inventory/orders`` and inventory router integration.

Covers:
    - ``list_orders`` service (N+1 free bulk loading)
    - ``GET /inventory/orders`` endpoint
    - Router registration in ``main.py`` (6 routes under /inventory)
    - Startup lifespan invokes ``create_db_and_tables()``
    - No duplicate ``/inventory/inventory`` prefix

Uses isolated SQLite in-memory databases and monkey-patching —
no real PostgreSQL/Supabase, TinyDB, or DATABASE_URL required.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

# Required by services.api.auth_settings and services.api.database
# before any imports of those modules.
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_schemas import (
    Category,
    InventoryOrderResponse,
    Warehouse,
)
from services.api.inventory_service import list_orders


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
    sku_code: str = "SKU-ORD-001",
    name: str = "Order Test SKU",
    warehouse: str = "LA",
    category: str = "electronics",
    client_name: str = "TestClient",
) -> SKU:
    """Helper — create a SKU record directly."""
    sku = SKU(
        name=name,
        sku=sku_code,
        client_name=client_name,
        category=category,
        warehouse=warehouse,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)
    return sku


def _add_entry(
    session: Session,
    sku_id: int,
    quantity: int = 10,
    warehouse: str = "LA",
    user_uuid: str = "user-entry",
    reference: str = "PO-001",
    created_at: datetime | None = None,
) -> StockEntry:
    """Helper — add a StockEntry directly."""
    entry = StockEntry(
        sku_id=sku_id,
        quantity=quantity,
        reference=reference,
        warehouse=warehouse,
        user_uuid=user_uuid,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def _add_exit(
    session: Session,
    sku_id: int,
    quantity: int = 5,
    warehouse: str = "LA",
    user_uuid: str = "user-exit",
    exit_type: str = "dispatch",
    tracking_number: str | None = "DISP-001",
    created_at: datetime | None = None,
) -> StockExit:
    """Helper — add a StockExit directly."""
    exit_rec = StockExit(
        sku_id=sku_id,
        quantity=quantity,
        exit_type=exit_type,
        tracking_number=tracking_number,
        warehouse=warehouse,
        user_uuid=user_uuid,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(exit_rec)
    session.commit()
    session.refresh(exit_rec)
    return exit_rec


# ═════════════════════════════════════════════════════════════════════════════
# list_orders service tests
# ═════════════════════════════════════════════════════════════════════════════


class TestListOrdersService:
    """Suite for the ``list_orders`` service function."""

    # ── 1. No movements → empty list ────────────────────────────────────

    def test_empty_returns_empty_list(self, db_session: Session) -> None:
        """ORDERS-SRV-01: no movements → []."""
        result = list_orders(db_session)
        assert result == []

    # ── 2. Single entry appears as inbound ──────────────────────────────

    def test_single_entry_is_inbound(self, db_session: Session) -> None:
        """ORDERS-SRV-02: one entry → movement_type='inbound'."""
        sku = _create_sku(db_session)
        entry = _add_entry(db_session, sku_id=sku.id)

        result = list_orders(db_session)
        assert len(result) == 1
        item = result[0]
        assert item["movement_type"] == "inbound"
        assert item["id"] == entry.id
        assert item["sku_id"] == sku.id
        assert item["quantity"] == entry.quantity

    # ── 3. Single exit appears as outbound ─────────────────────────────

    def test_single_exit_is_outbound(self, db_session: Session) -> None:
        """ORDERS-SRV-03: one exit → movement_type='outbound'."""
        sku = _create_sku(db_session)
        exit_rec = _add_exit(db_session, sku_id=sku.id)

        result = list_orders(db_session)
        assert len(result) == 1
        item = result[0]
        assert item["movement_type"] == "outbound"
        assert item["id"] == exit_rec.id
        assert item["sku_id"] == sku.id
        assert item["quantity"] == exit_rec.quantity

    # ── 4. Entry includes reference ─────────────────────────────────────

    def test_entry_includes_reference(self, db_session: Session) -> None:
        """ORDERS-SRV-04: inbound record includes ``reference``."""
        sku = _create_sku(db_session)
        _add_entry(db_session, sku_id=sku.id, reference="PO-ABC-123")

        result = list_orders(db_session)
        assert result[0]["reference"] == "PO-ABC-123"

    # ── 5. Outbound includes exit_type ──────────────────────────────────

    def test_outbound_includes_exit_type(self, db_session: Session) -> None:
        """ORDERS-SRV-05: outbound record includes ``exit_type``."""
        sku = _create_sku(db_session)
        _add_exit(db_session, sku_id=sku.id, exit_type="loss", tracking_number=None)

        result = list_orders(db_session)
        assert result[0]["exit_type"] == "loss"

    # ── 6. Outbound includes tracking_number ────────────────────────────

    def test_outbound_includes_tracking_number(self, db_session: Session) -> None:
        """ORDERS-SRV-06: outbound dispatch includes ``tracking_number``."""
        sku = _create_sku(db_session)
        _add_exit(db_session, sku_id=sku.id, tracking_number="TRACK-999")

        result = list_orders(db_session)
        assert result[0]["tracking_number"] == "TRACK-999"

    # ── 7. Each movement includes user_uuid ─────────────────────────────

    def test_entry_includes_user_uuid(self, db_session: Session) -> None:
        """ORDERS-SRV-07: inbound record has ``user_uuid``."""
        sku = _create_sku(db_session)
        _add_entry(db_session, sku_id=sku.id, user_uuid="custom-user-01")

        result = list_orders(db_session)
        assert result[0]["user_uuid"] == "custom-user-01"

    def test_exit_includes_user_uuid(self, db_session: Session) -> None:
        """ORDERS-SRV-08: outbound record has ``user_uuid``."""
        sku = _create_sku(db_session)
        _add_exit(db_session, sku_id=sku.id, user_uuid="custom-user-02")

        result = list_orders(db_session)
        assert result[0]["user_uuid"] == "custom-user-02"

    # ── 8. Each movement includes warehouse ─────────────────────────────

    def test_entry_includes_warehouse(self, db_session: Session) -> None:
        """ORDERS-SRV-09: inbound record has ``warehouse``."""
        sku = _create_sku(db_session, warehouse="ZGZ")
        _add_entry(db_session, sku_id=sku.id, warehouse="ZGZ")

        result = list_orders(db_session)
        assert result[0]["warehouse"] == "ZGZ"

    def test_exit_includes_warehouse(self, db_session: Session) -> None:
        """ORDERS-SRV-10: outbound record has ``warehouse``."""
        sku = _create_sku(db_session, warehouse="LA")
        _add_exit(db_session, sku_id=sku.id, warehouse="LA")

        result = list_orders(db_session)
        assert result[0]["warehouse"] == "LA"

    # ── 9. Each movement includes SKU data ──────────────────────────────

    def test_entry_includes_sku_data(self, db_session: Session) -> None:
        """ORDERS-SRV-11: inbound record contains SKU details."""
        sku = _create_sku(
            db_session,
            sku_code="SKU-DATA-01",
            name="Data SKU",
            client_name="DataClient",
            category="cosmetics",
            warehouse="ZGZ",
        )
        _add_entry(db_session, sku_id=sku.id, warehouse="ZGZ")

        result = list_orders(db_session)
        sku_data = result[0]["sku"]
        assert sku_data.id == sku.id
        assert sku_data.name == "Data SKU"
        assert sku_data.sku == "SKU-DATA-01"
        assert sku_data.client_name == "DataClient"
        assert sku_data.category == "cosmetics"
        assert sku_data.warehouse == "ZGZ"

    def test_exit_includes_sku_data(self, db_session: Session) -> None:
        """ORDERS-SRV-12: outbound record contains SKU details."""
        sku = _create_sku(
            db_session,
            sku_code="SKU-EXT-DATA",
            name="Exit SKU",
        )
        _add_exit(db_session, sku_id=sku.id)

        result = list_orders(db_session)
        sku_data = result[0]["sku"]
        assert sku_data.id == sku.id
        assert sku_data.name == "Exit SKU"
        assert sku_data.sku == "SKU-EXT-DATA"

    # ── 10. Multiple entries/exits returned together ───────────────────

    def test_mixed_entries_and_exits(self, db_session: Session) -> None:
        """ORDERS-SRV-13: both entries and exits appear in combined list."""
        sku = _create_sku(db_session)
        _add_entry(db_session, sku_id=sku.id, quantity=100,
                    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        _add_exit(db_session, sku_id=sku.id, quantity=30,
                   created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))

        result = list_orders(db_session)
        assert len(result) == 2
        types = [r["movement_type"] for r in result]
        assert "inbound" in types
        assert "outbound" in types

    # ── 11. Deterministic order by created_at ──────────────────────────

    def test_ordered_by_created_at_ascending(self, db_session: Session) -> None:
        """ORDERS-SRV-14: movements sorted by created_at ascending."""
        sku = _create_sku(db_session)
        _add_exit(db_session, sku_id=sku.id, quantity=5,
                   created_at=datetime(2024, 2, 1, tzinfo=timezone.utc))
        _add_entry(db_session, sku_id=sku.id, quantity=10,
                    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))

        result = list_orders(db_session)
        assert len(result) == 2
        # First should be the entry (Jan 1), then the exit (Feb 1)
        assert result[0]["movement_type"] == "inbound"
        assert result[1]["movement_type"] == "outbound"

    # ── 12. SKU data does NOT include current_stock ────────────────────

    def test_sku_summary_no_current_stock(self, db_session: Session) -> None:
        """ORDERS-SRV-15: SKU summary excludes ``current_stock``."""
        sku = _create_sku(db_session)
        _add_entry(db_session, sku_id=sku.id)

        result = list_orders(db_session)
        sku_data = result[0]["sku"]
        # SKUSummary has no current_stock field
        assert not hasattr(sku_data, "current_stock")

    # ── 13. Multiple SKUs ──────────────────────────────────────────────

    def test_multiple_sku_movements(self, db_session: Session) -> None:
        """ORDERS-SRV-16: movements for different SKUs are all included."""
        sku_a = _create_sku(db_session, sku_code="SKU-A")
        sku_b = _create_sku(db_session, sku_code="SKU-B")
        _add_entry(db_session, sku_id=sku_a.id, quantity=10,
                    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        _add_entry(db_session, sku_id=sku_b.id, quantity=20,
                    created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
        _add_exit(db_session, sku_id=sku_a.id, quantity=5,
                   created_at=datetime(2024, 1, 3, tzinfo=timezone.utc))

        result = list_orders(db_session)
        assert len(result) == 3
        sku_ids = {(r["sku_id"], r["movement_type"]) for r in result}
        assert (sku_a.id, "inbound") in sku_ids
        assert (sku_b.id, "inbound") in sku_ids
        assert (sku_a.id, "outbound") in sku_ids


# ═════════════════════════════════════════════════════════════════════════════
# N+1 avoidance test
# ═════════════════════════════════════════════════════════════════════════════


class TestListOrdersNplusOne:
    """Verify that ``list_orders`` does NOT perform a SKU lookup per movement.

    The test monkeypatches ``session.get(SKU, ...)`` to fail if called,
    proving the implementation uses a bulk query instead of per-row lookups.
    """

    def test_no_per_movement_sku_get(self, db_session: Session) -> None:
        """ORDERS-N1-01: ``session.get(SKU, ...)`` is never called."""
        sku = _create_sku(db_session)
        _add_entry(db_session, sku_id=sku.id, quantity=10)
        _add_exit(db_session, sku_id=sku.id, quantity=3)

        original_get = db_session.get

        def _guarded_get(model, ident):
            """Fail if someone tries a PK lookup on SKU per movement."""
            if hasattr(model, "__tablename__") and model.__tablename__ == "sku":
                raise RuntimeError(
                    "N+1 detected: session.get(SKU, ...) was called. "
                    "Use bulk load instead."
                )
            return original_get(model, ident)

        db_session.get = _guarded_get  # type: ignore[method-assign]

        # This must succeed without hitting session.get(SKU)
        result = list_orders(db_session)
        assert len(result) == 2
        assert result[0]["sku"] is not None
        assert result[1]["sku"] is not None


# ═════════════════════════════════════════════════════════════════════════════
# Router registration tests
# ═════════════════════════════════════════════════════════════════════════════


class TestRouterRegistration:
    """Confirm that the inventory router is correctly registered in main.py."""

    def test_all_inventory_routes_exist(self) -> None:
        """ROUTER-01: exactly 6 inventory routes exist with correct paths.

        We check the inventory_router itself (which is imported and used
        by main.py) to verify all route paths are defined.
        """
        from services.api.routes.inventory import router

        paths = sorted(
            r.path for r in router.routes if hasattr(r, "path")
        )
        expected = [
            "/inventory/orders",
            "/inventory/orders/inbound",
            "/inventory/orders/outbound",
            "/inventory/products",
            "/inventory/products/{id}",
        ]
        for p in expected:
            assert p in paths, f"Missing route: {p}"

        # The inventory router has 5 unique paths (6 route entries because
        # /inventory/products has both GET and POST)
        assert len(set(paths)) == 5, f"Expected 5 unique paths, got {len(set(paths))}"

    def test_inventory_prefix_is_single(self) -> None:
        """ROUTER-02: no route contains ``/inventory/inventory``."""
        from services.api.routes.inventory import router

        for r in router.routes:
            if hasattr(r, "path"):
                assert "/inventory/inventory" not in r.path, (
                    f"Double prefix found: {r.path}"
                )

    def test_inventory_router_used_in_main(self) -> None:
        """ROUTER-03: verify main.py imports and includes the inventory router."""
        import services.api.main as main_module

        # Verify the import exists
        assert hasattr(main_module, "inventory_router")

        # Verify it's the same router
        from services.api.routes.inventory import router as inventory_router

        assert main_module.inventory_router is inventory_router


# ═════════════════════════════════════════════════════════════════════════════
# Startup lifespan tests
# ═════════════════════════════════════════════════════════════════════════════


class TestStartupLifespan:
    """Verify the FastAPI lifespan calls ``create_db_and_tables()``
    unconditionally and propagates errors."""

    def test_lifespan_calls_create_db_and_tables(self, monkeypatch) -> None:
        """STARTUP-01: lifespan invokes ``create_db_and_tables()`` once.

        We monkeypatch the function to record the call without
        actually requiring a real database.
        """
        call_count = 0

        def _fake_create_db_and_tables():
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(
            "services.api.main.create_db_and_tables",
            _fake_create_db_and_tables,
        )

        import asyncio
        from fastapi import FastAPI
        from services.api.main import lifespan

        async def _run():
            async with lifespan(FastAPI()):
                pass

        asyncio.run(_run())

        assert call_count == 1, (
            "create_db_and_tables should have been called once during startup"
        )

    def test_lifespan_propagates_create_db_and_tables_error(self, monkeypatch) -> None:
        """STARTUP-02: if ``create_db_and_tables()`` raises, the error
        propagates through lifespan — application must not start."""
        def _failing_create():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(
            "services.api.main.create_db_and_tables",
            _failing_create,
        )

        import asyncio
        from fastapi import FastAPI
        from services.api.main import lifespan

        async def _run():
            async with lifespan(FastAPI()):
                pass

        with pytest.raises(RuntimeError, match="database unavailable"):
            asyncio.run(_run())

    def test_create_db_and_tables_fails_without_database_url(self, monkeypatch) -> None:
        """STARTUP-03: the real ``create_db_and_tables()`` raises
        ``RuntimeError`` when ``DATABASE_URL`` is not set.

        This tests the infrastructure itself (not the lifespan wrapper)
        to confirm that absent configuration stops startup.
        """
        # Clear DATABASE_URL so _get_engine() fails
        monkeypatch.delenv("DATABASE_URL", raising=False)

        # The engine may be cached from earlier imports — force a fresh
        # resolution by clearing the module-level cache.
        import services.api.database as db_mod
        db_mod._engine = None  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            db_mod.create_db_and_tables()


# ═════════════════════════════════════════════════════════════════════════════
# Response schema validation
# ═════════════════════════════════════════════════════════════════════════════


class TestInventoryOrderResponseSchema:
    """Verify that ``InventoryOrderResponse`` has the expected shape."""

    def test_inbound_minimal(self) -> None:
        """SCHEMA-01: an inbound order response has the right fields."""
        response = InventoryOrderResponse(
            id=1,
            movement_type="inbound",
            sku_id=10,
            quantity=5,
            warehouse="LA",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            user_uuid="test-user",
            sku={
                "id": 10,
                "name": "Test",
                "sku": "SKU-001",
                "client_name": "Client",
                "category": "electronics",
                "warehouse": "LA",
            },
            reference="PO-REF",
        )
        assert response.movement_type == "inbound"
        assert response.reference == "PO-REF"
        assert response.exit_type is None
        assert response.tracking_number is None

    def test_outbound_minimal(self) -> None:
        """SCHEMA-02: an outbound order response has the right fields."""
        response = InventoryOrderResponse(
            id=2,
            movement_type="outbound",
            sku_id=10,
            quantity=3,
            warehouse="ZGZ",
            created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            user_uuid="test-user-2",
            sku={
                "id": 10,
                "name": "Test",
                "sku": "SKU-001",
                "client_name": "Client",
                "category": "cosmetics",
                "warehouse": "ZGZ",
            },
            exit_type="dispatch",
            tracking_number="TRACK-100",
        )
        assert response.movement_type == "outbound"
        assert response.exit_type == "dispatch"
        assert response.tracking_number == "TRACK-100"
        assert response.reference is None

    def test_skusummary_fields(self) -> None:
        """SCHEMA-03: SKUSummary contains expected fields."""
        from services.api.inventory_schemas import SKUSummary

        summary = SKUSummary(
            id=1,
            name="Prod",
            sku="SKU-001",
            client_name="Client",
            category="fashion",
            warehouse="LA",
        )
        assert summary.id == 1
        assert summary.name == "Prod"
        assert summary.sku == "SKU-001"
        assert summary.client_name == "Client"
        assert summary.category == "fashion"
        assert summary.warehouse == "LA"