from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
from time import monotonic

import pytest
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import SecretStr

from agents.customer_support.mcp_tools import (
    SupportMCPSettings,
    SupportMCPTools,
    _tool_error_category,
)
import agents.customer_support.external_graph as external_graph
from agents.customer_support.external_graph import ExternalSupportAgent, SupportUnavailable
from agents.customer_support.tools import (
    InventoryLookupInput,
    TicketLookupInput,
    ToolStatus,
)
from services.rag.resilience import QueryDeadline
from services.api.access_control import AccessContext
from services.api.auth_models import Role


class Session:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        return self.results[name]


class Client:
    def __init__(self, tools):
        self.tools = tools
        self.calls = 0

    async def get_tools(self, *, server_name=None):
        self.calls += 1
        assert server_name == "trackflow"
        return self.tools


def converted(session: Session, name: str):
    return convert_mcp_tool_to_langchain_tool(
        session,
        Tool(
            name=name,
            description=f"QA {name}",
            inputSchema={"type": "object", "properties": {"input": {"type": "object"}}},
        ),
        server_name="trackflow",
    )


def test_real_adapter_returns_tool_message_with_structured_artifact() -> None:
    session = Session(
        {
            "get_incident": CallToolResult(
                content=[TextContent(type="text", text="structured response")],
                structuredContent={"incident_id": "TRF-000001", "version": 1},
            )
        }
    )
    tool = converted(session, "get_incident")

    result = asyncio.run(
        tool.ainvoke(
            {
                "name": "get_incident",
                "args": {"input": {"incident_id": "TRF-000001"}},
                "id": "qa-call",
                "type": "tool_call",
            }
        )
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert result.artifact == {
        "structured_content": {"incident_id": "TRF-000001", "version": 1}
    }


def test_graph_has_no_legacy_operational_lookup_fallback() -> None:
    source = inspect.getsource(external_graph)
    assert "lookup_ticket" not in source
    assert "lookup_inventory_product" not in source
    assert "graph.ainvoke" in source
    assert "graph.invoke(" not in source


def test_support_adapter_projects_both_mcp_read_tools_and_reuses_discovery() -> None:
    now = datetime(2026, 11, 10, tzinfo=timezone.utc).isoformat()
    session = Session(
        {
            "get_incident": CallToolResult(
                content=[TextContent(type="text", text="ok")],
                structuredContent={
                    "incident_id": "TRF-000001",
                    "category": "DAMAGE",
                    "status": "OPEN",
                    "created_at": now,
                    "updated_at": now,
                    "version": 2,
                },
            ),
            "get_inventory_product": CallToolResult(
                content=[TextContent(type="text", text="ok")],
                structuredContent={
                    "id": 17,
                    "name": "Widget",
                    "sku": "W-17",
                    "category": "fashion",
                    "warehouse": "LA",
                    "current_stock": 4,
                },
            ),
        }
    )
    client = Client(
        [
            converted(session, "get_incident"),
            converted(session, "get_inventory_product"),
        ]
    )
    adapter = SupportMCPTools(
        SecretStr("subject-token"),
        SupportMCPSettings("http://127.0.0.1:18084/mcp", 2),
        client=client,
    )

    async def scenario():
        ticket = await adapter.lookup_ticket(
            TicketLookupInput("TRF-000001"), QueryDeadline(20)
        )
        product = await adapter.lookup_inventory_product(
            InventoryLookupInput(17), QueryDeadline(20)
        )
        return ticket, product

    ticket, product = asyncio.run(scenario())
    assert ticket.status is ToolStatus.ok and ticket.version == 2
    assert product.status is ToolStatus.ok and product.current_stock == 4
    assert client.calls == 1
    assert session.calls == [
        ("get_incident", {"input": {"incident_id": "TRF-000001"}}),
        ("get_inventory_product", {"input": {"product_id": 17}}),
    ]


def test_not_found_and_mcp_outage_are_sanitized() -> None:
    not_found = Session(
        {
            "get_incident": CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "Error executing tool get_incident: not_found: "
                            "Resource was not found or is not accessible."
                        ),
                    )
                ],
            )
        }
    )
    client = Client(
        [
            converted(not_found, "get_incident"),
            converted(not_found, "get_inventory_product"),
        ]
    )
    adapter = SupportMCPTools(
        SecretStr("never-logged"),
        SupportMCPSettings("http://127.0.0.1:18084/mcp", 1),
        client=client,
    )
    result = asyncio.run(
        adapter.lookup_ticket(TicketLookupInput("TRF-000999"), QueryDeadline(10))
    )
    assert result.status is ToolStatus.not_found

    class DownClient:
        async def get_tools(self, *, server_name=None):
            raise ConnectionError("private network diagnostic")

    down = SupportMCPTools(
        SecretStr("never-logged"),
        SupportMCPSettings("http://127.0.0.1:18084/mcp", 1),
        client=DownClient(),
    )
    unavailable = asyncio.run(
        down.lookup_ticket(TicketLookupInput("TRF-000001"), QueryDeadline(10))
    )
    assert unavailable.status is ToolStatus.unavailable


