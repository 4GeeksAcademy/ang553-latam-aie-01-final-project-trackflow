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
from services.api.inventory_clients import CLIENT_IDS_BY_NAME
from services.api.models import SupplierResponse

ROOT = Path(__file__).resolve().parents[1]
SPECIAL = {
    ("DELETE", "/users/{user_id}"),
    ("DELETE", "/suppliers/{supplier_id}"),
    ("DELETE", "/api/suppliers/{supplier_id}"),
    ("GET", "/api/incidents/results/export"),
}
LEGACY_STRUCTURAL_JSON = {
    ("POST", "/telemetry/events"),
    ("GET", "/telemetry/report"),
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
        ("POST", "/telemetry/events"), ("GET", "/telemetry/report"),
        ("GET", "/reporting/weekly-warehouse-client-performance"),
        ("GET", "/reporting/pipeline-runs/latest"),
        ("POST", "/reporting/pipeline-runs"),
        ("GET", "/tasks/{task_id}"),
        ("PATCH", "/inventory/products/{id}/threshold"),
        ("POST", "/inventory/physical-counts"),
        ("POST", "/inventory/commercial-orders"),
        ("POST", "/inventory/commercial-orders/{order_id}/dispatches"),
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


def test_runtime_manifest_has_exactly_43_registrations() -> None:
    routes = _routes()
    found = {(method, path) for method, path, _ in routes}
    assert len(routes) == 43
    assert len(found) == 43
    assert found == EXPECTED_ROUTES


def test_all_json_routes_have_nominal_pydantic_contracts() -> None:
    routes = {(method, path): route for method, path, route in _routes()}
    for key in EXPECTED_ROUTES - SPECIAL:
        assert routes[key].response_model is not None, key
        if key in LEGACY_STRUCTURAL_JSON:
            continue
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
    assert len(visible) == 37
    for method, path in visible:
        operation = openapi["paths"][path][method.lower()]
        success = next(
            (operation["responses"].get(code) for code in ("200", "201", "202") if code in operation["responses"]),
            None,
        )
        if (method, path) in SPECIAL | LEGACY_STRUCTURAL_JSON:
            continue
        assert success is not None
        schema = success["content"]["application/json"]["schema"]
        assert "$ref" in schema or (schema.get("type") == "array" and "$ref" in schema["items"])
    for path in ("/users/{user_id}", "/suppliers/{supplier_id}"):
        assert "content" not in openapi["paths"][path]["delete"]["responses"]["204"]


def test_application_decorators_explicitly_declare_response_models() -> None:
    files = [
        ROOT / "services/api/main.py",
        *((ROOT / "services/api/routes").glob("*.py")),
        *((ROOT / "services/reporting").glob("*.py")),
        ROOT / "services/task_queue/router.py",
    ]
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
    assert len(source_decorators) == 37
    assert len(unique_json) == 32
    assert len(unique_special) == 5
    assert len(inferred_only) == 2
    assert all("services\\api\\routes\\telemetry.py" in item for item in inferred_only)


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
    assert len(runtime) == 43
    assert len(runtime_json) == 39
    assert len(runtime_special) == 4
    assert len(visible) == 37


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
    from sqlmodel import Session, SQLModel, create_engine, select
    from sqlalchemy.pool import StaticPool
    from services.api import database
    from services.api.cache import orders_cache, products_cache
    from services.api.inventory_models import SKU
    from services.api.inventory_outbox import inventory_outbox_metadata

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    inventory_outbox_metadata.create_all(engine)

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
                "client_id": CLIENT_IDS_BY_NAME["PureStep Footwear"],
                "client_name": "PureStep Footwear",
                "category": "electronics",
                "warehouse": "LA",
            },
        )
        assert product.status_code == 200, product.text
        product_body = product.json()
        assert product_body["client_id"] == CLIENT_IDS_BY_NAME["PureStep Footwear"]
        assert product_body["client_name"] == "PureStep Footwear"
        sku_id = product_body["id"]

        listed = _request("GET", "/inventory/products", headers=headers)
        assert listed.status_code == 200, listed.text
        listed_product = next(item for item in listed.json() if item["id"] == sku_id)
        assert listed_product["client_id"] == CLIENT_IDS_BY_NAME["PureStep Footwear"]
        assert listed_product["client_name"] == "PureStep Footwear"

        detail = _request(
            "GET", f"/inventory/products/{sku_id}", headers=headers
        )
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["client_id"] == CLIENT_IDS_BY_NAME["PureStep Footwear"]
        assert detail_body["client_name"] == "PureStep Footwear"

        for invalid_payload in (
            {
                "name": "HTTP Missing Client",
                "sku": "HTTP-MISSING-CLIENT",
                "client_name": "PureStep Footwear",
                "category": "electronics",
                "warehouse": "LA",
            },
            {
                "name": "HTTP Unknown Client",
                "sku": "HTTP-UNKNOWN-CLIENT",
                "client_id": "00000000-0000-4000-8000-000000000000",
                "client_name": "PureStep Footwear",
                "category": "electronics",
                "warehouse": "LA",
            },
            {
                "name": "HTTP Mismatched Client",
                "sku": "HTTP-MISMATCHED-CLIENT",
                "client_id": CLIENT_IDS_BY_NAME["PureStep Footwear"],
                "client_name": "SoundWave Electronics",
                "category": "electronics",
                "warehouse": "LA",
            },
        ):
            invalid = _request(
                "POST",
                "/inventory/products",
                headers=headers,
                json=invalid_payload,
            )
            assert invalid.status_code == 422, invalid.text

        with Session(engine) as session:
            assert session.exec(
                select(SKU).where(SKU.sku == "HTTP-MISSING-CLIENT")
            ).first() is None
            assert session.exec(
                select(SKU).where(SKU.sku == "HTTP-UNKNOWN-CLIENT")
            ).first() is None
            assert session.exec(
                select(SKU).where(SKU.sku == "HTTP-MISMATCHED-CLIENT")
            ).first() is None
            assert session.exec(
                select(SKU).where(SKU.sku == "HTTP-001")
            ).first() is not None

        legacy = SKU(
            name="Legacy HTTP SKU",
            sku="HTTP-LEGACY-001",
            client_name="Legacy Client",
            client_id=None,
            category="electronics",
            warehouse="LA",
        )
        with Session(engine) as session:
            session.add(legacy)
            session.commit()
            session.refresh(legacy)

        legacy_response = _request(
            "GET", f"/inventory/products/{legacy.id}", headers=headers
        )
        assert legacy_response.status_code == 200, legacy_response.text
        legacy_body = legacy_response.json()
        assert legacy_body["client_id"] is None
        assert legacy_body["client_name"] == "Legacy Client"

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

        threshold = _request(
            "PATCH",
            f"/inventory/products/{sku_id}/threshold",
            headers=headers,
            json={"minimum_stock_threshold": 2},
        )
        assert threshold.status_code == 200, threshold.text
        assert threshold.json()["minimum_stock_threshold"] == 2
        invalid_threshold = _request(
            "PATCH",
            f"/inventory/products/{sku_id}/threshold",
            headers=headers,
            json={"minimum_stock_threshold": True},
        )
        assert invalid_threshold.status_code == 422

        count_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        count_payload = {
            "count_id": count_id,
            "sku_id": sku_id,
            "observed_quantity": 3,
            "warehouse": "LA",
        }
        count = _request(
            "POST", "/inventory/physical-counts", headers=headers, json=count_payload
        )
        assert count.status_code == 201, count.text
        assert count.json()["count_id"] == count_id
        count_retry = _request(
            "POST", "/inventory/physical-counts", headers=headers, json=count_payload
        )
        assert count_retry.status_code == 201, count_retry.text
        assert count_retry.json()["id"] == count.json()["id"]

        order_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        dispatch_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        order_payload = {
            "order_id": order_id,
            "client_id": CLIENT_IDS_BY_NAME["PureStep Footwear"],
            "warehouse": "LA",
            "dispatches": [
                {
                    "dispatch_id": dispatch_id,
                    "sku_id": sku_id,
                    "quantity": 2,
                    "tracking_number": "HTTP-ORDER-1",
                }
            ],
        }
        order = _request(
            "POST", "/inventory/commercial-orders", headers=headers, json=order_payload
        )
        assert order.status_code == 201, order.text
        assert order.json()["dispatch_ids"] == [dispatch_id]
        duplicate_dispatches = _request(
            "POST",
            "/inventory/commercial-orders",
            headers=headers,
            json={
                **order_payload,
                "order_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "dispatches": [
                    order_payload["dispatches"][0],
                    order_payload["dispatches"][0],
                ],
            },
        )
        assert duplicate_dispatches.status_code == 422
        order_retry = _request(
            "POST", "/inventory/commercial-orders", headers=headers, json=order_payload
        )
        assert order_retry.status_code == 201, order_retry.text
        assert order_retry.json() == order.json()

        second_dispatch_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        append_payload = {
            "order_id": order_id,
            "dispatches": [
                order_payload["dispatches"][0],
                {
                    "dispatch_id": second_dispatch_id,
                    "sku_id": sku_id,
                    "quantity": 1,
                    "tracking_number": "HTTP-ORDER-2",
                },
            ],
        }
        appended = _request(
            "POST",
            f"/inventory/commercial-orders/{order_id}/dispatches",
            headers=headers,
            json=append_payload,
        )
        assert appended.status_code == 201, appended.text
        assert set(appended.json()["dispatch_ids"]) == {
            dispatch_id,
            second_dispatch_id,
        }
        append_retry = _request(
            "POST",
            f"/inventory/commercial-orders/{order_id}/dispatches",
            headers=headers,
            json=append_payload,
        )
        assert append_retry.status_code == 201, append_retry.text
        assert append_retry.json() == appended.json()

        mismatched_path = _request(
            "POST",
            "/inventory/commercial-orders/eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee/dispatches",
            headers=headers,
            json={"order_id": order_id, "dispatches": order_payload["dispatches"]},
        )
        assert mismatched_path.status_code == 409

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
        products_cache.clear()
        orders_cache.clear()
        engine.dispose()


