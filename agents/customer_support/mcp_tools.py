"""Async LangChain MCP adapters for the support graph's operational reads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import math
import os
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4
from uuid import UUID

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agents.customer_support.tools import (
    InventoryLookupInput,
    InventoryLookupResult,
    TicketLookupInput,
    TicketLookupResult,
    TrackingLookupInput,
    TrackingLookupResult,
    ToolStatus,
)
from agents.customer_support.guardrail_contracts import DispatchState
from services.rag.resilience import QueryDeadline


_TOOL_TIMEOUT_RESERVE_SECONDS = 3.0
_LOGGER = logging.getLogger("agent.customer_support.mcp")


def _tool_error_category(name: str, content: object) -> str | None:
    """Extract only the two operational error categories the graph understands."""

    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            candidate = first.get("text")
            if isinstance(candidate, str):
                text = candidate
    if text is None:
        return None

    adapter_prefix = f"Error executing tool {name}: "
    if text.startswith(adapter_prefix):
        text = text[len(adapter_prefix) :]
    category, separator, _detail = text.partition(":")
    if separator and category in {"not_found", "timeout"}:
        return category
    return None


class _IncidentProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    incident_id: str = Field(pattern=r"^TRF-\d{6}$")
    category: Literal[
        "LOST_PARCEL",
        "DELAYED_DELIVERY",
        "WRONG_ADDRESS",
        "RETURN_REQUEST",
        "DAMAGE",
    ]
    status: Literal["OPEN", "CLOSED", "DISCARDED"]
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1, strict=True)


class _InventoryProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: int = Field(gt=0, strict=True)
    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    category: Literal["fashion", "electronics", "cosmetics"]
    warehouse: Literal["LA", "ZGZ"]
    current_stock: int = Field(strict=True)


class _TrackingProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["found", "not_found", "unavailable"]
    tracking_number: str | None = Field(default=None, min_length=1, max_length=128)
    dispatch_state: Literal["recorded_dispatch"] | None = None
    commercial_order_id: UUID | None = None

    @classmethod
    def _empty_failure(cls, value: "_TrackingProjection") -> bool:
        return all(
            item is None
            for item in (
                value.tracking_number,
                value.dispatch_state,
                value.commercial_order_id,
            )
        )

    def model_post_init(self, _context: Any) -> None:
        if self.status == "found":
            if (
                self.tracking_number is None
                or self.dispatch_state != "recorded_dispatch"
                or self.commercial_order_id is None
            ):
                raise ValueError("found tracking requires its safe dispatch projection")
        elif not self._empty_failure(self):
            raise ValueError("non-found tracking must not expose order data")


@dataclass(frozen=True)
class SupportMCPSettings:
    url: str
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/mcp"
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        ):
            raise ValueError("support MCP URL must use HTTPS or loopback HTTP")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("support MCP timeout must be positive")
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @classmethod
    def from_env(cls) -> "SupportMCPSettings":
        url = os.environ["TRACKFLOW_SUPPORT_MCP_URL"]
        timeout = float(os.getenv("TRACKFLOW_SUPPORT_MCP_TIMEOUT_SECONDS", "8"))
        return cls(url, timeout)


class SupportMCPTools:
    """Load and call the three read tools through langchain-mcp-adapters."""

    def __init__(
        self,
        token: SecretStr,
        settings: SupportMCPSettings,
        *,
        client: MultiServerMCPClient | None = None,
    ) -> None:
        self._token = token
        self._settings = settings
        self._client = client or MultiServerMCPClient(
            {
                "trackflow": {
                    "transport": "streamable_http",
                    "url": settings.url,
                    "headers": {
                        "Authorization": f"Bearer {token.get_secret_value()}"
                    },
                    "timeout": settings.timeout_seconds,
                    "sse_read_timeout": settings.timeout_seconds,
                }
            }
        )
        self._tools: dict[str, BaseTool] | None = None
        self._load_lock = asyncio.Lock()

    async def _load_tools(self) -> dict[str, BaseTool]:
        if self._tools is not None:
            return self._tools
        async with self._load_lock:
            if self._tools is None:
                tools = await self._client.get_tools(server_name="trackflow")
                indexed = {tool.name: tool for tool in tools}
                required = {
                    "get_incident",
                    "get_inventory_product",
                    "get_authorized_tracking",
                }
                if not required.issubset(indexed):
                    raise RuntimeError("required MCP read tools are unavailable")
                self._tools = indexed
        return self._tools

    async def _invoke(
        self,
        name: str,
        payload: dict[str, object],
        deadline: QueryDeadline,
    ) -> tuple[ToolStatus, dict[str, Any] | None]:
        available = deadline.remaining() - _TOOL_TIMEOUT_RESERVE_SECONDS
        if available <= 0:
            return ToolStatus.timeout, None
        timeout = min(self._settings.timeout_seconds, available)
        expires_at = monotonic() + timeout
        try:
            tools = await asyncio.wait_for(
                self._load_tools(), timeout=max(0.001, expires_at - monotonic())
            )
            remaining = expires_at - monotonic()
            if remaining <= 0:
                return ToolStatus.timeout, None
            result = await asyncio.wait_for(
                tools[name].ainvoke(
                    {
                        "name": name,
                        "args": {"input": payload},
                        "id": str(uuid4()),
                        "type": "tool_call",
                    }
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return ToolStatus.timeout, None
        except Exception as exc:
            _LOGGER.warning(
                "Support MCP call unavailable | operation=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            return ToolStatus.unavailable, None

        if not isinstance(result, ToolMessage):
            return ToolStatus.unavailable, None
        if result.status == "error":
            category = _tool_error_category(name, result.content)
            if category == "not_found":
                return ToolStatus.not_found, None
            if category == "timeout":
                return ToolStatus.timeout, None
            return ToolStatus.unavailable, None
        artifact = result.artifact
        if not isinstance(artifact, dict):
            return ToolStatus.unavailable, None
        structured = artifact.get("structured_content")
        if not isinstance(structured, dict):
            return ToolStatus.unavailable, None
        return ToolStatus.ok, structured

    async def lookup_ticket(
        self,
        payload: TicketLookupInput,
        deadline: QueryDeadline,
    ) -> TicketLookupResult:
        status, structured = await self._invoke(
            "get_incident", {"incident_id": payload.incident_id}, deadline
        )
        if status is not ToolStatus.ok or structured is None:
            return TicketLookupResult(status=status)
        try:
            incident = _IncidentProjection.model_validate(structured)
        except ValidationError:
            return TicketLookupResult(status=ToolStatus.unavailable)
        if incident.incident_id != payload.incident_id:
            return TicketLookupResult(status=ToolStatus.unavailable)
        return TicketLookupResult(
            status=ToolStatus.ok,
            incident_id=incident.incident_id,
            incident_status=incident.status,
            category=incident.category,
            version=incident.version,
            updated_at=incident.updated_at,
        )

    async def lookup_inventory_product(
        self,
        payload: InventoryLookupInput,
        deadline: QueryDeadline,
    ) -> InventoryLookupResult:
        status, structured = await self._invoke(
            "get_inventory_product", {"product_id": payload.product_id}, deadline
        )
        if status is not ToolStatus.ok or structured is None:
            return InventoryLookupResult(status=status)
        try:
            product = _InventoryProjection.model_validate(structured)
        except ValidationError:
            return InventoryLookupResult(status=ToolStatus.unavailable)
        if product.id != payload.product_id:
            return InventoryLookupResult(status=ToolStatus.unavailable)
        return InventoryLookupResult(
            status=ToolStatus.ok,
            product_id=product.id,
            name=product.name,
            sku=product.sku,
            warehouse=product.warehouse,
            current_stock=product.current_stock,
        )

    async def lookup_tracking(
        self,
        payload: TrackingLookupInput,
        deadline: QueryDeadline,
    ) -> TrackingLookupResult:
        status, structured = await self._invoke(
            "get_authorized_tracking",
            {"tracking_number": payload.tracking_number},
            deadline,
        )
        if status is not ToolStatus.ok or structured is None:
            return TrackingLookupResult(status=status)
        try:
            tracking = _TrackingProjection.model_validate(structured)
        except (ValidationError, ValueError):
            return TrackingLookupResult(status=ToolStatus.unavailable)
        if tracking.status == "not_found":
            return TrackingLookupResult(status=ToolStatus.not_found)
        if tracking.status == "unavailable":
            return TrackingLookupResult(status=ToolStatus.unavailable)
        if tracking.tracking_number != payload.tracking_number:
            return TrackingLookupResult(status=ToolStatus.unavailable)
        return TrackingLookupResult(
            status=ToolStatus.ok,
            tracking_number=tracking.tracking_number,
            dispatch_state=DispatchState.recorded_dispatch,
            commercial_order_id=tracking.commercial_order_id,
        )
