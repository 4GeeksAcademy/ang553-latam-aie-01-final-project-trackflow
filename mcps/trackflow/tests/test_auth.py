from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from trackflow_mcp.app import create_app
from trackflow_mcp.auth import (
    MCPIdentityError,
    MCPPermissionDenied,
    build_auth_runtime,
    external_identity,
    protect_streamable_http_app,
    require_scopes,
)
from trackflow_mcp.settings import (
    INCIDENTS_CREATE_SCOPE,
    INCIDENTS_READ_SCOPE,
    INCIDENTS_STATUS_SCOPE,
    INVENTORY_READ_SCOPE,
    MCPConfigurationError,
    MCPSettings,
    SUPPORTED_SCOPES,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def settings() -> MCPSettings:
    return MCPSettings.local_qa()


@pytest.fixture
def rsa_material(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "qa-key", "use": "sig", "alg": "RS256"})
    jwks = {"keys": [public_jwk]}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _self: jwks)
    return private_key, public_jwk


def issue_token(
    private_key: Any,
    settings: MCPSettings,
    *,
    subject: str = "external-subject",
    user_id: str | None = "trackflow-user-42",
    issuer: str | None = None,
    audience: str | list[str] | None = None,
    scopes: tuple[str, ...] = SUPPORTED_SCOPES,
    expires_delta: int = 300,
    key_id: str = "qa-key",
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer or settings.issuer,
        "sub": subject,
        "aud": audience or settings.audience,
        "iat": now,
        "exp": now + expires_delta,
        "scope": " ".join(scopes),
        "client_id": "mcp-playground",
    }
    if user_id is not None:
        claims["trackflow_user_id"] = user_id
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


def make_test_app(settings: MCPSettings):
    runtime = build_auth_runtime(settings)

    async def identity(_request: Request) -> JSONResponse:
        current = external_identity(runtime)
        return JSONResponse(
            {
                "trackflow_user_id": current.trackflow_user_id,
                "subject": current.subject,
                "issuer": current.issuer,
                "client_id": current.client_id,
                "scopes": sorted(current.scopes),
                "has_roles": hasattr(current, "roles"),
                "has_tenant": hasattr(current, "tenant"),
                "has_token": hasattr(current, "access_token"),
            }
        )

    inner = Starlette(routes=[Route("/mcp", identity)])
    return protect_streamable_http_app(inner, settings, runtime), runtime


