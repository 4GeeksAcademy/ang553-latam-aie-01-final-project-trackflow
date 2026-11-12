"""FastMCP tool registration for TrackFlow's approved operations."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import TypeVar
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .auth import AuthRuntime, MCPIdentityError, MCPPermissionDenied, require_scopes
from .contracts import (
    CreateIncidentInput,
    GetIncidentInput,
    GetInventoryProductInput,
    IncidentProjection,
    InventoryProductProjection,
    UpdateIncidentStatusInput,
)
from .downstream import DownstreamError, DownstreamResult, InventoryReadAdapter, TrackFlowAPIClient
from .settings import (
    INCIDENTS_CREATE_SCOPE,
    INCIDENTS_READ_SCOPE,
    INCIDENTS_STATUS_SCOPE,
    INVENTORY_READ_SCOPE,
)
from .token_exchange import (
    KeycloakTokenExchangeClient,
    TokenExchangeError,
    exchange_for_downstream,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ToolExecutor:
    def __init__(
        self,
        runtime: AuthRuntime,
        token_exchange: KeycloakTokenExchangeClient,
        incidents: TrackFlowAPIClient,
        inventory: InventoryReadAdapter,
    ) -> None:
        self.runtime = runtime
        self.token_exchange = token_exchange
        self.incidents = incidents
        self.inventory = inventory

    async def execute(
        self,
        tool: str,
        scope: str,
        operation: Callable[[object, str], Awaitable[DownstreamResult]],
    ) -> T:
        correlation_id = str(uuid4())
        started = monotonic()
        result_name = "error"
        downstream_status: int | None = None
        try:
            require_scopes(self.runtime, scope)
            downstream_token = await exchange_for_downstream(
                self.runtime, self.token_exchange, scopes=(scope,)
            )
            response = await operation(downstream_token.access_token, correlation_id)
            downstream_status = response.status_code
            result_name = "success"
            return response.data
        except MCPPermissionDenied:
            result_name = "forbidden"
            raise ToolError("forbidden: operation is not authorized") from None
        except MCPIdentityError:
            result_name = "unauthorized"
            raise ToolError("unauthorized: authenticated identity is unavailable") from None
        except TokenExchangeError:
            result_name = "unavailable"
            raise ToolError("unavailable: downstream authentication failed") from None
        except DownstreamError as exc:
            result_name = exc.category
            downstream_status = exc.downstream_status
            raise ToolError(f"{exc.category}: {exc}") from None
        finally:
            logger.info(
                "trackflow_mcp_tool",
                extra={
                    "correlation_id": correlation_id,
                    "tool": tool,
                    "result": result_name,
                    "duration_ms": round((monotonic() - started) * 1000, 3),
                    "downstream_status": downstream_status,
                },
            )


def register_tools(mcp: FastMCP, executor: ToolExecutor) -> None:
    @mcp.tool(
        name="create_incident",
        description="Create a TrackFlow operational incident for an authorized client.",
    )
    async def create_incident(input: CreateIncidentInput) -> IncidentProjection:
        return await executor.execute(
            "create_incident",
            INCIDENTS_CREATE_SCOPE,
            lambda token, correlation_id: executor.incidents.create_incident(
                input.model_dump(mode="json"),
                token=token,
                correlation_id=correlation_id,
            ),
        )

    @mcp.tool(
        name="get_incident",
        description="Read one accessible TrackFlow incident by its incident ID.",
    )
    async def get_incident(input: GetIncidentInput) -> IncidentProjection:
        return await executor.execute(
            "get_incident",
            INCIDENTS_READ_SCOPE,
            lambda token, correlation_id: executor.incidents.get_incident(
                input.incident_id, token=token, correlation_id=correlation_id
            ),
        )

    @mcp.tool(
        name="update_incident_status",
        description="Change an incident status using optimistic expected_version concurrency.",
    )
    async def update_incident_status(
        input: UpdateIncidentStatusInput,
    ) -> IncidentProjection:
        return await executor.execute(
            "update_incident_status",
            INCIDENTS_STATUS_SCOPE,
            lambda token, correlation_id: executor.incidents.update_incident_status(
                input.incident_id,
                input.api_payload(),
                token=token,
                correlation_id=correlation_id,
            ),
        )

    @mcp.tool(
        name="list_inventory_products",
        description="List accessible inventory products with their computed current stock.",
    )
    async def list_inventory_products() -> list[InventoryProductProjection]:
        return await executor.execute(
            "list_inventory_products",
            INVENTORY_READ_SCOPE,
            lambda token, correlation_id: executor.inventory.list_products(
                token=token, correlation_id=correlation_id
            ),
        )

    @mcp.tool(
        name="get_inventory_product",
        description="Read one accessible inventory product and its computed current stock.",
    )
    async def get_inventory_product(
        input: GetInventoryProductInput,
    ) -> InventoryProductProjection:
        return await executor.execute(
            "get_inventory_product",
            INVENTORY_READ_SCOPE,
            lambda token, correlation_id: executor.inventory.get_product(
                input.product_id, token=token, correlation_id=correlation_id
            ),
        )
