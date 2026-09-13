"""Functional coverage for supplier mutation response and TinyDB persistence contracts."""

from __future__ import annotations

from datetime import datetime

from tinydb import TinyDB

from services.api.models import (
    SupplierCreate,
    SupplierCreatedResponse,
    SupplierMutationResponse,
    SupplierRateUpdate,
    SupplierResponse,
    SupplierStatus,
    SupplierStatusUpdate,
)
from services.api.routes import suppliers as suppliers_route


def _supplier_payload() -> SupplierCreate:
    return SupplierCreate(
        name="Reliable Logistics",
        country="USA",
        categories=["carrier_last_mile", "warehouse_supplies"],
        rate_per_shipment=12.5,
        currency="USD",
        status="active",
        service_zone="West Coast",
        contact_email="ops@reliable.example",
        notes="Preferred test supplier",
    )


def _isolated_suppliers(monkeypatch, tmp_path):
    db = TinyDB(tmp_path / "suppliers.json")
    table = db.table("suppliers")
    monkeypatch.setattr(suppliers_route, "suppliers", table)
    return db, table


def test_create_supplier_returns_minimal_response_and_persists_all_fields(monkeypatch, tmp_path):
    db, table = _isolated_suppliers(monkeypatch, tmp_path)
    try:
        result = suppliers_route.create_supplier(_supplier_payload())

        assert isinstance(result, SupplierCreatedResponse)
        assert result.model_dump() == {"id": 1}

        stored = table.get(doc_id=result.id)
        assert stored is not None
        assert stored["name"] == "Reliable Logistics"
        assert stored["country"] == "USA"
        assert stored["categories"] == ["carrier_last_mile", "warehouse_supplies"]
        assert stored["rate_per_shipment"] == 12.5
        assert stored["currency"] == "USD"
        assert stored["status"] == "active"
        assert isinstance(stored["updated_at"], str)
        datetime.fromisoformat(stored["updated_at"])
        assert stored["service_zone"] == "West Coast"
        assert stored["contact_email"] == "ops@reliable.example"
        assert stored["notes"] == "Preferred test supplier"
    finally:
        db.close()


def test_update_supplier_rate_returns_minimal_response_and_preserves_supplier(monkeypatch, tmp_path):
    db, table = _isolated_suppliers(monkeypatch, tmp_path)
    try:
        timestamps = iter([
            "2026-09-13T20:00:00+00:00",
            "2026-09-13T20:01:00+00:00",
        ])
        monkeypatch.setattr(suppliers_route, "_utc_now_iso", lambda: next(timestamps))

        created = suppliers_route.create_supplier(_supplier_payload())
        before = table.get(doc_id=created.id)
        assert before is not None
        assert before["updated_at"] == "2026-09-13T20:00:00+00:00"

        result = suppliers_route.update_supplier_rate(
            created.id,
            SupplierRateUpdate(rate_per_shipment=18.75),
        )

        assert isinstance(result, SupplierMutationResponse)
        assert set(result.model_dump()) == {"id", "updated_at"}
        assert result.id == created.id
        stored = table.get(doc_id=created.id)
        assert stored is not None
        assert stored["rate_per_shipment"] == 18.75
        assert stored["updated_at"] == "2026-09-13T20:01:00+00:00"
        assert stored["updated_at"] == result.updated_at.isoformat()
        for field in ("name", "country", "categories", "currency", "status", "service_zone", "contact_email", "notes"):
            assert stored[field] == before[field]
    finally:
        db.close()


def test_update_supplier_status_returns_minimal_response_and_preserves_supplier(monkeypatch, tmp_path):
    db, table = _isolated_suppliers(monkeypatch, tmp_path)
    try:
        timestamps = iter([
            "2026-09-13T20:00:00+00:00",
            "2026-09-13T20:01:00+00:00",
        ])
        monkeypatch.setattr(suppliers_route, "_utc_now_iso", lambda: next(timestamps))

        created = suppliers_route.create_supplier(_supplier_payload())
        before = table.get(doc_id=created.id)
        assert before is not None
        assert before["updated_at"] == "2026-09-13T20:00:00+00:00"

        result = suppliers_route.update_supplier_status(
            created.id,
            SupplierStatusUpdate(status=SupplierStatus.suspended),
        )

        assert isinstance(result, SupplierMutationResponse)
        assert set(result.model_dump()) == {"id", "updated_at"}
        assert result.id == created.id
        stored = table.get(doc_id=created.id)
        assert stored is not None
        assert stored["status"] == "suspended"
        assert stored["updated_at"] == "2026-09-13T20:01:00+00:00"
        assert stored["updated_at"] == result.updated_at.isoformat()
        for field in ("name", "country", "categories", "rate_per_shipment", "currency", "service_zone", "contact_email", "notes"):
            assert stored[field] == before[field]
    finally:
        db.close()


def test_supplier_reads_return_full_supplier_response(monkeypatch, tmp_path):
    db, table = _isolated_suppliers(monkeypatch, tmp_path)
    try:
        created = suppliers_route.create_supplier(_supplier_payload())

        listed = suppliers_route.list_suppliers()
        detailed = suppliers_route.get_supplier(created.id)

        assert isinstance(listed[0], SupplierResponse)
        assert isinstance(detailed, SupplierResponse)
        expected_fields = set(SupplierResponse.model_fields)
        assert set(listed[0].model_dump()) == expected_fields
        assert set(detailed.model_dump()) == expected_fields
        assert detailed.id == created.id
        assert detailed.name == "Reliable Logistics"
        assert detailed.service_zone == "West Coast"
        assert detailed.contact_email == "ops@reliable.example"
        assert detailed.notes == "Preferred test supplier"
    finally:
        db.close()
