"""Configuration boundaries for the TrackFlow MCP resource server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import SecretStr

INCIDENTS_CREATE_SCOPE = "incidents:create"
INCIDENTS_READ_SCOPE = "incidents:read"
INCIDENTS_STATUS_SCOPE = "incidents:status"
INVENTORY_READ_SCOPE = "inventory:read"

SUPPORTED_SCOPES: tuple[str, ...] = (
    INCIDENTS_CREATE_SCOPE,
    INCIDENTS_READ_SCOPE,
    INCIDENTS_STATUS_SCOPE,
    INVENTORY_READ_SCOPE,
)


class MCPConfigurationError(ValueError):
    """Raised when the MCP trust boundary is configured unsafely."""


def _validate_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise MCPConfigurationError(f"{name} must be an absolute URL without query or fragment.")
    if parsed.scheme not in {"http", "https"}:
        raise MCPConfigurationError(f"{name} must use HTTPS, or HTTP for loopback QA.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise MCPConfigurationError(f"{name} must use HTTPS outside loopback QA.")
    return value.rstrip("/")


@dataclass(frozen=True)
class MCPSettings:
    """Non-secret OAuth metadata plus optional confidential-client credentials."""

    resource_url: str
    issuer: str
    audience: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    api_base_url: str
    downstream_timeout_seconds: float = 5.0
    downstream_audience: str = "trackflow-api"
    keycloak_client_id: str | None = None
    keycloak_client_secret: SecretStr | None = None

    def __post_init__(self) -> None:
        for name in (
            "resource_url",
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "api_base_url",
        ):
            object.__setattr__(self, name, _validate_url(name, getattr(self, name)))
        if urlparse(self.resource_url).path.rstrip("/") != "/mcp":
            raise MCPConfigurationError("resource_url must identify the Streamable HTTP /mcp endpoint.")
        if not self.audience.strip():
            raise MCPConfigurationError("audience must be configured.")
        if self.downstream_audience != "trackflow-api":
            raise MCPConfigurationError("downstream_audience must remain trackflow-api.")
        if self.downstream_timeout_seconds <= 0:
            raise MCPConfigurationError("downstream_timeout_seconds must be positive.")
        if bool(self.keycloak_client_id) != bool(self.keycloak_client_secret):
            raise MCPConfigurationError(
                "Keycloak token exchange requires both client ID and client secret, or neither."
            )

    @classmethod
    def local_qa(cls) -> "MCPSettings":
        issuer = "http://127.0.0.1:8080/realms/trackflow"
        protocol = f"{issuer}/protocol/openid-connect"
        return cls(
            resource_url="http://127.0.0.1:8010/mcp",
            issuer=issuer,
            audience="trackflow-mcp",
            authorization_endpoint=f"{protocol}/auth",
            token_endpoint=f"{protocol}/token",
            jwks_uri=f"{protocol}/certs",
            api_base_url="http://127.0.0.1:8000",
        )

    @classmethod
    def from_env(cls) -> "MCPSettings":
        issuer = os.environ["TRACKFLOW_MCP_ISSUER"].rstrip("/")
        protocol = f"{issuer}/protocol/openid-connect"
        secret = os.getenv("TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET")
        return cls(
            resource_url=os.environ["TRACKFLOW_MCP_RESOURCE_URL"],
            issuer=issuer,
            audience=os.environ["TRACKFLOW_MCP_AUDIENCE"],
            authorization_endpoint=os.getenv(
                "TRACKFLOW_MCP_AUTHORIZATION_ENDPOINT", f"{protocol}/auth"
            ),
            token_endpoint=os.getenv("TRACKFLOW_MCP_TOKEN_ENDPOINT", f"{protocol}/token"),
            jwks_uri=os.getenv("TRACKFLOW_MCP_JWKS_URI", f"{protocol}/certs"),
            api_base_url=os.environ["TRACKFLOW_API_BASE_URL"],
            downstream_timeout_seconds=float(
                os.getenv("TRACKFLOW_MCP_DOWNSTREAM_TIMEOUT_SECONDS", "5")
            ),
            keycloak_client_id=os.getenv("TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID"),
            keycloak_client_secret=SecretStr(secret) if secret else None,
        )
