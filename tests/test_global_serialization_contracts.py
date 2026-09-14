"""Global runtime, OpenAPI, and HTTP serialization verification."""

from __future__ import annotations

import ast
import asyncio
import csv
import io
from pathlib import Path
from typing import Any, get_args, get_origin

import httpx
from fastapi import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from services.api import main as api_main
from services.api.main import IncidentAnalysisResponse, app
from services.api.models import SupplierResponse

ROOT = Path(__file__).resolve().parents[1]
SPECIAL = {
    ("DELETE", "/users/{user_id}"),
    ("DELETE", "/suppliers/{supplier_id}"),
    ("DELETE", "/api/suppliers/{supplier_id}"),
    ("GET", "/api/incidents/results/export"),
}
EXPECTED_ROUTES = {
    (method, path)
    for method, path in [
        ("POST", "/auth/login"), ("GET", "/auth/me"),
        ("POST", "/auth/forgot-password"), ("POST", "/auth/reset-password"),
        ("POST", "/auth/change-password"), ("POST", "/users"), ("GET", "/users"),
        ("GET", "/users/{user_id}"), ("PUT", "/users/{user_id}"),
        ("DELETE", "/users/{user_id}"), ("GET", "/profiles/me"),
        ("PUT", "/profiles/me"), ("GET", "/inventory/products"),
        ("GET", "/inventory/products/{id}"), ("POST", "/inventory/products"),
        ("POST", "/inventory/orders/inbound"), ("POST", "/inventory/orders/outbound"),
        ("GET", "/inventory/orders"), ("GET", "/api/suppliers"),
        ("GET", "/suppliers"), ("GET", "/api/suppliers/{supplier_id}"),
        ("GET", "/suppliers/{supplier_id}"), ("POST", "/api/suppliers"),
        ("POST", "/suppliers"), ("PATCH", "/api/suppliers/{supplier_id}/rate"),
        ("PATCH", "/suppliers/{supplier_id}/rate"),
        ("PATCH", "/api/suppliers/{supplier_id}/status"),
        ("PATCH", "/suppliers/{supplier_id}/status"),
        ("DELETE", "/api/suppliers/{supplier_id}"),
        ("DELETE", "/suppliers/{supplier_id}"),
        ("POST", "/api/incidents/analyze"),
        ("GET", "/api/incidents/results/export"), ("GET", "/health"),
    ]
}


def _routes() -> list[tuple[str, str, APIRoute]]:
    result: list[tuple[str, str, APIRoute]] = []
    for registered in app.routes:
        if isinstance(registered, APIRoute):
            result.extend((method, registered.path, registered) for method in registered.methods)
        elif hasattr(registered, "original_router"):
            prefix = registered.include_context.prefix
            result.extend(
                (method, prefix + route.path, route)
                for route in registered.original_router.routes
                if isinstance(route, APIRoute)
                for method in route.methods
            )
    return result


def _model_is_nominal(model: Any) -> bool:
    origin = get_origin(model)
    if origin in (list,):
        args = get_args(model)
        return len(args) == 1 and _model_is_nominal(args[0])
    return isinstance(model, type) and issubclass(model, BaseModel)


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def _exact_keys(response: httpx.Response, keys: set[str]) -> dict[str, Any]:
    assert response.status_code < 300, response.text
    payload = response.json()
    assert set(payload) == keys
    return payload


def test_runtime_manifest_has_exactly_33_registrations() -> None:
    routes = _routes()
    found = {(method, path) for method, path, _ in routes}
    assert len(routes) == 33
    assert len(found) == 33
    assert found == EXPECTED_ROUTES


def test_all_json_routes_have_nominal_pydantic_contracts() -> None:
    routes = {(method, path): route for method, path, route in _routes()}
    for key in EXPECTED_ROUTES - SPECIAL:
        assert routes[key].response_model is not None, key
        assert _model_is_nominal(routes[key].response_model), key


