"""Tests for inventory order endpoints (``routes/inventory.py``).

Covers:
    - POST /inventory/orders/inbound    — stock entry (inbound)
    - POST /inventory/orders/outbound   — stock exit (outbound)

Uses an isolated SQLite in-memory database and direct handler calls
(no TestClient), consistent with the rest of the test suite.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, create_engine, select

from services.api.auth_models import UserInDB
from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_schemas import (
    ExitType,
    StockEntryCreate,
    StockEntryResponse,
    StockExitCreate,
    StockExitResponse,
    Warehouse,
)
from services.api.inventory_service import (
    create_stock_entry,
    create_stock_exit,
    get_current_stock,
)
from services.api.routes.inventory import (
    create_inbound_order,
    create_outbound_order,
)


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


@pytest.fixture(scope="function")
def second_auth_user() -> UserInDB:
    """Return a second authenticated user for identity tests."""
    return UserInDB(
        id="second-user-uuid",
        email="second@trackflow.io",
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
) -> SKU:
    """Helper — create a SKU record directly in the database."""
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


# ═════════════════════════════════════════════════════════════════════════════
# POST /inventory/orders/inbound
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateInboundOrder:
    """Suite for ``create_inbound_order`` (POST /inventory/orders/inbound)."""

    # ── 1. Creates StockEntry correctly ─────────────────────────────────

    def test_creates_stock_entry(self, db_session: Session, auth_user: UserInDB) -> None:
        """INBOUND-01: valid payload creates StockEntry in DB."""
        sku = _create_sku(db_session, sku_code="INB-SKU-01")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=50,
            reference="PO-12345",
            warehouse=Warehouse.LA,
        )

        result = create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert result.sku_id == sku.id
        assert result.quantity == 50
        assert result.reference == "PO-12345"
        assert result.warehouse == Warehouse.LA

        # Verify it's persisted in the database
        db_entry = db_session.get(StockEntry, result.id)
        assert db_entry is not None
        assert db_entry.quantity == 50
        assert db_entry.reference == "PO-12345"

    # ── 2. Response contains all expected fields ─────────────────────────

    def test_response_contains_all_fields(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-02: response has id, sku_id, quantity, reference, warehouse,
        created_at, user_uuid."""
        sku = _create_sku(db_session, sku_code="INB-SKU-02")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=10,
            reference="PO-REF",
            warehouse=Warehouse.LA,
        )

        result = create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert result.sku_id == sku.id
        assert result.quantity == 10
        assert result.reference == "PO-REF"
        assert result.warehouse == Warehouse.LA
        assert result.created_at is not None
        assert result.user_uuid is not None

    # ── 3. Response contains warehouse ──────────────────────────────────

    def test_response_includes_warehouse(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-03: response has warehouse field."""
        sku = _create_sku(db_session, sku_code="INB-SKU-03", warehouse="ZGZ")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=5,
            reference="PO-WH",
            warehouse=Warehouse.ZGZ,
        )

        result = create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.warehouse == Warehouse.ZGZ

    # ── 4. user_uuid matches current_user.id ────────────────────────────

    def test_user_uuid_matches_current_user(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-04: user_uuid in response equals current_user.id."""
        sku = _create_sku(db_session, sku_code="INB-SKU-04")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=20,
            reference="PO-UUID",
            warehouse=Warehouse.LA,
        )

        result = create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.user_uuid == auth_user.id

    # ── 5. Client cannot decide user_uuid ───────────────────────────────

    def test_user_uuid_not_in_schema(self) -> None:
        """INBOUND-05: StockEntryCreate has no user_uuid field."""
        payload = StockEntryCreate(
            sku_id=1,
            quantity=10,
            reference="PO-NO-UUID",
            warehouse=Warehouse.LA,
        )
        assert not hasattr(payload, "user_uuid")

    # ── 6. SKU inexistente → 404 ────────────────────────────────────────

    def test_nonexistent_sku_returns_404(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-06: missing SKU raises HTTP 404."""
        payload = StockEntryCreate(
            sku_id=99999,
            quantity=10,
            reference="PO-NO-SKU",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException) as exc_info:
            create_inbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "SKU not found."

    # ── 7. Warehouse mismatch → 400 ─────────────────────────────────────

    def test_warehouse_mismatch_returns_400(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-07: mismatched warehouse raises HTTP 400."""
        sku = _create_sku(db_session, sku_code="INB-SKU-07", warehouse="ZGZ")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=10,
            reference="PO-MISMATCH",
            warehouse=Warehouse.LA,  # does not match sku.warehouse = "ZGZ"
        )

        with pytest.raises(HTTPException) as exc_info:
            create_inbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.status_code == 400
        assert "Warehouse mismatch" in exc_info.value.detail

    # ── 8. Valid entry increases current_stock ─────────────────────────

    def test_valid_entry_increases_stock(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-08: after a valid entry, get_current_stock increases."""
        sku = _create_sku(db_session, sku_code="INB-SKU-08")

        # Stock before entry
        before = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert before == 0

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=75,
            reference="PO-STOCK-UP",
            warehouse=Warehouse.LA,
        )

        create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        after = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert after == 75

    # ── 9. Requires authenticated user ──────────────────────────────────

    def test_requires_auth_user(self, db_session: Session) -> None:
        """INBOUND-09: missing current_user raises TypeError.

        The ``current_user`` parameter is required by the function
        signature — calling without it fails because the endpoint
        declaration uses ``Depends(get_current_user)``.
        """
        sku = _create_sku(db_session, sku_code="INB-SKU-09")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=10,
            reference="PO-AUTH",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(TypeError):
            create_inbound_order(
                payload=payload,
                session=db_session,
                # no current_user provided
            )

    # ── 10. Response is not direct ORM ─────────────────────────────────

    def test_response_is_not_direct_orm(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """INBOUND-10: response is a StockEntryResponse, not a raw ORM."""
        sku = _create_sku(db_session, sku_code="INB-SKU-10")

        payload = StockEntryCreate(
            sku_id=sku.id,
            quantity=10,
            reference="PO-NO-ORM",
            warehouse=Warehouse.LA,
        )

        result = create_inbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert isinstance(result, StockEntryResponse)
        assert not isinstance(result, StockEntry)  # not a raw ORM


# ═════════════════════════════════════════════════════════════════════════════
# POST /inventory/orders/outbound
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateOutboundOrder:
    """Suite for ``create_outbound_order`` (POST /inventory/orders/outbound)."""

    # ── 11. Creates StockExit correctly ─────────────────────────────────

    def test_creates_stock_exit(self, db_session: Session, auth_user: UserInDB) -> None:
        """OUTBOUND-01: valid payload creates StockExit in DB."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-11")

        # Pre-load stock so the exit can succeed
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=100,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=30,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-001",
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert result.sku_id == sku.id
        assert result.quantity == 30
        assert result.exit_type == ExitType.DISPATCH
        assert result.tracking_number == "DISP-001"
        assert result.warehouse == Warehouse.LA

        # Verify it's persisted in the database
        db_exit = db_session.get(StockExit, result.id)
        assert db_exit is not None
        assert db_exit.quantity == 30
        assert db_exit.exit_type == "dispatch"

    # ── 12. Response contains all fields ────────────────────────────────

    def test_response_contains_all_fields(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-02: response has id, sku_id, quantity, exit_type,
        tracking_number, warehouse, created_at, user_uuid."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-12")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-002",
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.id is not None
        assert result.sku_id == sku.id
        assert result.quantity == 10
        assert result.exit_type == ExitType.DISPATCH
        assert result.tracking_number == "DISP-002"
        assert result.warehouse == Warehouse.LA
        assert result.created_at is not None
        assert result.user_uuid is not None

    # ── 13. user_uuid matches current_user.id ───────────────────────────

    def test_user_uuid_matches_current_user(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-03: user_uuid in response equals current_user.id."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-13")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-UUID",
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.user_uuid == auth_user.id

    # ── 14. Valid exit reduces stock ────────────────────────────────────

    def test_valid_exit_reduces_stock(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-04: after a valid exit, get_current_stock decreases."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-14")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=100,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        before = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert before == 100

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=40,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-REDUCE",
            warehouse=Warehouse.LA,
        )

        create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        after = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert after == 60  # 100 - 40

    # ── 15. available == requested works ────────────────────────────────

    def test_exact_stock_works(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-05: exit when available == requested succeeds."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-15")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=50,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-EXACT",
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.quantity == 50

        # Stock is now 0
        after = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert after == 0

    # ── 16. Insufficient stock → HTTP 400 ───────────────────────────────

    def test_insufficient_stock_returns_400(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-06: exit exceeding available stock raises HTTP 400."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-16")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=20,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=30,  # only 20 available
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-INSUF",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException) as exc_info:
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.status_code == 400

    # ── 17. Detail of insufficient stock matches exactly ────────────────

    def test_insufficient_stock_detail_matches(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-07: insufficient stock detail is exact."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-17")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=20,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=30,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-DETAIL",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException) as exc_info:
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.detail == (
            f"Insufficient stock for SKU 'OUT-SKU-17'. "
            f"Available: 20, requested: 30."
        )

    # ── 18. Rejection does not persist StockExit ───────────────────────

    def test_rejection_does_not_persist(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-08: failed exit does not create a StockExit row."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-18")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=10,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=99,  # only 10 available
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-NOPERSIST",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException):
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        # No StockExit should exist
        exits = db_session.exec(select(StockExit)).all()
        assert len(exits) == 0

    # ── 19. Stock remains intact after rejection ────────────────────────

    def test_stock_unchanged_after_rejection(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-09: stock is unchanged after a failed exit."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-19")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=10,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        before = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=99,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-NOCHANGE",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException):
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        after = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert after == before

    # ── 20. SKU inexistente → 404 ──────────────────────────────────────

    def test_nonexistent_sku_returns_404(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-10: missing SKU raises HTTP 404."""
        payload = StockExitCreate(
            sku_id=99999,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-NOSKU",
            warehouse=Warehouse.LA,
        )

        with pytest.raises(HTTPException) as exc_info:
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "SKU not found."

    # ── 21. Warehouse mismatch → 400 ───────────────────────────────────

    def test_warehouse_mismatch_returns_400(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-11: mismatched warehouse raises HTTP 400."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-21", warehouse="ZGZ")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.ZGZ,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="DISP-MISMATCH",
            warehouse=Warehouse.LA,  # does not match sku.warehouse = "ZGZ"
        )

        with pytest.raises(HTTPException) as exc_info:
            create_outbound_order(
                payload=payload,
                session=db_session,
                current_user=auth_user,
            )

        assert exc_info.value.status_code == 400
        assert "Warehouse mismatch" in exc_info.value.detail

    # ── 22. Valid dispatch with tracking works ─────────────────────────

    def test_dispatch_with_tracking_succeeds(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-12: dispatch with tracking_number succeeds."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-22")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="TRACK-001",
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.exit_type == ExitType.DISPATCH
        assert result.tracking_number == "TRACK-001"

    # ── 23. Valid loss with tracking None works ─────────────────────────

    def test_loss_without_tracking_succeeds(
        self, db_session: Session, auth_user: UserInDB
    ) -> None:
        """OUTBOUND-13: loss exit with tracking_number=None succeeds."""
        sku = _create_sku(db_session, sku_code="OUT-SKU-23")
        create_stock_entry(
            session=db_session,
            data=StockEntryCreate(
                sku_id=sku.id,
                quantity=50,
                reference="PO-SEED",
                warehouse=Warehouse.LA,
            ),
            user_uuid=auth_user.id,
        )

        payload = StockExitCreate(
            sku_id=sku.id,
            quantity=5,
            exit_type=ExitType.LOSS,
            tracking_number=None,
            warehouse=Warehouse.LA,
        )

        result = create_outbound_order(
            payload=payload,
            session=db_session,
            current_user=auth_user,
        )

        assert result.exit_type == ExitType.LOSS
        assert result.tracking_number is None


# ═════════════════════════════════════════════════════════════════════════════
# Pydantic validation tests for StockExitCreate
# ═════════════════════════════════════════════════════════════════════════════


class TestStockExitCreateValidation:
    """Validation rules enforced by StockExitCreate Pydantic schema."""

    # ── 24. Dispatch without tracking → validation error ────────────────

    def test_dispatch_without_tracking_raises_validation_error(self) -> None:
        """SCHEMA-01: dispatch without tracking_number raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StockExitCreate(
                sku_id=1,
                quantity=10,
                exit_type=ExitType.DISPATCH,
                tracking_number=None,
                warehouse=Warehouse.LA,
            )

        errors = exc_info.value.errors()
        assert any(
            "tracking_number is required when exit_type is 'dispatch'" in str(e["msg"])
            for e in errors
        )

    def test_dispatch_without_tracking_field_raises_validation_error(self) -> None:
        """SCHEMA-01b: dispatch without tracking_number field raises error."""
        with pytest.raises(ValidationError):
            StockExitCreate(
                sku_id=1,
                quantity=10,
                exit_type=ExitType.DISPATCH,
                # tracking_number omitted
                warehouse=Warehouse.LA,
            )

    # ── 25. Loss with tracking not-null → validation error ──────────────

    def test_loss_with_tracking_raises_validation_error(self) -> None:
        """SCHEMA-02: loss with non-null tracking_number raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            StockExitCreate(
                sku_id=1,
                quantity=10,
                exit_type=ExitType.LOSS,
                tracking_number="should-not-exist",
                warehouse=Warehouse.LA,
            )

        errors = exc_info.value.errors()
        assert any(
            "tracking_number must be None when exit_type is 'loss'" in str(e["msg"])
            for e in errors
        )

    # ── 26. quantity <= 0 → validation error ────────────────────────────

    def test_quantity_zero_raises_validation_error(self) -> None:
        """SCHEMA-03: quantity=0 raises ValidationError."""
        with pytest.raises(ValidationError):
            StockExitCreate(
                sku_id=1,
                quantity=0,
                exit_type=ExitType.DISPATCH,
                tracking_number="TRACK-001",
                warehouse=Warehouse.LA,
            )

    def test_quantity_negative_raises_validation_error(self) -> None:
        """SCHEMA-04: quantity=-1 raises ValidationError."""
        with pytest.raises(ValidationError):
            StockExitCreate(
                sku_id=1,
                quantity=-1,
                exit_type=ExitType.DISPATCH,
                tracking_number="TRACK-001",
                warehouse=Warehouse.LA,
            )

    # ── user_uuid not in schema ─────────────────────────────────────────

    def test_user_uuid_not_in_stock_exit_create(self) -> None:
        """SCHEMA-05: StockExitCreate has no user_uuid field."""
        payload = StockExitCreate(
            sku_id=1,
            quantity=10,
            exit_type=ExitType.DISPATCH,
            tracking_number="TRACK-001",
            warehouse=Warehouse.LA,
        )
        assert not hasattr(payload, "user_uuid")