async def request(app: Any, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(path, headers=headers)


def test_prm_is_public_and_advertises_exact_resource_scopes(settings: MCPSettings) -> None:
    app, _runtime = make_test_app(settings)
    response = run(request(app, "/.well-known/oauth-protected-resource/mcp"))

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == settings.resource_url
    assert metadata["scopes_supported"] == list(SUPPORTED_SCOPES)
    assert metadata["bearer_methods_supported"] == ["header"]
    assert settings.issuer in str(metadata["authorization_servers"])


def test_missing_and_malformed_bearer_are_401(settings: MCPSettings) -> None:
    app, _runtime = make_test_app(settings)

    missing = run(request(app, "/mcp"))
    malformed = run(request(app, "/mcp", "not-a-jwt"))

    assert missing.status_code == 401
    assert malformed.status_code == 401
    assert "Bearer" in missing.headers["www-authenticate"]
    assert "resource_metadata" in missing.headers["www-authenticate"]


@pytest.mark.parametrize("failure", ["expired", "issuer", "audience", "signature"])
def test_invalid_jwt_claims_or_signature_are_401(
    settings: MCPSettings,
    rsa_material: tuple[Any, dict[str, Any]],
    failure: str,
) -> None:
    private_key, _public_jwk = rsa_material
    kwargs: dict[str, Any] = {}
    signing_key = private_key
    if failure == "expired":
        kwargs["expires_delta"] = -1
    elif failure == "issuer":
        kwargs["issuer"] = "http://127.0.0.1:8080/realms/wrong"
    elif failure == "audience":
        kwargs["audience"] = "another-resource"
    else:
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    app, _runtime = make_test_app(settings)
    token = issue_token(signing_key, settings, **kwargs)
    response = run(request(app, "/mcp", token))

    assert response.status_code == 401


def test_valid_identity_is_safely_projected(
    settings: MCPSettings, rsa_material: tuple[Any, dict[str, Any]]
) -> None:
    private_key, _public_jwk = rsa_material
    app, runtime = make_test_app(settings)
    token = issue_token(private_key, settings)

    response = run(request(app, "/mcp", token))

    assert response.status_code == 200
    assert response.json() == {
        "trackflow_user_id": "trackflow-user-42",
        "subject": "external-subject",
        "issuer": settings.issuer,
        "client_id": "mcp-playground",
        "scopes": sorted(SUPPORTED_SCOPES),
        "has_roles": False,
        "has_tenant": False,
        "has_token": False,
    }
    assert runtime.context.get() is None


def test_missing_trackflow_binding_is_sanitized_403(
    settings: MCPSettings, rsa_material: tuple[Any, dict[str, Any]]
) -> None:
    private_key, _public_jwk = rsa_material
    app, _runtime = make_test_app(settings)
    token = issue_token(private_key, settings, user_id=None)

    response = run(request(app, "/mcp", token))

    assert response.status_code == 403
    assert response.json() == {"error": "identity_not_linked"}
    assert token not in response.text


def test_operation_scope_guard_is_exact(settings: MCPSettings) -> None:
    runtime = build_auth_runtime(settings)
    from mcpauth import AuthInfo

    info = AuthInfo(
        token="raw-token-never-exposed",
        subject="subject",
        issuer=settings.issuer,
        audience=settings.audience,
        scopes=[INCIDENTS_READ_SCOPE],
        claims={"trackflow_user_id": "user"},
        client_id="client",
    )
    reset = runtime.context.set(info)
    try:
        assert require_scopes(runtime, INCIDENTS_READ_SCOPE).trackflow_user_id == "user"
        with pytest.raises(MCPPermissionDenied) as denied:
            require_scopes(runtime, INCIDENTS_STATUS_SCOPE)
        assert denied.value.status_code == 403
        assert denied.value.missing_scopes == (INCIDENTS_STATUS_SCOPE,)
        with pytest.raises(ValueError, match="Unsupported"):
            require_scopes(runtime, "admin")
    finally:
        runtime.context.reset(reset)


def test_no_identity_outside_authenticated_context(settings: MCPSettings) -> None:
    runtime = build_auth_runtime(settings)
    with pytest.raises(MCPIdentityError):
        external_identity(runtime)


def test_sequential_requests_do_not_leak_identity(
    settings: MCPSettings, rsa_material: tuple[Any, dict[str, Any]]
) -> None:
    private_key, _public_jwk = rsa_material
    app, runtime = make_test_app(settings)
    first = issue_token(private_key, settings, subject="one", user_id="user-one")
    second = issue_token(private_key, settings, subject="two", user_id="user-two")

    first_response = run(request(app, "/mcp", first))
    second_response = run(request(app, "/mcp", second))
    missing_response = run(request(app, "/mcp"))

    assert first_response.json()["trackflow_user_id"] == "user-one"
    assert second_response.json()["trackflow_user_id"] == "user-two"
    assert missing_response.status_code == 401
    assert runtime.context.get() is None


def test_concurrent_requests_keep_identity_context_isolated(
    settings: MCPSettings, rsa_material: tuple[Any, dict[str, Any]]
) -> None:
    private_key, _public_jwk = rsa_material
    app, runtime = make_test_app(settings)
    tokens = [
        issue_token(private_key, settings, subject=f"subject-{index}", user_id=f"user-{index}")
        for index in range(8)
    ]

    async def exercise() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await asyncio.gather(
                *(
                    client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
                    for token in tokens
                )
            )

    responses = run(exercise())

    assert [response.status_code for response in responses] == [200] * 8
    assert [response.json()["trackflow_user_id"] for response in responses] == [
        f"user-{index}" for index in range(8)
    ]
    assert runtime.context.get() is None


def test_token_does_not_appear_in_logs_or_errors(
    settings: MCPSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _runtime = make_test_app(settings)
    raw_token = "header.payload.super-secret-signature"
    caplog.set_level(logging.DEBUG)

    response = run(request(app, "/mcp", raw_token))

    assert response.status_code == 401
    assert raw_token not in response.text
    assert raw_token not in caplog.text


def test_configuration_rejects_unsafe_or_incomplete_values() -> None:
    base = MCPSettings.local_qa()
    with pytest.raises(MCPConfigurationError, match="HTTPS"):
        MCPSettings(
            **{**base.__dict__, "resource_url": "http://example.test/mcp"}
        )
    with pytest.raises(MCPConfigurationError, match="HTTPS"):
        MCPSettings(
            **{**base.__dict__, "resource_url": "ftp://127.0.0.1/mcp"}
        )
    with pytest.raises(MCPConfigurationError, match="both client ID and client secret"):
        MCPSettings(**{**base.__dict__, "keycloak_client_id": "client"})
    with pytest.raises(MCPConfigurationError, match="Streamable HTTP"):
        MCPSettings(**{**base.__dict__, "resource_url": "http://127.0.0.1:8010/other"})
    with pytest.raises(MCPConfigurationError, match="absolute URL"):
        MCPSettings(
            **{
                **base.__dict__,
                "token_endpoint": "https://client:secret@idp.example.test/token",
            }
        )

    configured = MCPSettings(
        **{
            **base.__dict__,
            "keycloak_client_id": "client",
            "keycloak_client_secret": SecretStr("never-print-this"),
        }
    )
    assert "never-print-this" not in repr(configured)


def test_configuration_loads_confidential_client_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKFLOW_MCP_RESOURCE_URL", "https://mcp.example.test/mcp")
    monkeypatch.setenv("TRACKFLOW_MCP_ISSUER", "https://idp.example.test/realms/trackflow")
    monkeypatch.setenv("TRACKFLOW_MCP_AUDIENCE", "trackflow-mcp")
    monkeypatch.setenv("TRACKFLOW_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID", "trackflow-mcp-confidential")
    monkeypatch.setenv("TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET", "environment-secret")

    configured = MCPSettings.from_env()

    assert configured.keycloak_client_id == "trackflow-mcp-confidential"
    assert configured.keycloak_client_secret is not None
    assert configured.keycloak_client_secret.get_secret_value() == "environment-secret"
    assert "environment-secret" not in repr(configured)


def test_app_uses_stateless_streamable_http_without_fastmcp_auth_helpers(
    settings: MCPSettings,
) -> None:
    from trackflow_mcp.token_exchange import DownstreamAccessToken

    class FakeExchange:
        async def exchange(self, subject_token: str, *, scopes: tuple[str, ...]):
            return DownstreamAccessToken(SecretStr("downstream-token"), "Bearer", 60)

    app = create_app(settings, token_exchange_client=FakeExchange())
    mcp = app.state.mcp_server

    assert mcp.settings.stateless_http is True
    assert mcp.settings.streamable_http_path == "/mcp"
    assert mcp.settings.auth is None
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == {
        "create_incident",
        "get_incident",
        "update_incident_status",
        "list_inventory_products",
        "get_inventory_product",
    }
    paths = [getattr(route, "path", None) for route in app.routes]
    assert "/.well-known/oauth-protected-resource/mcp" in paths


def test_scope_constants_are_only_the_authorized_operations() -> None:
    assert SUPPORTED_SCOPES == (
        INCIDENTS_CREATE_SCOPE,
        INCIDENTS_READ_SCOPE,
        INCIDENTS_STATUS_SCOPE,
        INVENTORY_READ_SCOPE,
    )