def test_special_http_contracts_and_openapi_are_explicit() -> None:
    routes = {(method, path): route for method, path, route in _routes()}
    for key in SPECIAL - {("GET", "/api/incidents/results/export")}:
        route = routes[key]
        assert route.response_model is None
        assert route.status_code == 204
        assert route.response_class is Response
    csv_route = routes[("GET", "/api/incidents/results/export")]
    assert csv_route.response_model is None and csv_route.response_class is Response
    csv_success = app.openapi()["paths"]["/api/incidents/results/export"]["get"]["responses"]["200"]
    assert csv_success["content"] == {"text/csv": {"schema": {"type": "string"}}}
    assert "Content-Disposition" in csv_success["headers"]


def test_openapi_visible_success_responses_are_nominal_or_special() -> None:
    openapi = app.openapi()
    visible = {
        (method, path)
        for method, path, route in _routes()
        if route.include_in_schema and not path.startswith("/api/suppliers")
    }
    assert len(visible) == 27
    for method, path in visible:
        operation = openapi["paths"][path][method.lower()]
        success = operation["responses"]["200"] if "200" in operation["responses"] else operation["responses"].get("201")
        if (method, path) in SPECIAL:
            continue
        assert success is not None
        schema = success["content"]["application/json"]["schema"]
        assert "$ref" in schema or (schema.get("type") == "array" and "$ref" in schema["items"])
    for path in ("/users/{user_id}", "/suppliers/{supplier_id}"):
        assert "content" not in openapi["paths"][path]["delete"]["responses"]["204"]


def test_application_decorators_explicitly_declare_response_models() -> None:
    files = [ROOT / "services/api/main.py", *((ROOT / "services/api/routes").glob("*.py"))]
    http_decorators = {"get", "post", "put", "patch", "delete", "options", "head"}
    source_decorators: list[tuple[Path, str, set[str]]] = []
    inferred_only: list[str] = []
    for file in files:
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                if call is None or not isinstance(call.func, ast.Attribute):
                    continue
                owner = call.func.value.id if isinstance(call.func.value, ast.Name) else None
                if owner not in {"router", "app"} or call.func.attr not in http_decorators:
                    continue
                names = {keyword.arg for keyword in call.keywords if keyword.arg}
                source_decorators.append((file, node.name, names))
                if "response_model" not in names and not (
                    {"response_class", "responses"} <= names
                    or {"response_class", "status_code"} <= names
                ):
                    inferred_only.append(f"{file}:{node.name}")
    unique_json = [item for item in source_decorators if "response_model" in item[2]]
    unique_special = [
        item for item in source_decorators if "response_model" not in item[2]
    ]
    assert len(source_decorators) == 27
    assert len(unique_json) == 24
    assert len(unique_special) == 3
    assert inferred_only == []


