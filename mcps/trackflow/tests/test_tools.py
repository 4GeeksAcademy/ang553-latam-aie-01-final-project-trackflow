from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

import httpx
import pytest
from mcpauth import AuthInfo
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import SecretStr

from trackflow_mcp.app import create_app
from trackflow_mcp.downstream import DownstreamError, InventoryReadAdapter, TrackFlowAPIClient
from trackflow_mcp.settings import (
    INCIDENTS_CREATE_SCOPE,
    INCIDENTS_READ_SCOPE,
    INCIDENTS_STATUS_SCOPE,
    INVENTORY_READ_SCOPE,
    MCPSettings,
)
from trackflow_mcp.token_exchange import DownstreamAccessToken

INCIDENT_RESPONSE = {
    "incident_id": "TRF-123456",
    "incident_date": "2026-11-09",
    "country": "US",
    "customer_type": "B2C",
    "tracking_number": "SECRET-TRACKING",
    "carrier": "UPS",
    "category": "DELAYED_DELIVERY",
    "description": "private customer description",
    "status": "OPEN",
    "customer_email": "private@example.com",
    "source_channel": "email",
    "created_at": "2026-11-09T10:00:00Z",
    "updated_at": "2026-11-09T10:00:00Z",
    "version": 1,
    "history": [{"reason": "private history"}],
}

PRODUCT_RESPONSE = {
    "id": 17,
    "name": "Widget",
    "sku": "WIDGET-17",
    "client_id": "private-client",
    "client_name": "Private Client",
    "category": "electronics",
    "warehouse": "LA",
    "minimum_stock_threshold": 9,
    "current_stock": 4,
}

CREATE_INPUT = {
    "client_id": "client-a",
    "incident_date": "2026-11-09",
    "country": "US",
    "customer_type": "B2C",
    "tracking_number": "TRACK-123456",
    "carrier": "UPS",
    "category": "DELAYED_DELIVERY",
    "description": "sensitive description",
    "customer_email": "customer@example.com",
    "source_channel": "email",
    "source_reference": "mail-123",
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class RecordingExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def exchange(
        self, subject_token: str, *, scopes: tuple[str, ...]
    ) -> DownstreamAccessToken:
        self.calls.append((subject_token, scopes))
        return DownstreamAccessToken(
            SecretStr("downstream-trackflow-api-token"), "Bearer", 60
        )


def authenticated_call(
    app: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    scopes: tuple[str, ...],
    token: str = "external-mcp-token",
) -> Any:
    runtime = app.state.auth_runtime
    reset = runtime.context.set(
        AuthInfo(
            token=token,
            issuer=MCPSettings.local_qa().issuer,
            client_id="mcp-playground",
            scopes=list(scopes),
            subject="external-subject",
            audience="trackflow-mcp",
            claims={"trackflow_user_id": "trackflow-user-42"},
        )
    )
    try:
        raw_result = run(app.state.mcp_server.call_tool(name, arguments))
        if isinstance(raw_result, tuple):
            return raw_result[1]
        return raw_result
    finally:
        runtime.context.reset(reset)


def make_app(handler) -> tuple[Any, RecordingExchange, httpx.AsyncClient]:
    exchange = RecordingExchange()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        MCPSettings.local_qa(),
        token_exchange_client=exchange,
        http_client=http,
    )
    return app, exchange, http


def test_discovery_exposes_only_five_named_described_tools_and_real_schemas() -> None:
    app, _exchange, http = make_app(lambda _request: httpx.Response(500))
    tools = {tool.name: tool for tool in run(app.state.mcp_server.list_tools())}

    assert set(tools) == {
        "create_incident",
        "get_incident",
        "update_incident_status",
        "list_inventory_products",
        "get_inventory_product",
    }
    assert all(tool.description for tool in tools.values())
    create_schema = tools["create_incident"].inputSchema["$defs"]["CreateIncidentInput"]
    assert set(create_schema["properties"]) == set(CREATE_INPUT)
    assert create_schema["additionalProperties"] is False
    status_schema = tools["update_incident_status"].inputSchema["$defs"][
        "UpdateIncidentStatusInput"
    ]
    assert set(status_schema["properties"]) == {
        "incident_id",
        "expected_version",
        "status",
        "reason",
        "satisfaction_score",
    }
    assert tools["list_inventory_products"].inputSchema["properties"] == {}
    run(http.aclose())


def test_unknown_inventory_mutation_tool_is_rejected_before_exchange_and_network() -> None:
    network_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    app, exchange, http = make_app(handler)

    with pytest.raises(ToolError, match=r"^Unknown tool: update_inventory_product$"):
        authenticated_call(
            app,
            "update_inventory_product",
            {"input": {"product_id": 91001, "current_stock": 999}},
            scopes=(INVENTORY_READ_SCOPE,),
        )

    assert exchange.calls == []
    assert network_calls == 0
    run(http.aclose())