@pytest.mark.parametrize(
    "content",
    (
        [],
        [{}],
        [{"type": "text"}],
        [{"type": "text", "text": 42}],
        [{"type": "image", "data": "ignored"}],
        [{"type": "text", "text": "forbidden: private diagnostic"}],
        [
            {"type": "image", "data": "ignored"},
            {"type": "text", "text": "not_found: must not inspect later blocks"},
        ],
    ),
)
def test_malformed_adapter_error_blocks_fail_closed(content) -> None:
    assert _tool_error_category("get_incident", content) is None


def test_adapter_error_category_requires_exact_tool_wrapper() -> None:
    assert (
        _tool_error_category(
            "get_inventory_product",
            [
                {
                    "type": "text",
                    "text": (
                        "Error executing tool get_inventory_product: timeout: "
                        "Downstream timed out."
                    ),
                }
            ],
        )
        == "timeout"
    )
    assert (
        _tool_error_category(
            "get_incident",
            [
                {
                    "type": "text",
                    "text": (
                        "Error executing tool get_inventory_product: not_found: "
                        "mismatched tool"
                    ),
                }
            ],
        )
        is None
    )


def test_mcp_outage_becomes_sanitized_total_support_failure() -> None:
    class DownClient:
        async def get_tools(self, *, server_name=None):
            raise ConnectionError("private MCP endpoint diagnostic")

    adapter = SupportMCPTools(
        SecretStr("never-logged"),
        SupportMCPSettings("http://127.0.0.1:18084/mcp", 1),
        client=DownClient(),
    )

    with pytest.raises(SupportUnavailable):
        asyncio.run(
            ExternalSupportAgent().execute(
                "Check ticket TRF-000001",
                session=object(),
                access=AccessContext(
                    "admin", Role.admin, frozenset(), frozenset(), True
                ),
                deadline=QueryDeadline(10),
                rag_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("RAG must remain lazy")
                ),
                ticket_tool=adapter.lookup_ticket,
            )
        )


def test_support_mcp_settings_fail_closed() -> None:
    invalid = [
        "ftp://127.0.0.1/mcp",
        "http://example.test/mcp",
        "https://user:secret@example.test/mcp",
        "https://example.test/mcp?token=secret",
        "https://example.test/other",
    ]
    for url in invalid:
        try:
            SupportMCPSettings(url)
        except ValueError:
            continue
        raise AssertionError(f"unsafe URL accepted: {url}")
    for timeout in (float("nan"), float("inf"), 0, -1):
        try:
            SupportMCPSettings("http://127.0.0.1:18084/mcp", timeout)
        except ValueError:
            continue
        raise AssertionError(f"unsafe timeout accepted: {timeout}")


def test_discovery_and_invocation_share_one_timeout_budget() -> None:
    class SlowTool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, call):
            await asyncio.sleep(0.07)
            raise AssertionError("the shared budget should expire")

    class SlowClient:
        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(0.07)
            return [SlowTool("get_incident"), SlowTool("get_inventory_product")]

    adapter = SupportMCPTools(
        SecretStr("never-logged"),
        SupportMCPSettings("http://127.0.0.1:18084/mcp", 0.1),
        client=SlowClient(),
    )
    started = monotonic()
    result = asyncio.run(
        adapter.lookup_ticket(TicketLookupInput("TRF-000001"), QueryDeadline(10))
    )
    elapsed = monotonic() - started
    assert result.status is ToolStatus.timeout
    assert elapsed < 0.17
