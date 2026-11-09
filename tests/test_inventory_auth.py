"""HTTP authorization contract for inventory read endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from tinydb import Query

from services.api.access_control import assign_user_to_client, register_verified_client
from services.api.auth_models import Role
from services.api.main import app
from services.api.inventory_clients import CLIENT_IDS_BY_NAME
from services.incidents.models import operational_security_metadata


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


@pytest.fixture
def isolated_inventory_db():
    from services.api import database
    from services.api.cache import orders_cache, products_cache
    from services.api.inventory_outbox import inventory_outbox_metadata

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    inventory_outbox_metadata.create_all(engine)
    operational_security_metadata.create_all(engine)

    def isolated_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[database.get_db] = isolated_get_db
    try:
        yield engine
    finally:
        app.dependency_overrides.pop(database.get_db, None)
        products_cache.clear()
        orders_cache.clear()
        engine.dispose()


def test_inventory_gets_require_authentication(isolated_inventory_db) -> None:
    for path in ("/inventory/products", "/inventory/products/999999", "/inventory/orders"):
        response = _request("GET", path)
        assert response.status_code == 401, (path, response.text)


def test_cors_exposes_inventory_error_code_header(isolated_inventory_db) -> None:
    preflight = _request(
        "OPTIONS",
        "/inventory/orders/inbound",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert preflight.status_code == 200
    assert preflight.headers["Access-Control-Allow-Origin"] == "http://localhost:3001"
    assert "POST" in preflight.headers["Access-Control-Allow-Methods"]
    allowed_headers = preflight.headers["Access-Control-Allow-Headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers

    response = _request(
        "GET",
        "/inventory/products",
        headers={"Origin": "http://localhost:3001"},
    )
    assert response.status_code == 401
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3001"
    exposed_headers = response.headers["Access-Control-Expose-Headers"]
    assert "X-Request-ID" in exposed_headers
    assert "X-TrackFlow-Error-Code" in exposed_headers


def test_authenticated_inventory_gets_preserve_contract(
    isolated_auth_db, isolated_inventory_db
) -> None:
    engine = isolated_inventory_db
    email = "inventory-auth@example.com"
    registration = _request(
        "POST", "/users", json={"email": email, "password": "password-123"}
    )
    assert registration.status_code == 201
    user = isolated_auth_db.users.get(Query().email == email)
    assert user is not None
    isolated_auth_db.users.update(
        {"role": Role.manager.value}, Query().email == email
    )
    client_id = CLIENT_IDS_BY_NAME["UrbanThread"]
    with Session(engine) as session:
        register_verified_client(
            session,
            client_id=client_id,
            display_name="UrbanThread",
            actor_user_id=user["id"],
        )
        assign_user_to_client(
            session,
            user_id=user["id"],
            client_id=client_id,
            can_view_sensitive=False,
            actor_user_id=user["id"],
        )

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
            "client_id": client_id,
            "client_name": "UrbanThread",
            "category": "electronics",
            "warehouse": "LA",
        },
    )
    assert product.status_code == 200
    product_id = product.json()["id"]

    products = _request("GET", "/inventory/products", headers=headers)
    assert products.status_code == 200
    assert products.json()[0]["current_stock"] == 0
    assert products.json()[0]["sku"] == "AUTH-001"
    unauthenticated_products = _request("GET", "/inventory/products")
    assert unauthenticated_products.status_code == 401
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
        item
        for item in authenticated_orders.json()
        if item["id"] == inbound.json()["id"]
    )
    assert "user_uuid" not in item
    unauthenticated_orders = _request("GET", "/inventory/orders")
    assert unauthenticated_orders.status_code == 401