def test_scope_is_rejected_before_exchange_and_network() -> None:
    network_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    app, exchange, http = make_app(handler)

    with pytest.raises(ToolError, match="forbidden"):
        authenticated_call(
            app,
            "create_incident",
            {"input": CREATE_INPUT},
            scopes=(INCIDENTS_READ_SCOPE,),
        )

    assert exchange.calls == []
    assert network_calls == 0
    run(http.aclose())


def test_incident_tools_use_exact_routes_methods_payloads_and_downstream_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = dict(INCIDENT_RESPONSE)
        if request.method == "PATCH":
            response.update({"status": "CLOSED", "version": 2})
        return httpx.Response(201 if request.method == "POST" else 200, json=response)

    app, exchange, http = make_app(handler)
    created = authenticated_call(
        app,
        "create_incident",
        {"input": CREATE_INPUT},
        scopes=(INCIDENTS_CREATE_SCOPE,),
    )
    read = authenticated_call(
        app,
        "get_incident",
        {"input": {"incident_id": "TRF-123456"}},
        scopes=(INCIDENTS_READ_SCOPE,),
    )
    updated = authenticated_call(
        app,
        "update_incident_status",
        {
            "input": {
                "incident_id": "TRF-123456",
                "expected_version": 1,
                "status": "CLOSED",
                "reason": "Delivered after review",
                "satisfaction_score": 4,
            }
        },
        scopes=(INCIDENTS_STATUS_SCOPE,),
    )

    assert [(item.method, item.url.path) for item in requests] == [
        ("POST", "/api/incidents"),
        ("GET", "/api/incidents/TRF-123456"),
        ("PATCH", "/api/incidents/TRF-123456/status"),
    ]
    assert requests[1].content == b""
    assert json.loads(requests[0].content) == CREATE_INPUT
    assert json.loads(requests[2].content) == {
        "expected_version": 1,
        "status": "CLOSED",
        "reason": "Delivered after review",
        "satisfaction_score": 4,
    }
    assert all(
        item.headers["Authorization"] == "Bearer downstream-trackflow-api-token"
        for item in requests
    )
    assert all("external-mcp-token" not in str(dict(item.headers)) for item in requests)
    assert exchange.calls == [
        ("external-mcp-token", (INCIDENTS_CREATE_SCOPE,)),
        ("external-mcp-token", (INCIDENTS_READ_SCOPE,)),
        ("external-mcp-token", (INCIDENTS_STATUS_SCOPE,)),
    ]
    expected_fields = {
        "incident_id",
        "category",
        "status",
        "created_at",
        "updated_at",
        "version",
    }
    assert set(created) == expected_fields
    assert set(read) == expected_fields
    assert set(updated) == expected_fields
    assert not expected_fields.intersection({"description", "customer_email", "history"})
    run(http.aclose())


def test_inventory_tools_are_get_only_and_return_safe_projections() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/inventory/products":
            return httpx.Response(200, json=[PRODUCT_RESPONSE])
        return httpx.Response(200, json=PRODUCT_RESPONSE)

    app, exchange, http = make_app(handler)
    listed = authenticated_call(
        app, "list_inventory_products", {}, scopes=(INVENTORY_READ_SCOPE,)
    )
    product = authenticated_call(
        app,
        "get_inventory_product",
        {"input": {"product_id": 17}},
        scopes=(INVENTORY_READ_SCOPE,),
    )

    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", "/inventory/products"),
        ("GET", "/inventory/products/17"),
    ]
    safe_fields = {"id", "name", "sku", "category", "warehouse", "current_stock"}
    assert set(listed["result"][0]) == safe_fields
    assert set(product) == safe_fields
    assert exchange.calls == [
        ("external-mcp-token", (INVENTORY_READ_SCOPE,)),
        ("external-mcp-token", (INVENTORY_READ_SCOPE,)),
    ]
    run(http.aclose())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_inventory_adapter_rejects_mutation_before_network(method: str) -> None:
    network_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = InventoryReadAdapter(
        TrackFlowAPIClient("http://127.0.0.1:8000", timeout_seconds=1, client=http)
    )

    with pytest.raises(DownstreamError) as rejected:
        run(
            adapter.request(
                method,
                "/inventory/products",
                token=SecretStr("downstream-token"),
                correlation_id="correlation",
            )
        )

    assert rejected.value.category == "inventory_read_only"
    assert network_calls == 0
    run(http.aclose())


