"""Streamable HTTP application factory for the TrackFlow MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
import httpx
from starlette.applications import Starlette

from .auth import build_auth_runtime, protect_streamable_http_app
from .downstream import InventoryReadAdapter, TrackFlowAPIClient
from .settings import MCPConfigurationError, MCPSettings
from .token_exchange import KeycloakTokenExchangeClient, TokenExchangeConfig
from .tools import ToolExecutor, register_tools


def create_app(
    settings: MCPSettings,
    *,
    token_exchange_client: KeycloakTokenExchangeClient | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    """Create an OAuth-protected MCP app without FastMCP auth helpers."""

    mcp = FastMCP(
        "TrackFlow",
        instructions="Authorized TrackFlow incident and inventory operations.",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    streamable_app = mcp.streamable_http_app()
    runtime = build_auth_runtime(settings)
    if token_exchange_client is None:
        if settings.keycloak_client_id is None or settings.keycloak_client_secret is None:
            raise MCPConfigurationError(
                "Keycloak confidential-client credentials are required for MCP tools."
            )
        token_exchange_client = KeycloakTokenExchangeClient(
            TokenExchangeConfig(
                token_endpoint=settings.token_endpoint,
                client_id=settings.keycloak_client_id,
                client_secret=settings.keycloak_client_secret,
                audience=settings.downstream_audience,
                timeout_seconds=settings.downstream_timeout_seconds,
            )
        )
    api_client = TrackFlowAPIClient(
        settings.api_base_url,
        timeout_seconds=settings.downstream_timeout_seconds,
        client=http_client,
    )
    executor = ToolExecutor(
        runtime,
        token_exchange_client,
        api_client,
        InventoryReadAdapter(api_client),
    )
    register_tools(mcp, executor)
    app = protect_streamable_http_app(
        streamable_app,
        settings,
        runtime,
        lifespan=streamable_app.router.lifespan_context,
    )
    app.state.mcp_server = mcp
    app.state.auth_runtime = runtime
    app.state.tool_executor = executor
    return app
