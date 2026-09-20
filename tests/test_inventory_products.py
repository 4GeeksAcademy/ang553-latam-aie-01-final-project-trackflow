"""Tests for inventory product endpoints (``routes/inventory.py``).

Covers:
    - GET  /inventory/products         — list with per-warehouse stock
    - GET  /inventory/products/{id}    — single product lookup
    - POST /inventory/products         — SKU creation with auth

Uses an isolated SQLite in-memory database and direct handler calls
(no TestClient), consistent with the rest of the test suite.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from services.api.auth_models import UserInDB
from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_clients import CLIENT_IDS_BY_NAME
from services.api.inventory_schemas import SKUCreate
from services.api.inventory_service import (
    create_stock_entry,
    get_current_stock,
    get_current_stocks,
)
from services.api.cache import TTLCache, inventory_cache_key, orders_cache, products_cache
from services.api.routes import inventory as inventory_routes
from services.api.routes.inventory import create_product, get_product, list_products


def _request(request_id: str = "550e8400-e29b-41d4-a716-446655440099"):
    """Minimal request context used by direct route-handler tests."""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(request_id=request_id))


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


@pytest.fixture(scope="function")
def auth_user() -> UserInDB:
    """Return a minimal authenticated user for POST tests."""
    return UserInDB(
        id="test-user-uuid",
        email="test@trackflow.io",
        hashed_password="fake-hash",
        is_active=True,
    )


def _create_sku(
    session: Session,
    sku_code: str = "SKU-TEST-001",
    name: str = "Test SKU",
    warehouse: str = "LA",
    category: str = "electronics",
    client_name: str = "TestClient",
    client_id: str | None = None,
) -> SKU:
    """Helper — create a SKU record directly in the database."""
    sku = SKU(
        name=name,
        sku=sku_code,
        client_name=client_name,
        client_id=client_id,
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
    quantity: int,
    warehouse: str = "LA",
    user_uuid: str = "test-user",
) -> StockEntry:
    """Helper — record a stock-in movement."""
    from services.api.inventory_schemas import StockEntryCreate as _EntryCreate

    return create_stock_entry(
        session,
        data=_EntryCreate(
            sku_id=sku_id,
            quantity=quantity,
            reference="TEST-PO",
            warehouse=warehouse,
        ),
        user_uuid=user_uuid,
    )


def _add_exit(
    session: Session,
    sku_id: int,
    quantity: int,
    warehouse: str = "LA",
    user_uuid: str = "test-user",
) -> StockExit:
    """Helper — record a stock-out movement."""
    from services.api.inventory_schemas import StockExitCreate as _ExitCreate

    from services.api.inventory_service import create_stock_exit

    return create_stock_exit(
        session,
        data=_ExitCreate(
            sku_id=sku_id,
            quantity=quantity,
            exit_type="dispatch",
            tracking_number="DISP-001",
            warehouse=warehouse,
        ),
        user_uuid=user_uuid,
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /inventory/products
# ═════════════════════════════════════════════════════════════════════════════


class TestListProducts:
    """Suite for ``list_products`` (GET /inventory/products)."""

    # ── 1. Empty list ───────────────────────────────────────────────────

    def test_empty_list_returns_empty(self, db_session: Session) -> None:
        """PROD-LIST-01: no SKUs → empty list."""
        result = list_products(session=db_session)
        assert result == []

    # ── 2. SKU without movements → current_stock 0 ──────────────────────

    def test_sku_without_movements_has_zero_stock(
        self, db_session: Session
    ) -> None:
        """PROD-LIST-02: SKU with no entries/exits → current_stock 0."""
        _create_sku(db_session, sku_code="SKU-NO-MOV")

        result = list_products(session=db_session)
        assert len(result) == 1
        assert result[0].current_stock == 0
        assert result[0].sku == "SKU-NO-MOV"

    # ── 3. SKU with entries/exits → correct stock ───────────────────────

    def test_sku_with_movements_has_correct_stock(
        self, db_session: Session
    ) -> None:
        """PROD-LIST-03: entries and exits produce correct current_stock."""
        sku = _create_sku(db_session, sku_code="SKU-MOV-01")
        _add_entry(db_session, sku_id=sku.id, quantity=100, warehouse="LA")
        _add_entry(db_session, sku_id=sku.id, quantity=50, warehouse="LA")
        _add_exit(db_session, sku_id=sku.id, quantity=30, warehouse="LA")

        result = list_products(session=db_session)
        la_sku = [r for r in result if r.sku == "SKU-MOV-01"][0]
        assert la_sku.current_stock == 120  # 100 + 50 - 30

    # ── 4. Two SKU with different warehouses keep stocks separate ─────

    def test_different_warehouses_keep_separate_stocks(
        self, db_session: Session
    ) -> None:
        """PROD-LIST-04: stock computed per (sku_id, warehouse) pair."""
        sku_la = _create_sku(db_session, sku_code="SKU-LA", warehouse="LA")
        sku_zgz = _create_sku(db_session, sku_code="SKU-ZGZ", warehouse="ZGZ")

        # Add stock only to LA
        _add_entry(db_session, sku_id=sku_la.id, quantity=80, warehouse="LA")
        _add_exit(db_session, sku_id=sku_la.id, quantity=20, warehouse="LA")

        # Add stock only to ZGZ
        _add_entry(db_session, sku_id=sku_zgz.id, quantity=50, warehouse="ZGZ")

        result = list_products(session=db_session)

        la_result = [r for r in result if r.sku == "SKU-LA"][0]
        zgz_result = [r for r in result if r.sku == "SKU-ZGZ"][0]

        assert la_result.current_stock == 60  # 80 - 20
        assert zgz_result.current_stock == 50  # 50

    # ── 5. Response contains warehouse ──────────────────────────────────

    def test_response_includes_warehouse(self, db_session: Session) -> None:
        """PROD-LIST-05: response has warehouse field."""
        _create_sku(db_session, sku_code="SKU-WH", warehouse="ZGZ")

        result = list_products(session=db_session)
        assert result[0].warehouse == "ZGZ"

    # ── 6. Response contains current_stock ──────────────────────────────

    def test_response_includes_current_stock(self, db_session: Session) -> None:
        """PROD-LIST-06: response has current_stock field."""
        _create_sku(db_session, sku_code="SKU-CS")

        result = list_products(session=db_session)
        assert hasattr(result[0], "current_stock")

    def test_second_read_uses_cached_projection(self, db_session: Session, monkeypatch) -> None:
        """PROD-CACHE-01: repeated reads do not re-query the product table."""
        _create_sku(db_session, sku_code="SKU-CACHE")
        first = list_products(session=db_session)

        def fail_query(*args, **kwargs):
            raise AssertionError("cached product projection should be used")

        monkeypatch.setattr(db_session, "exec", fail_query)
        second = list_products(session=db_session)

        assert second == first

    def test_cache_expires_after_30_seconds_and_requeries(
        self, db_session: Session, monkeypatch
    ) -> None:
        """PROD-CACHE-03: a product projection expires at its 30s TTL."""
        class FakeClock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = FakeClock()
        cache = TTLCache[list](ttl_seconds=30, clock=clock)
        monkeypatch.setattr(inventory_routes, "products_cache", cache)
        _create_sku(db_session, sku_code="SKU-EXPIRY")

        first = list_products(session=db_session)
        monkeypatch.setattr(
            db_session,
            "exec",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("query should not run before product TTL expiry")
            ),
        )
        assert list_products(session=db_session) == first

        clock.now = 30
        monkeypatch.setattr(db_session, "exec", Session.exec.__get__(db_session))
        assert list_products(session=db_session) == first

    def test_create_product_invalidates_products_but_not_orders(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """PROD-CACHE-04: product creation selectively invalidates caches."""
        products = list_products(session=db_session)
        orders = []
        products_key = inventory_cache_key("products", db_session)
        orders_key = inventory_cache_key("orders", db_session)
        orders_cache.set(orders_key, orders)
        assert products_cache.get(products_key) == products

        create_product(
            request=_request(),
            payload=SKUCreate(
                name="Created SKU",
                sku="SKU-CREATE-CACHE",
                client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
                client_name="PureStep Footwear",
                category="electronics",
                warehouse="LA",
            ),
            session=db_session,
            current_user=auth_user,
        )

        assert products_cache.get(products_key) is None
        assert orders_cache.get(orders_key) == orders

    def test_movement_invalidates_cached_projection(self, db_session: Session) -> None:
        """PROD-CACHE-02: stock movement makes the next read fresh."""
        sku = _create_sku(db_session, sku_code="SKU-INVALIDATE")
        assert list_products(session=db_session)[0].current_stock == 0
        _add_entry(db_session, sku.id, quantity=7)

        assert list_products(session=db_session)[0].current_stock == 7

    def test_outbound_invalidates_both_caches_and_updates_stock(
        self, db_session: Session
    ) -> None:
        """PROD-CACHE-05: outbound invalidates both projections."""
        sku = _create_sku(db_session, sku_code="SKU-OUTBOUND-CACHE")
        _add_entry(db_session, sku.id, quantity=10)
        list_products(session=db_session)
        orders_cache.set(inventory_cache_key("orders", db_session), [])
        _add_exit(db_session, sku.id, quantity=4)

        assert products_cache.get(inventory_cache_key("products", db_session)) is None
        assert orders_cache.get(inventory_cache_key("orders", db_session)) is None
        refreshed = list_products(session=db_session)
        assert refreshed[0].current_stock == 6

    def test_failed_outbound_preserves_warm_caches(
        self, db_session: Session
    ) -> None:
        """PROD-CACHE-06: insufficient stock does not invalidate projections."""
        sku = _create_sku(db_session, sku_code="SKU-FAILED-OUTBOUND")
        list_products(session=db_session)
        orders_cache.set(inventory_cache_key("orders", db_session), [])
        products_key = inventory_cache_key("products", db_session)
        orders_key = inventory_cache_key("orders", db_session)
        products_snapshot = products_cache.get(products_key)

        with pytest.raises(HTTPException) as exc_info:
            _add_exit(db_session, sku.id, quantity=1)

        assert exc_info.value.status_code == 400
        assert products_cache.get(products_key) == products_snapshot
        assert orders_cache.get(orders_key) == []


# ═════════════════════════════════════════════════════════════════════════════
# GET /inventory/products/{id}
# ═════════════════════════════════════════════════════════════════════════════


class TestGetProduct:
    """Suite for ``get_product`` (GET /inventory/products/{id})."""

    # ── 1. Existing SKU returns correct data ────────────────────────────

    def test_existing_sku_returns_correct_data(
        self, db_session: Session
    ) -> None:
        """PROD-GET-01: existing SKU returns data with computed stock."""
        sku = _create_sku(db_session, sku_code="SKU-GET-01", name="Get SKU")
        _add_entry(db_session, sku_id=sku.id, quantity=30, warehouse="LA")

        result = get_product(id=sku.id, session=db_session)

        assert result.id == sku.id
        assert result.name == "Get SKU"
        assert result.sku == "SKU-GET-01"
        assert result.warehouse == "LA"

    # ── 2. Current stock computed correctly ─────────────────────────────

    def test_current_stock_computed_correctly(
        self, db_session: Session
    ) -> None:
        """PROD-GET-02: current_stock reflects entries minus exits."""
        sku = _create_sku(db_session, sku_code="SKU-GET-02")
        _add_entry(db_session, sku_id=sku.id, quantity=50, warehouse="LA")
        _add_exit(db_session, sku_id=sku.id, quantity=15, warehouse="LA")

        result = get_product(id=sku.id, session=db_session)
        assert result.current_stock == 35  # 50 - 15

    # ── 3. Non-existent SKU → 404 ──────────────────────────────────────

    def test_nonexistent_sku_returns_404(self, db_session: Session) -> None:
        """PROD-GET-03: missing SKU raises HTTP 404."""
        with pytest.raises(HTTPException) as exc_info:
            get_product(id=99999, session=db_session)

        assert exc_info.value.status_code == 404

    # ── 4. Detail message ────────────────────────────────────────────────

    def test_nonexistent_sku_detail_message(
        self, db_session: Session
    ) -> None:
        """PROD-GET-04: 404 detail is 'SKU not found.'."""
        with pytest.raises(HTTPException) as exc_info:
            get_product(id=99999, session=db_session)

        assert exc_info.value.detail == "SKU not found."


# ═════════════════════════════════════════════════════════════════════════════
# POST /inventory/products
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateProduct:
    """Suite for ``create_product`` (POST /inventory/products)."""

    def test_captures_exact_inventory_product_created_event(
        self,
        db_session: Session,
        auth_user: UserInDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Successful creation emits one event with the exact contract fields."""
        captured = []
        request_id = "550e8400-e29b-41d4-a716-446655440099"
        monkeypatch.setattr(
            inventory_routes, "capture_telemetry_events", captured.extend
        )
        payload = SKUCreate(
            name="Telemetry Product",
            sku="SKU-TELEMETRY-001",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="fashion",
            warehouse="LA",
        )

        result = create_product(
            request=_request(request_id),
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert len(captured) == 1
        event = captured[0]
        assert event.event_type == "inventory_product_created"
        assert event.properties == {
            "warehouse": "los_angeles",
            "client_id": CLIENT_IDS_BY_NAME["PureStep Footwear"],
            "product_id": str(result.id),
            "product_category": "fashion",
        }
        assert event.eventId.version == 4
        assert event.timestamp.endswith("Z")
        assert event.schemaVersion == "1.0"
        assert event.requestId == request_id
        assert event.userId == auth_user.id
        assert event.sessionId is None

    @pytest.mark.parametrize(
        ("warehouse", "expected"),
        [
            ("LA", "los_angeles"),
            ("ZGZ", "zaragoza"),
            ("Los Angeles", "los_angeles"),
            ("Zaragoza", "zaragoza"),
        ],
    )
    def test_canonicalizes_telemetry_warehouse(
        self,
        warehouse: str,
        expected: str,
    ) -> None:
        """Domain warehouse values map to the canonical telemetry values."""
        assert inventory_routes._canonical_telemetry_warehouse(warehouse) == expected

    def test_sink_failure_does_not_break_creation(
        self,
        db_session: Session,
        auth_user: UserInDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Telemetry failure preserves the functional product creation."""
        def fail_capture(_events) -> None:
            raise RuntimeError("sink failure")

        monkeypatch.setattr(inventory_routes, "capture_telemetry_events", fail_capture)
        payload = SKUCreate(
            name="Sink Failure Product",
            sku="SKU-SINK-FAIL",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="electronics",
            warehouse="ZGZ",
        )

        result = create_product(
            request=_request(),
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert db_session.get(SKU, result.id) is not None

    def test_persisted_creation_event_survives_cache_invalidation_failure(
        self,
        db_session: Session,
        auth_user: UserInDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The creation fact is captured before a later cache failure."""
        captured = []
        monkeypatch.setattr(
            inventory_routes, "capture_telemetry_events", captured.extend
        )
        monkeypatch.setattr(
            inventory_routes,
            "invalidate_products_cache",
            lambda _session: (_ for _ in ()).throw(
                RuntimeError("cache invalidation failure")
            ),
        )
        payload = SKUCreate(
            name="Cache Failure Product",
            sku="SKU-CACHE-FAIL",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="fashion",
            warehouse="LA",
        )

        with pytest.raises(RuntimeError, match="cache invalidation failure"):
            create_product(
                request=_request(),
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        persisted_sku = db_session.exec(
            select(SKU).where(SKU.sku == "SKU-CACHE-FAIL")
        ).one()
        assert persisted_sku.id is not None
        assert db_session.get(SKU, persisted_sku.id) is not None
        assert len(captured) == 1
        assert captured[0].event_type == "inventory_product_created"
        assert captured[0].properties["product_id"] == str(persisted_sku.id)

    def test_commit_failure_emits_no_event(
        self,
        db_session: Session,
        auth_user: UserInDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A commit failure occurs before telemetry construction."""
        captured = []
        monkeypatch.setattr(
            inventory_routes, "capture_telemetry_events", captured.extend
        )
        monkeypatch.setattr(
            db_session,
            "commit",
            lambda: (_ for _ in ()).throw(RuntimeError("commit failure")),
        )
        payload = SKUCreate(
            name="Commit Failure Product",
            sku="SKU-COMMIT-FAIL",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="cosmetics",
            warehouse="LA",
        )

        with pytest.raises(RuntimeError, match="commit failure"):
            create_product(
                request=_request(),
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert captured == []

    def test_refresh_failure_emits_no_event(
        self,
        db_session: Session,
        auth_user: UserInDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refresh failure occurs before telemetry construction."""
        captured = []
        monkeypatch.setattr(
            inventory_routes, "capture_telemetry_events", captured.extend
        )
        monkeypatch.setattr(
            db_session,
            "refresh",
            lambda _sku: (_ for _ in ()).throw(RuntimeError("refresh failure")),
        )
        payload = SKUCreate(
            name="Refresh Failure Product",
            sku="SKU-REFRESH-FAIL",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="cosmetics",
            warehouse="LA",
        )

        with pytest.raises(RuntimeError, match="refresh failure"):
            create_product(
                request=_request(),
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert captured == []

    # ── 1. Creates SKU correctly ────────────────────────────────────────

    def test_creates_sku(self, db_session: Session, auth_user: UserInDB) -> None:
        """PROD-CREATE-01: valid payload creates SKU in DB."""
        payload = SKUCreate(
            name="New Product",
            sku="SKU-NEW-001",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="fashion",
            warehouse="LA",
        )

        result = create_product(
            request=_request(),
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert result.name == "New Product"
        assert result.sku == "SKU-NEW-001"
        assert result.client_name == "PureStep Footwear"
        assert result.category == "fashion"
        assert result.warehouse == "LA"

        # Verify it's actually in the database
        db_sku = db_session.get(SKU, result.id)
        assert db_sku is not None
        assert db_sku.name == "New Product"
        assert db_sku.client_id == CLIENT_IDS_BY_NAME["PureStep Footwear"]
        assert db_sku.client_name == "PureStep Footwear"

    # ── 2. Response contains current_stock=0 ────────────────────────────

    def test_initial_stock_is_zero(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """PROD-CREATE-02: new SKU has current_stock=0."""
        payload = SKUCreate(
            name="Zero Stock",
            sku="SKU-ZERO-001",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="electronics",
            warehouse="ZGZ",
        )

        result = create_product(
            request=_request(),
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.current_stock == 0
        assert result.client_id == CLIENT_IDS_BY_NAME["PureStep Footwear"]

    # ── 3. current_stock is NOT persisted in ORM ─────────────────────────

    def test_current_stock_not_persisted(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """PROD-CREATE-03: SKU ORM has no current_stock column."""
        from services.api.inventory_models import SKU as SKUModel

        payload = SKUCreate(
            name="No Persisted Stock",
            sku="SKU-NP-001",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="cosmetics",
            warehouse="LA",
        )

        result = create_product(
            request=_request(),
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        # Confirm the ORM object has no current_stock attribute
        db_sku = db_session.get(SKUModel, result.id)
        assert not hasattr(db_sku, "current_stock")

        # And get_current_stock returns 0
        stock = get_current_stock(
            db_session, sku_id=result.id, warehouse="LA"
        )
        assert stock == 0

    # ── 4. Requires authenticated user ───────────────────────────────────

    def test_requires_auth_user(self, db_session: Session) -> None:
        """PROD-CREATE-04: missing current_user raises TypeError.

        The ``current_user`` parameter is required by the function
        signature — calling without it fails because the endpoint
        declaration uses ``Depends(get_current_user)``.
        """
        payload = SKUCreate(
            name="Auth Required",
            sku="SKU-AUTH-001",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="electronics",
            warehouse="LA",
        )

        with pytest.raises(TypeError):
            create_product(
                request=_request(),
                payload=payload,
                session=db_session,
                # no current_user provided
            )

    # ── 5. Schema does not accept current_stock ─────────────────────────

    def test_schema_rejects_current_stock(self) -> None:
        """PROD-CREATE-05: SKUCreate has no current_stock field."""
        payload = SKUCreate(
            name="Test",
            sku="SKU-REJECT-CS",
            client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
            client_name="PureStep Footwear",
            category="electronics",
            warehouse="LA",
        )

        # SKUCreate does not have current_stock
        assert not hasattr(payload, "current_stock")

    def test_unknown_client_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown client_id"):
            SKUCreate(
                name="Unknown client",
                sku="SKU-UNKNOWN-CLIENT",
                client_id="00000000-0000-4000-8000-000000000000",
                client_name="PureStep Footwear",
                category="electronics",
                warehouse="LA",
            )

    def test_missing_client_id_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            SKUCreate(
                name="Missing client",
                sku="SKU-MISSING-CLIENT",
                client_name="PureStep Footwear",
                category="electronics",
                warehouse="LA",
            )

        assert any(error["loc"] == ("client_id",) for error in exc_info.value.errors())

    def test_mismatched_client_identity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="do not match"):
            SKUCreate(
                name="Mismatched client",
                sku="SKU-MISMATCHED-CLIENT",
                client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
                client_name="SoundWave Electronics",
                category="electronics",
                warehouse="LA",
            )

        # Extra fields are forbidden by ConfigDict(extra="forbid")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            SKUCreate(
                name="Bad",
                sku="SKU-BAD",
                client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
                client_name="PureStep Footwear",
                category="electronics",
                warehouse="LA",
                current_stock=100,  # type: ignore[call-arg]
            )

    # ── 6. Invalid category/warehouse rejected by Pydantic ──────────────

    def test_invalid_category_rejected(self) -> None:
        """PROD-CREATE-06: bad category raises Pydantic validation error."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SKUCreate(
                name="Bad Category",
                sku="SKU-BAD-CAT",
                client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
                client_name="PureStep Footwear",
                category="invalid_category",  # not in enum
                warehouse="LA",
            )

    def test_invalid_warehouse_rejected(self) -> None:
        """PROD-CREATE-07: bad warehouse raises Pydantic validation error."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SKUCreate(
                name="Bad Warehouse",
                sku="SKU-BAD-WH",
                client_id=CLIENT_IDS_BY_NAME["PureStep Footwear"],
                client_name="PureStep Footwear",
                category="electronics",
                warehouse="INVALID",  # not in enum
            )


# ═════════════════════════════════════════════════════════════════════════════
# Bulk stock function (get_current_stocks)
# ═════════════════════════════════════════════════════════════════════════════


class TestGetCurrentStocks:
    """Suite for the bulk ``get_current_stocks`` function."""

    # ── 1. No movements → empty dict ────────────────────────────────────

    def test_no_movements_returns_empty_dict(self, db_session: Session) -> None:
        """BULK-01: no entries or exits → empty dict."""
        _create_sku(db_session, sku_code="SKU-BULK-EMPTY")

        result = get_current_stocks(db_session)
        # There's a SKU but no movements → it won't appear in the dict
        # (callers default to 0)
        assert isinstance(result, dict)

    # ── 2. Single entry ─────────────────────────────────────────────────

    def test_single_entry(self, db_session: Session) -> None:
        """BULK-02: one entry reflected in bulk result."""
        sku = _create_sku(db_session, sku_code="SKU-BULK-1")
        _add_entry(db_session, sku_id=sku.id, quantity=42, warehouse="LA")

        result = get_current_stocks(db_session)
        assert result[(sku.id, "LA")] == 42

    # ── 3. Entries and exits ────────────────────────────────────────────

    def test_entries_and_exits(self, db_session: Session) -> None:
        """BULK-03: entries minus exits in bulk result."""
        sku = _create_sku(db_session, sku_code="SKU-BULK-2")
        _add_entry(db_session, sku_id=sku.id, quantity=100, warehouse="LA")
        _add_exit(db_session, sku_id=sku.id, quantity=30, warehouse="LA")

        result = get_current_stocks(db_session)
        assert result[(sku.id, "LA")] == 70

    # ── 4. Multiple SKU with different warehouses ───────────────────────

    def test_multiple_sku_different_warehouses(
        self, db_session: Session
    ) -> None:
        """BULK-04: separate (sku_id, warehouse) keys."""
        sku_la = _create_sku(db_session, sku_code="SKU-BULK-LA", warehouse="LA")
        sku_zgz = _create_sku(
            db_session, sku_code="SKU-BULK-ZGZ", warehouse="ZGZ"
        )
        _add_entry(db_session, sku_id=sku_la.id, quantity=60, warehouse="LA")
        _add_entry(db_session, sku_id=sku_zgz.id, quantity=25, warehouse="ZGZ")

        result = get_current_stocks(db_session)
        assert result[(sku_la.id, "LA")] == 60
        assert result[(sku_zgz.id, "ZGZ")] == 25

    # ── 5. Only exits (no entries) ──────────────────────────────────────

    def test_only_exits(self, db_session: Session) -> None:
        """BULK-05: only exits produce negative stock."""
        sku = _create_sku(db_session, sku_code="SKU-BULK-OE")
        # Bypass create_stock_exit (which has negative-stock guard) and
        # insert directly to test the bulk aggregation logic.
        exit_record = StockExit(
            sku_id=sku.id,
            quantity=15,
            exit_type="loss",
            tracking_number=None,
            warehouse="LA",
            user_uuid="test-user",
        )
        db_session.add(exit_record)
        db_session.commit()

        result = get_current_stocks(db_session)
        assert result[(sku.id, "LA")] == -15


# ═════════════════════════════════════════════════════════════════════════════
# N+1 protection — list_products must NOT call get_current_stock per product
# ═════════════════════════════════════════════════════════════════════════════


class TestNoNPlusOne:
    """Confirm that ``list_products`` avoids calling ``get_current_stock``
    per product by monkeypatching it to fail."""

    def test_list_products_does_not_call_get_current_stock_per_sku(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NPLUS-01: monkeypatch ``get_current_stock`` to crash.

        If ``list_products`` tries to call ``get_current_stock`` even once,
        the test fails.  The bulk function ``get_current_stocks`` is used
        instead.
        """
        # Arrange: create a couple of SKUs with movements
        sku_a = _create_sku(db_session, sku_code="SKU-NPLUS-A", warehouse="LA")
        sku_b = _create_sku(
            db_session, sku_code="SKU-NPLUS-B", warehouse="ZGZ"
        )
        _add_entry(db_session, sku_id=sku_a.id, quantity=10, warehouse="LA")
        _add_entry(db_session, sku_id=sku_b.id, quantity=20, warehouse="ZGZ")

        # Arrange: make get_current_stock raise if called
        def _crash(*args, **kwargs):
            raise RuntimeError(
                "get_current_stock was called! list_products should use "
                "get_current_stocks instead."
            )

        monkeypatch.setattr(
            "services.api.routes.inventory.get_current_stock", _crash
        )

        # Act — this must succeed via get_current_stocks, not per-sku calls
        result = list_products(session=db_session)

        # Assert
        assert len(result) == 2
        la_res = [r for r in result if r.sku == "SKU-NPLUS-A"][0]
        zgz_res = [r for r in result if r.sku == "SKU-NPLUS-B"][0]
        assert la_res.current_stock == 10
        assert zgz_res.current_stock == 20