@pytest.mark.parametrize(
    "path",
    ["/auth/me", "/inventory/orders", "/inventory/products/0", "/inventory/products/-1"],
)
def test_inventory_adapter_rejects_arbitrary_get_before_network(path: str) -> None:
    network_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = InventoryReadAdapter(
        TrackFlowAPIClient("http://127.0.0.1:8000", timeout_seconds=1, client=http)
    )
    with pytest.raises(DownstreamError) as rejected:
        run(
            adapter.request(
                "GET",
                path,
                token=SecretStr("downstream-token"),
                correlation_id="correlation",
            )
        )
    assert rejected.value.category == "inventory_read_only"
    assert network_calls == 0
    run(http.aclose())


@pytest.mark.parametrize(
    "status_code,category",
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (422, "validation"),
        (503, "unavailable"),
    ],
)
def test_downstream_http_errors_are_sanitized(
    status_code: int, category: str
) -> None:
    secret_body = "private-provider-body"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=secret_body)

    app, _exchange, http = make_app(handler)

    with pytest.raises(ToolError) as rejected:
        authenticated_call(
            app,
            "get_incident",
            {"input": {"incident_id": "TRF-123456"}},
            scopes=(INCIDENTS_READ_SCOPE,),
        )

    assert f": {category}:" in str(rejected.value)
    assert secret_body not in str(rejected.value)
    run(http.aclose())


def test_not_found_message_does_not_distinguish_absent_from_inaccessible() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "tenant-specific secret"})

    app, _exchange, http = make_app(handler)
    with pytest.raises(ToolError) as rejected:
        authenticated_call(
            app,
            "get_incident",
            {"input": {"incident_id": "TRF-123456"}},
            scopes=(INCIDENTS_READ_SCOPE,),
        )
    assert str(rejected.value).endswith(
        "not_found: Resource was not found or is not accessible."
    )
    run(http.aclose())


def test_timeout_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout diagnostic", request=request)

    app, _exchange, http = make_app(handler)
    with pytest.raises(ToolError) as rejected:
        authenticated_call(
            app,
            "get_inventory_product",
            {"input": {"product_id": 17}},
            scopes=(INVENTORY_READ_SCOPE,),
        )
    assert str(rejected.value).endswith("timeout: TrackFlow API timed out.")
    run(http.aclose())


@pytest.mark.parametrize(
    "tool,arguments,scopes,response",
    [
        (
            "get_incident",
            {"input": {"incident_id": "TRF-123456"}},
            (INCIDENTS_READ_SCOPE,),
            {**INCIDENT_RESPONSE, "status": "UNSUPPORTED_STATUS"},
        ),
        (
            "get_inventory_product",
            {"input": {"product_id": 17}},
            (INVENTORY_READ_SCOPE,),
            {**PRODUCT_RESPONSE, "warehouse": "UNSUPPORTED_WAREHOUSE"},
        ),
    ],
)
def test_downstream_contract_drift_is_sanitized_as_unavailable(
    tool: str,
    arguments: dict[str, Any],
    scopes: tuple[str, ...],
    response: dict[str, Any],
) -> None:
    app, _exchange, http = make_app(
        lambda _request: httpx.Response(200, json=response)
    )
    with pytest.raises(ToolError) as rejected:
        authenticated_call(app, tool, arguments, scopes=scopes)
    assert str(rejected.value).endswith(
        "unavailable: TrackFlow API returned an invalid response."
    )
    assert "UNSUPPORTED_" not in str(rejected.value)
    run(http.aclose())


def test_structured_log_and_correlation_header_exclude_tokens_payload_and_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed_correlation: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_correlation.append(request.headers["X-Request-ID"])
        return httpx.Response(
            201,
            json=INCIDENT_RESPONSE,
            headers={"X-Request-ID": request.headers["X-Request-ID"]},
        )

    app, _exchange, http = make_app(handler)
    caplog.set_level(logging.INFO, logger="trackflow_mcp.tools")

    authenticated_call(
        app,
        "create_incident",
        {"input": CREATE_INPUT},
        scopes=(INCIDENTS_CREATE_SCOPE,),
    )

    records = [record for record in caplog.records if record.message == "trackflow_mcp_tool"]
    assert len(records) == 1
    record = records[0]
    assert record.correlation_id == observed_correlation[0]
    assert UUID(record.correlation_id).version == 4
    assert record.tool == "create_incident"
    assert record.result == "success"
    assert record.downstream_status == 201
    assert isinstance(record.duration_ms, float)
    serialized = caplog.text + repr(record.__dict__)
    for secret in (
        "external-mcp-token",
        "downstream-trackflow-api-token",
        "customer@example.com",
        "sensitive description",
        "SECRET-TRACKING",
    ):
        assert secret not in serialized
    run(http.aclose())