def test_registration_accounting_distinguishes_source_and_runtime_layers() -> None:
    runtime = {(method, path) for method, path, _ in _routes()}
    runtime_special = runtime & SPECIAL
    runtime_json = runtime - SPECIAL
    openapi = app.openapi()
    visible = {
        (method.upper(), path)
        for path, operation_set in openapi["paths"].items()
        for method in operation_set
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert len(runtime) == 33
    assert len(runtime_json) == 29
    assert len(runtime_special) == 4
    assert len(visible) == 27


def test_http_auth_profile_supplier_and_health_contracts(isolated_auth_db, monkeypatch) -> None:
    from services.api import database
    from services.api.routes import suppliers as supplier_routes
    from tinydb import TinyDB

    supplier_db = TinyDB(str(Path(isolated_auth_db.db_path).with_name("suppliers-test.json")))
    monkeypatch.setattr(database, "suppliers", supplier_db.table("suppliers"))
    monkeypatch.setattr(supplier_routes, "suppliers", database.suppliers)

    try:
        email = "http-contract@example.com"
        registration = _request("POST", "/users", json={"email": email, "password": "password-123"})
        assert registration.status_code == 201
        assert registration.json() == {"message": "User registered successfully."}
        token_response = _request("POST", "/auth/login", data={"username": email, "password": "password-123"})
        token = _exact_keys(token_response, {"access_token", "token_type"})["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        _exact_keys(_request("GET", "/auth/me", headers=headers), {"id", "email", "is_active", "role"})

        profile = _request("PUT", "/profiles/me", json={"name": "Test", "phone": None, "address": "Local"}, headers=headers)
        _exact_keys(profile, {"name", "phone", "address"})
        _exact_keys(_request("GET", "/profiles/me", headers=headers), {"name", "phone", "address"})

        supplier_payload = {"name": "HTTP Supplier", "country": "USA", "categories": ["carrier_last_mile"], "rate_per_shipment": 2.5, "currency": "USD", "status": "active"}
        created = _exact_keys(_request("POST", "/api/suppliers", json=supplier_payload, headers=headers), {"id"})
        listed = _request("GET", "/api/suppliers", headers=headers)
        assert listed.status_code == 200
        assert set(listed.json()[0]) == set(SupplierResponse.model_fields)
        alias = _request("GET", "/suppliers", headers=headers)
        assert alias.status_code == 200 and alias.json()[0]["id"] == created["id"]

        assert _request("GET", "/health").json() == {"status": "ok"}
    finally:
        supplier_db.close()


def test_http_incidents_analyze_then_export_uses_real_request(isolated_auth_db, monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_last_result", None)
    email = "incidents-http@example.com"
    registration = _request(
        "POST", "/users", json={"email": email, "password": "password-123"}
    )
    assert registration.status_code == 201
    token = _exact_keys(
        _request(
            "POST",
            "/auth/login",
            data={"username": email, "password": "password-123"},
        ),
        {"access_token", "token_type"},
    )["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    incident_csv = (
        "incident_id,date,country,customer_type,tracking_number,carrier,"
        "category,description,status,customer_email,satisfaction_score\n"
        "TRF-000002,2024-01-15,US,B2B,TRACK0002,UPS,LOST_PARCEL,"
        "Package was lost in transit,CLOSED,customer@example.com,5\n"
    )

    analyzed = _request(
        "POST",
        "/api/incidents/analyze",
        headers=headers,
        files={"file": ("incidents.csv", incident_csv, "text/csv")},
    )
    assert analyzed.status_code == 200, analyzed.text
    assert set(analyzed.json()) == set(IncidentAnalysisResponse.model_fields)
    IncidentAnalysisResponse.model_validate(analyzed.json())

    exported = _request("GET", "/api/incidents/results/export", headers=headers)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert exported.headers["content-disposition"] == 'attachment; filename="results.csv"'
    assert not exported.text.startswith("{")
    assert next(csv.reader(io.StringIO(exported.text))) == ["section", "metric", "value"]


def test_http_inventory_contracts_use_isolated_sqlite(isolated_auth_db, monkeypatch) -> None:
    from sqlmodel import Session, SQLModel, create_engine
    from sqlalchemy.pool import StaticPool
    from services.api import database
    from services.api.inventory_models import SKU

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
        email = "inventory-http@example.com"
        registration = _request(
            "POST", "/users", json={"email": email, "password": "password-123"}
        )
        assert registration.status_code == 201
        token = _exact_keys(
            _request(
                "POST",
                "/auth/login",
                data={"username": email, "password": "password-123"},
            ),
            {"access_token", "token_type"},
        )["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        product = _request(
            "POST",
            "/inventory/products",
            headers=headers,
            json={
                "name": "HTTP SKU",
                "sku": "HTTP-001",
                "client_name": "HTTP Client",
                "category": "electronics",
                "warehouse": "LA",
            },
        )
        assert product.status_code == 200, product.text
        sku_id = product.json()["id"]

        inbound = _request(
            "POST",
            "/inventory/orders/inbound",
            headers=headers,
            json={
                "sku_id": sku_id,
                "quantity": 4,
                "reference": "HTTP-REF",
                "warehouse": "LA",
            },
        )
        created = _exact_keys(inbound, {"id"})
        assert isinstance(created["id"], int)

        orders = _request("GET", "/inventory/orders", headers=headers)
        assert orders.status_code == 200, orders.text
        item = next(item for item in orders.json() if item["id"] == created["id"])
        assert set(item) == {
            "id", "movement_type", "quantity", "warehouse", "created_at",
            "user_uuid", "sku_name", "sku_code", "reference", "exit_type",
            "tracking_number",
        }
        assert "sku_id" not in item and "sku" not in item
    finally:
        app.dependency_overrides.pop(database.get_db, None)
        engine.dispose()