def test_inventory_openapi_client_id_contract() -> None:
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    sku_create = schemas["SKUCreate"]
    assert "client_id" in sku_create["required"]
    assert "client_name" in sku_create["required"]

    sku_response = schemas["SKUResponse"]
    client_id_schema = sku_response["properties"]["client_id"]
    assert client_id_schema.get("anyOf") == [
        {"type": "string"},
        {"type": "null"},
    ]


def test_inventory_business_openapi_contracts_are_explicit() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    assert "patch" in paths["/inventory/products/{id}/threshold"]
    assert "post" in paths["/inventory/physical-counts"]
    assert "post" in paths["/inventory/commercial-orders"]
    assert "post" in paths["/inventory/commercial-orders/{order_id}/dispatches"]

    models = schema["components"]["schemas"]
    assert set(models["SKUThresholdUpdate"]["required"]) == {
        "minimum_stock_threshold"
    }
    assert set(models["InventoryPhysicalCountCreate"]["required"]) == {
        "count_id",
        "sku_id",
        "observed_quantity",
        "warehouse",
    }
    assert set(models["CommercialOrderDispatchCreate"]["required"]) == {
        "dispatch_id",
        "sku_id",
        "quantity",
        "tracking_number",
    }
    assert set(models["CommercialOrderCreate"]["required"]) == {
        "order_id",
        "client_id",
        "warehouse",
        "dispatches",
    }
