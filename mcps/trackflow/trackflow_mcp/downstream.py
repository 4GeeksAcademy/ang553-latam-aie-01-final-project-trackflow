"""Typed HTTP adapters for the existing TrackFlow API."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from .contracts import IncidentProjection, InventoryProductProjection


class DownstreamError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        downstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.downstream_status = downstream_status


@dataclass(frozen=True)
class DownstreamResult:
    data: Any
    status_code: int


def _http_error(response: httpx.Response) -> DownstreamError:
    status_code = response.status_code
    if status_code == 401:
        return DownstreamError(
            "unauthorized", "Downstream authentication failed.", downstream_status=401
        )
    if status_code == 403:
        return DownstreamError(
            "forbidden", "Operation is not authorized.", downstream_status=403
        )
    if status_code == 404:
        return DownstreamError(
            "not_found",
            "Resource was not found or is not accessible.",
            downstream_status=404,
        )
    if status_code == 409:
        return DownstreamError(
            "conflict", "Resource version conflict.", downstream_status=409
        )
    if status_code == 422:
        return DownstreamError(
            "validation", "TrackFlow rejected the request.", downstream_status=422
        )
    return DownstreamError(
        "unavailable", "TrackFlow API is unavailable.", downstream_status=status_code
    )


class TrackFlowAPIClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: SecretStr,
        correlation_id: str,
        json: dict[str, object] | None = None,
    ) -> DownstreamResult:
        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "X-Request-ID": correlation_id,
        }

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            try:
                kwargs: dict[str, object] = {"headers": headers}
                if json is not None:
                    kwargs["json"] = json
                return await client.request(method, f"{self._base_url}{path}", **kwargs)
            except httpx.TimeoutException:
                raise DownstreamError("timeout", "TrackFlow API timed out.") from None
            except httpx.HTTPError:
                raise DownstreamError(
                    "unavailable", "TrackFlow API is unavailable."
                ) from None

        if self._client is None:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await send(client)
        else:
            response = await send(self._client)
        if not 200 <= response.status_code < 300:
            raise _http_error(response)
        try:
            payload = response.json()
        except ValueError:
            raise DownstreamError(
                "unavailable",
                "TrackFlow API returned an invalid response.",
                downstream_status=response.status_code,
            ) from None
        return DownstreamResult(payload, response.status_code)

    async def create_incident(
        self,
        payload: dict[str, object],
        *,
        token: SecretStr,
        correlation_id: str,
    ) -> DownstreamResult:
        response = await self._request(
            "POST",
            "/api/incidents",
            token=token,
            correlation_id=correlation_id,
            json=payload,
        )
        return self._project_incident(response)

    async def get_incident(
        self,
        incident_id: str,
        *,
        token: SecretStr,
        correlation_id: str,
    ) -> DownstreamResult:
        response = await self._request(
            "GET",
            f"/api/incidents/{incident_id}",
            token=token,
            correlation_id=correlation_id,
        )
        return self._project_incident(response)

    async def update_incident_status(
        self,
        incident_id: str,
        payload: dict[str, object],
        *,
        token: SecretStr,
        correlation_id: str,
    ) -> DownstreamResult:
        response = await self._request(
            "PATCH",
            f"/api/incidents/{incident_id}/status",
            token=token,
            correlation_id=correlation_id,
            json=payload,
        )
        return self._project_incident(response)

    @staticmethod
    def _project_incident(response: DownstreamResult) -> DownstreamResult:
        try:
            projection = IncidentProjection.model_validate(response.data)
        except ValidationError:
            raise DownstreamError(
                "unavailable",
                "TrackFlow API returned an invalid response.",
                downstream_status=response.status_code,
            ) from None
        return DownstreamResult(projection, response.status_code)


class InventoryReadAdapter:
    """GET-only adapter that cannot perform inventory mutations."""

    def __init__(self, api: TrackFlowAPIClient) -> None:
        self._api = api

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: SecretStr,
        correlation_id: str,
    ) -> DownstreamResult:
        if method.upper() != "GET" or not re.fullmatch(
            r"/inventory/products(?:/[1-9]\d*)?", path
        ):
            raise DownstreamError(
                "inventory_read_only", "Inventory mutations are not supported."
            )
        response = await self._api._request(
            "GET", path, token=token, correlation_id=correlation_id
        )
        try:
            if path == "/inventory/products":
                if not isinstance(response.data, list):
                    raise ValueError
                data = [InventoryProductProjection.model_validate(item) for item in response.data]
            else:
                data = InventoryProductProjection.model_validate(response.data)
        except (ValidationError, ValueError, TypeError):
            raise DownstreamError(
                "unavailable",
                "TrackFlow API returned an invalid response.",
                downstream_status=response.status_code,
            ) from None
        return DownstreamResult(data, response.status_code)

    async def list_products(
        self, *, token: SecretStr, correlation_id: str
    ) -> DownstreamResult:
        return await self.request(
            "GET", "/inventory/products", token=token, correlation_id=correlation_id
        )

    async def get_product(
        self, product_id: int, *, token: SecretStr, correlation_id: str
    ) -> DownstreamResult:
        return await self.request(
            "GET",
            f"/inventory/products/{product_id}",
            token=token,
            correlation_id=correlation_id,
        )
