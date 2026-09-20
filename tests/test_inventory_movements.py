"""Tests for ``inventory_service`` movement functions.

Covers:
    - ``create_stock_entry`` (StockEntry registration)
    - ``create_stock_exit`` (StockExit registration with negative-stock guard)

Uses an isolated SQLite in-memory database so no real PostgreSQL/Supabase
or TinyDB is affected.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from services.api.inventory_models import SKU, StockEntry, StockExit
from services.api.inventory_schemas import StockEntryCreate, StockExitCreate
from services.api.inventory_service import (
    create_stock_entry,
    create_stock_exit,
    get_current_stock,
)
import services.api.inventory_service as inventory_service


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
        client_id=None,
        category="electronics",
        warehouse=warehouse,
    )
    session.add(sku)
    session.commit()
    session.refresh(sku)
    return sku


# ═════════════════════════════════════════════════════════════════════════════
# StockEntry tests
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateStockEntry:
    """Suite for ``create_stock_entry``."""

    # ── 1. Valid entry persists ─────────────────────────────────────────

    def test_valid_entry_persists(self, db_session: Session) -> None:
        """MOVEMENT-ENTRY-01: a valid StockEntry is persisted."""
        sku = _create_sku(db_session, sku_code="SKU-ENT-01")

        entry = create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-001", warehouse="LA"
            ),
            user_uuid="backend-user-abc",
        )

        assert entry.id is not None
        assert entry.sku_id == sku.id
        assert entry.quantity == 10
        assert entry.reference == "PO-001"
        assert entry.warehouse == "LA"
        assert entry.user_uuid == "backend-user-abc"

    # ── 2. User UUID from backend ───────────────────────────────────────

    def test_stores_backend_user_uuid(self, db_session: Session) -> None:
        """MOVEMENT-ENTRY-02: ``user_uuid`` is set by the backend, not from
        the schema."""
        sku = _create_sku(db_session, sku_code="SKU-ENT-02")

        entry = create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-002", warehouse="LA"
            ),
            user_uuid="backend-user-xyz",
        )

        assert entry.user_uuid == "backend-user-xyz"

    def test_captures_exact_inbound_event_after_persistence(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persisted entry emits one event with only contract properties."""
        captured = []
        sku = SKU(
            name="Telemetry SKU",
            sku="SKU-INBOUND-TELEMETRY",
            client_name="Private Client Name",
            client_id="client-123",
            category="fashion",
            warehouse="LA",
        )
        db_session.add(sku)
        db_session.commit()
        db_session.refresh(sku)
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)

        entry = create_stock_entry(
            db_session,
            StockEntryCreate(
                sku_id=sku.id, quantity=7, reference="PRIVATE-REFERENCE", warehouse="LA"
            ),
            user_uuid="user-123",
            request_id="550e8400-e29b-41d4-a716-446655440099",
        )

        assert len(captured) == 1
        event = captured[0]
        assert event.event_type == "inbound_order_created"
        assert event.properties == {
            "warehouse": "los_angeles",
            "client_id": "client-123",
            "product_id": str(sku.id),
            "product_category": "fashion",
            "quantity": 7,
            "movement_id": str(entry.id),
        }
        assert event.eventId.version == 4
        assert event.timestamp.endswith("Z")
        assert event.schemaVersion == "1.0"
        assert event.requestId == "550e8400-e29b-41d4-a716-446655440099"
        assert event.userId == "user-123"
        assert event.sessionId is None

    def test_telemetry_sink_failure_preserves_entry(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sku = _create_sku(db_session, sku_code="SKU-SINK",)
        sku.client_id = "client-123"
        db_session.add(sku)
        db_session.commit()
        monkeypatch.setattr(
            inventory_service, "capture_telemetry_events", lambda _events: (_ for _ in ()).throw(RuntimeError())
        )

        entry = create_stock_entry(
            db_session,
            StockEntryCreate(sku_id=sku.id, quantity=3, reference="PO", warehouse="LA"),
            user_uuid="u1",
        )

        assert entry.id is not None
        assert db_session.get(StockEntry, entry.id) is not None

    def test_cache_failure_keeps_persisted_entry_and_event(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = []
        sku = _create_sku(db_session, sku_code="SKU-CACHE")
        sku.client_id = "client-123"
        db_session.add(sku)
        db_session.commit()
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)
        monkeypatch.setattr(
            "services.api.cache.invalidate_inventory_cache",
            lambda _session: (_ for _ in ()).throw(RuntimeError("cache failure")),
        )

        with pytest.raises(RuntimeError, match="cache failure"):
            create_stock_entry(
                db_session,
                StockEntryCreate(sku_id=sku.id, quantity=3, reference="PO", warehouse="LA"),
                user_uuid="u1",
            )

        persisted = db_session.exec(select(StockEntry)).one()
        assert persisted.id is not None
        assert len(captured) == 1
        assert captured[0].event_type == "inbound_order_created"
        assert captured[0].properties["movement_id"] == str(persisted.id)

    def test_legacy_client_id_skips_event_but_persists(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = []
        sku = _create_sku(db_session, sku_code="SKU-LEGACY")
        assert sku.client_id is None
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)
        entry = create_stock_entry(
            db_session,
            StockEntryCreate(sku_id=sku.id, quantity=3, reference="PO", warehouse="LA"),
            user_uuid="u1",
        )
        assert entry.id is not None
        assert db_session.get(StockEntry, entry.id) is not None
        assert captured == []

    def test_commit_failure_emits_no_event(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = []
        sku = _create_sku(db_session, sku_code="SKU-COMMIT")
        sku.client_id = "client-123"
        db_session.add(sku)
        db_session.commit()
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)
        monkeypatch.setattr(db_session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failure")))

        with pytest.raises(RuntimeError, match="commit failure"):
            create_stock_entry(
                db_session,
                StockEntryCreate(sku_id=sku.id, quantity=3, reference="PO", warehouse="LA"),
                user_uuid="u1",
            )
        assert captured == []

    def test_refresh_failure_emits_no_event(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = []
        sku = _create_sku(db_session, sku_code="SKU-REFRESH")
        sku.client_id = "client-123"
        db_session.add(sku)
        db_session.commit()
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)
        monkeypatch.setattr(db_session, "refresh", lambda _entry: (_ for _ in ()).throw(RuntimeError("refresh failure")))

        with pytest.raises(RuntimeError, match="refresh failure"):
            create_stock_entry(
                db_session,
                StockEntryCreate(sku_id=sku.id, quantity=3, reference="PO", warehouse="LA"),
                user_uuid="u1",
            )
        assert captured == []

    # ── 3. Entry increases get_current_stock ────────────────────────────

    def test_increases_stock(self, db_session: Session) -> None:
        """MOVEMENT-ENTRY-03: after an entry, get_current_stock reflects
        the increase."""
        sku = _create_sku(db_session, sku_code="SKU-ENT-03")

        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=20, reference="PO-003", warehouse="LA"
            ),
            user_uuid="u1",
        )

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert stock == 20

    # ── 4. SKU inexistente → 404 ────────────────────────────────────────

    def test_sku_not_found_raises_404(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MOVEMENT-ENTRY-04: non-existent SKU raises HTTP 404."""
        captured = []
        before = db_session.exec(select(StockEntry)).all()
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)
        with pytest.raises(HTTPException) as exc_info:
            create_stock_entry(
                db_session,
                data=StockEntryCreate(
                    sku_id=99999, quantity=5, reference="PO-XXX", warehouse="LA"
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "SKU not found."
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "unknown_product"}
        after = db_session.exec(select(StockEntry)).all()
        assert len(after) == len(before)
        assert captured == []

    # ── 5. Warehouse mismatch → 400 ─────────────────────────────────────

    def test_warehouse_mismatch_raises_400(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MOVEMENT-ENTRY-05: LA SKU cannot receive entry with warehouse ZGZ."""
        captured = []
        sku = _create_sku(db_session, sku_code="SKU-ENT-05", warehouse="LA")
        before = db_session.exec(select(StockEntry)).all()
        monkeypatch.setattr(inventory_service, "capture_telemetry_events", captured.extend)

        with pytest.raises(HTTPException) as exc_info:
            create_stock_entry(
                db_session,
                data=StockEntryCreate(
                    sku_id=sku.id, quantity=5, reference="PO-XXX", warehouse="ZGZ"
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 400
        assert "Warehouse mismatch" in exc_info.value.detail
        assert "LA" in exc_info.value.detail
        assert "ZGZ" in exc_info.value.detail
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "warehouse_mismatch"}
        after = db_session.exec(select(StockEntry)).all()
        assert len(after) == len(before)
        assert captured == []

    # ── 6. Rejected movement is not persisted ───────────────────────────

    def test_rejected_entry_not_persisted(self, db_session: Session) -> None:
        """MOVEMENT-ENTRY-06: a rejected entry (bad warehouse) does not
        create a StockEntry row."""
        sku = _create_sku(db_session, sku_code="SKU-ENT-06", warehouse="LA")
        count_before = db_session.exec(select(StockEntry)).all()

        with pytest.raises(HTTPException):
            create_stock_entry(
                db_session,
                data=StockEntryCreate(
                    sku_id=sku.id, quantity=5, reference="PO-XXX", warehouse="ZGZ"
                ),
                user_uuid="u1",
            )

        count_after = db_session.exec(select(StockEntry)).all()
        assert len(count_after) == len(count_before)


# ═════════════════════════════════════════════════════════════════════════════
# StockExit tests
# ═════════════════════════════════════════════════════════════════════════════


class TestCreateStockExit:
    """Suite for ``create_stock_exit``."""

    # ── 1. Valid exit persists ──────────────────────────────────────────

    def test_valid_exit_persists(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-01: a valid StockExit is persisted."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-01")
        # Add stock first
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        exit_record = create_stock_exit(
            db_session,
            data=StockExitCreate(
                sku_id=sku.id,
                quantity=3,
                exit_type="dispatch",
                tracking_number="TRK-001",
                warehouse="LA",
            ),
            user_uuid="backend-user-abc",
        )

        assert exit_record.id is not None
        assert exit_record.sku_id == sku.id
        assert exit_record.quantity == 3
        assert exit_record.exit_type == "dispatch"
        assert exit_record.tracking_number == "TRK-001"
        assert exit_record.warehouse == "LA"
        assert exit_record.user_uuid == "backend-user-abc"

    # ── 2. User UUID from backend ───────────────────────────────────────

    def test_stores_backend_user_uuid(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-02: ``user_uuid`` is set by the backend."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-02")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        exit_record = create_stock_exit(
            db_session,
            data=StockExitCreate(
                sku_id=sku.id,
                quantity=3,
                exit_type="dispatch",
                tracking_number="TRK-002",
                warehouse="LA",
            ),
            user_uuid="backend-user-xyz",
        )

        assert exit_record.user_uuid == "backend-user-xyz"

    # ── 3. Exit reduces get_current_stock ───────────────────────────────

    def test_reduces_stock(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-03: after an exit, get_current_stock reflects
        the reduction."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-03")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        create_stock_exit(
            db_session,
            data=StockExitCreate(
                sku_id=sku.id,
                quantity=4,
                exit_type="dispatch",
                tracking_number="TRK-003",
                warehouse="LA",
            ),
            user_uuid="u1",
        )

        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert stock == 6

    # ── 4. Exit exactly equal to available is allowed → stock 0 ─────────

    def test_exit_exact_available_allowed(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-04: exit with quantity == available is allowed,
        resulting stock = 0."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-04")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        exit_record = create_stock_exit(
            db_session,
            data=StockExitCreate(
                sku_id=sku.id,
                quantity=10,
                exit_type="dispatch",
                tracking_number="TRK-004",
                warehouse="LA",
            ),
            user_uuid="u1",
        )

        assert exit_record.quantity == 10
        stock = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert stock == 0

    # ── 5. Exit exceeding available is rejected ─────────────────────────

    def test_exit_exceeding_stock_rejected(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-05: exit with quantity > available is rejected
        with HTTP 400."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-05")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=8,
                    exit_type="dispatch",
                    tracking_number="TRK-005",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "insufficient_stock"}

    # ── 6. Insufficient stock uses status 400 ───────────────────────────

    def test_insufficient_stock_status_400(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-06: insufficient stock uses HTTP 400."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-06")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=8,
                    exit_type="dispatch",
                    tracking_number="TRK-006",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 400

    # ── 7. Detail message is exact required format ──────────────────────

    def test_insufficient_stock_detail_exact(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-07: detail message matches required format."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-07", warehouse="LA")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=8,
                    exit_type="dispatch",
                    tracking_number="TRK-007",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        expected = (
            f"Insufficient stock for SKU 'SKU-EXT-07'. "
            f"Available: 5, requested: 8."
        )
        assert exc_info.value.detail == expected

    # ── 8. Rejected exit does NOT persist ───────────────────────────────

    def test_rejected_exit_not_persisted(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-08: a rejected StockExit creates no row."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-08")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )
        count_before = db_session.exec(select(StockExit)).all()

        with pytest.raises(HTTPException):
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=8,
                    exit_type="dispatch",
                    tracking_number="TRK-008",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        count_after = db_session.exec(select(StockExit)).all()
        assert len(count_after) == len(count_before)

    # ── 9. Stock unchanged after rejection ──────────────────────────────

    def test_stock_unchanged_after_rejection(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-09: after a rejection, get_current_stock returns
        the same value as before."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-09")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=5, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        stock_before = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")

        with pytest.raises(HTTPException):
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=8,
                    exit_type="dispatch",
                    tracking_number="TRK-009",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        stock_after = get_current_stock(db_session, sku_id=sku.id, warehouse="LA")
        assert stock_after == stock_before
        assert stock_after == 5

    # ── 10. Warehouse mismatch → 400 ─────────────────────────────────────

    def test_warehouse_mismatch_raises_400(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-10: LA SKU cannot exit with warehouse ZGZ."""
        sku = _create_sku(db_session, sku_code="SKU-EXT-10", warehouse="LA")
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku.id, quantity=10, reference="PO-INIT", warehouse="LA"
            ),
            user_uuid="u1",
        )

        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku.id,
                    quantity=3,
                    exit_type="dispatch",
                    tracking_number="TRK-010",
                    warehouse="ZGZ",
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 400
        assert "Warehouse mismatch" in exc_info.value.detail
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "warehouse_mismatch"}

    # ── 11. SKU inexistente → 404 ────────────────────────────────────────

    def test_sku_not_found_raises_404(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-11: non-existent SKU raises HTTP 404."""
        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=99999,
                    quantity=3,
                    exit_type="dispatch",
                    tracking_number="TRK-011",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "SKU not found."
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "unknown_product"}

    # ── 12. LA stock cannot use ZGZ stock ────────────────────────────────

    def test_la_cannot_use_zgz_stock(self, db_session: Session) -> None:
        """MOVEMENT-EXIT-12: stock in ZGZ does not allow exits from LA.

        SKU with warehouse LA has only stock=3.
        A separate SKU in ZGZ has stock=100.
        Requested exit LA with quantity=5 must fail.
        ZGZ stock must remain untouched.
        """
        sku_la = _create_sku(db_session, sku_code="SKU-LA", warehouse="LA")
        sku_zgz = _create_sku(db_session, sku_code="SKU-ZGZ", warehouse="ZGZ")

        # LA movements — stock = 3
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku_la.id, quantity=3, reference="PO-LA", warehouse="LA"
            ),
            user_uuid="u1",
        )

        # ZGZ movements — stock = 100
        create_stock_entry(
            db_session,
            data=StockEntryCreate(
                sku_id=sku_zgz.id, quantity=100, reference="PO-ZGZ", warehouse="ZGZ"
            ),
            user_uuid="u1",
        )

        # Attempt to exit 5 from LA — only 3 available
        with pytest.raises(HTTPException) as exc_info:
            create_stock_exit(
                db_session,
                data=StockExitCreate(
                    sku_id=sku_la.id,
                    quantity=5,
                    exit_type="dispatch",
                    tracking_number="TRK-LA",
                    warehouse="LA",
                ),
                user_uuid="u1",
            )

        assert exc_info.value.status_code == 400
        expected = (
            f"Insufficient stock for SKU 'SKU-LA'. "
            f"Available: 3, requested: 5."
        )
        assert exc_info.value.detail == expected
        assert exc_info.value.headers == {"X-TrackFlow-Error-Code": "insufficient_stock"}

        # Verify ZGZ stock remains untouched
        zgz_stock = get_current_stock(db_session, sku_id=sku_zgz.id, warehouse="ZGZ")
        assert zgz_stock == 100