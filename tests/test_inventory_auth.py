"""HTTP authorization contract for inventory read endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from services.api.main import app


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def test_inventory_gets_require_authentication() -> None:
    for path in ("/inventory/products", "/inventory/products/999999", "/inventory/orders"):
        response = _request("GET", path)
        assert response.status_code == 401, (path, response.text)


def test_authenticated_inventory_gets_preserve_contract(isolated_auth_db) -> None:
    from services.api import database

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def isolated_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[database.get_db] = isolated_get_db
    try:
        email = "inventory-auth@example.com"
        registration = _request(
            "POST", "/users", json={"email": email, "password": "password-123"}
        )
        assert registration.status_code == 201
        login = _request(
            "POST",
            "/auth/login",
            data={"username": email, "password": "password-123"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        product = _request(
            "POST",
            "/inventory/products",
            headers=headers,
            json={
                "name": "Auth SKU",
                "sku": "AUTH-001",
                "client_name": "Auth Client",
                "category": "electronics",
                "warehouse": "LA",
            },
        )
        assert product.status_code == 200
        product_id = product.json()["id"]

        products = _request("GET", "/inventory/products", headers=headers)
        assert products.status_code == 200
        detail = _request("GET", f"/inventory/products/{product_id}", headers=headers)
        assert detail.status_code == 200
        orders = _request("GET", "/inventory/orders", headers=headers)
        assert orders.status_code == 200
        assert isinstance(orders.json(), list)

        inbound = _request(
            "POST",
            "/inventory/orders/inbound",
            headers=headers,
            json={
                "sku_id": product_id,
                "quantity": 1,
                "reference": "AUTH-REF",
                "warehouse": "LA",
            },
        )
        assert inbound.status_code == 201
        authenticated_orders = _request("GET", "/inventory/orders", headers=headers)
        assert authenticated_orders.status_code == 200
        item = next(
            item for item in authenticated_orders.json() if item["id"] == inbound.json()["id"]
        )
        assert "user_uuid" in item
        assert item["user_uuid"]
    finally:
        app.dependency_overrides.pop(database.get_db, None)
        engine.dispose()
