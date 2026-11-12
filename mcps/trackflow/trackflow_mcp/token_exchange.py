"""Keycloak Standard Token Exchange V2 for downstream TrackFlow API calls."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from .auth import AuthRuntime, MCPIdentityError, require_scopes
from .settings import SUPPORTED_SCOPES

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class TokenExchangeError(RuntimeError):
    """Sanitized token-exchange failure without provider body or credentials."""

    def __init__(self, category: str, status_code: int | None = None):
        super().__init__("Downstream authentication is unavailable.")
        self.category = category
        self.status_code = status_code


@dataclass(frozen=True)
class TokenExchangeConfig:
    token_endpoint: str
    client_id: str
    client_secret: SecretStr
    audience: str = "trackflow-api"
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.token_endpoint)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("token_endpoint must be an absolute URL.")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("token_endpoint must use HTTPS, or HTTP for loopback QA.")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("token_endpoint must use HTTPS outside loopback QA.")
        if not self.client_id.strip() or not self.client_secret.get_secret_value():
            raise ValueError("Confidential Keycloak client credentials are required.")
        if self.audience != "trackflow-api":
            raise ValueError("The downstream audience must remain trackflow-api.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")


@dataclass(frozen=True)
class DownstreamAccessToken:
    access_token: SecretStr
    token_type: str
    expires_in: int | None


class KeycloakTokenExchangeClient:
    def __init__(
        self,
        config: TokenExchangeConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self._config = config
        self._client = client

    async def exchange(
        self, subject_token: str, *, scopes: tuple[str, ...]
    ) -> DownstreamAccessToken:
        if not subject_token.strip():
            raise ValueError("subject_token is required.")
        if any(scope not in SUPPORTED_SCOPES for scope in scopes):
            raise ValueError("Unsupported downstream scope requested.")

        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": self._config.audience,
        }
        if scopes:
            form["scope"] = " ".join(scopes)

        async def request(client: httpx.AsyncClient) -> httpx.Response:
            try:
                return await client.post(
                    self._config.token_endpoint,
                    data=form,
                    auth=(
                        self._config.client_id,
                        self._config.client_secret.get_secret_value(),
                    ),
                )
            except httpx.TimeoutException:
                raise TokenExchangeError("timeout") from None
            except httpx.HTTPError:
                raise TokenExchangeError("network_error") from None

        if self._client is None:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                response = await request(client)
        else:
            response = await request(self._client)

        if response.status_code < 200 or response.status_code >= 300:
            raise TokenExchangeError("provider_rejected", response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise TokenExchangeError("invalid_response", response.status_code) from None
        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or access_token == subject_token
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or (expires_in is not None and not isinstance(expires_in, int))
        ):
            raise TokenExchangeError("invalid_response", response.status_code)
        return DownstreamAccessToken(
            access_token=SecretStr(access_token),
            token_type="Bearer",
            expires_in=expires_in,
        )


async def exchange_for_downstream(
    runtime: AuthRuntime,
    client: KeycloakTokenExchangeClient,
    *,
    scopes: tuple[str, ...],
) -> DownstreamAccessToken:
    """Exchange the contextual bearer token without exposing it to tool code."""

    info = runtime.context.get()
    if info is None or not isinstance(info.token, str) or not info.token:
        raise MCPIdentityError("Authenticated token is unavailable for exchange.")
    require_scopes(runtime, *scopes)
    return await client.exchange(info.token, scopes=scopes)
