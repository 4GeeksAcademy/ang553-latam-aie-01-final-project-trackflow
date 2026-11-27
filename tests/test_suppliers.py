"""Functional coverage for supplier mutation response and TinyDB persistence contracts."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from tinydb import TinyDB

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import create_access_token
from services.api.main import app
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

        manager = UserInDB(email="manager@example.test", hashed_password="test", role=Role.manager)
        listed = suppliers_route.list_suppliers(manager, Response())
        detailed = suppliers_route.get_supplier(created.id, manager, Response())

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


@pytest.mark.parametrize("prefix", ["/suppliers", "/api/suppliers"])
@pytest.mark.parametrize("role", [None, Role.user, Role.manager, Role.admin])
@pytest.mark.parametrize("operation", ["list", "detail", "create", "rate", "status", "delete"])
def test_catalog_access_matrix(prefix, role, operation, monkeypatch, tmp_path, isolated_auth_db):
    db, table = _isolated_suppliers(monkeypatch, tmp_path)
    try:
        seed = _supplier_payload().model_dump(mode="json")
        seed["updated_at"] = "2026-09-13T20:00:00+00:00"
        supplier_id = table.insert(seed)
        detail = f"{prefix}/{supplier_id}"
        headers = {}
        if role is not None:
            user = UserInDB(email="catalog-qa@example.test", hashed_password="test", role=role)
            isolated_auth_db.users.insert(user.model_dump(mode="json"))
            headers["Authorization"] = f"Bearer {create_access_token(user.id)}"
        method, path, payload, success = {
            "list": ("GET", f"{prefix}?country=USA&category=carrier_last_mile", None, 200),
            "detail": ("GET", detail, None, 200),
            "create": ("POST", prefix, {**seed, "name": "Second QA Supplier"}, 201),
            "rate": ("PATCH", f"{detail}/rate", {"rate_per_shipment": 19.5}, 200),
            "status": ("PATCH", f"{detail}/status", {"status": "suspended"}, 200),
            "delete": ("DELETE", detail, None, 204),
        }[operation]
        basic_read = operation in {"list", "detail"}
        authorized = (
            basic_read or role is Role.admin or (role is Role.manager and operation != "delete")
        )
        expected_status = 401 if role is None else success if authorized else 403
        # Dispatch ASGI without running the production PostgreSQL lifespan.
        client = TestClient(app, raise_server_exceptions=False)
        try:
            response = client.request(method, path, headers=headers, json=payload)
        finally:
            client.close()
        assert response.status_code == expected_status

        if expected_status in {401, 403}:
            assert len(table) == 1
            assert dict(table.get(doc_id=supplier_id)) == seed
        elif basic_read:
            item = response.json()[0] if operation == "list" else response.json()
            assert response.headers["cache-control"] == "private, no-store"
            fields = set(SupplierResponse.model_fields)
            commercial_fields = {"rate_per_shipment", "currency", "contact_email", "notes"}
            assert set(item) == (fields - commercial_fields if role is Role.user else fields)
            assert item["id"] == supplier_id
            assert item["name"] == seed["name"]
            if role is not Role.user:
                assert all(item[field] == seed[field] for field in commercial_fields)
        elif operation == "delete":
            assert response.content == b""
            assert len(table) == 0
        elif operation == "create":
            new_supplier = table.get(doc_id=response.json()["id"])
            assert len(table) == 2
            assert new_supplier["name"] == "Second QA Supplier"
        elif operation == "rate":
            assert table.get(doc_id=supplier_id)["rate_per_shipment"] == 19.5
        else:
            assert table.get(doc_id=supplier_id)["status"] == "suspended"
    finally:
        db.close()
