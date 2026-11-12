from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from mcpauth import AuthInfo

from trackflow_mcp.auth import (
    MCPIdentityError,
    MCPPermissionDenied,
    build_auth_runtime,
    external_identity,
)
from trackflow_mcp.settings import MCPSettings
from trackflow_mcp.settings import INCIDENTS_READ_SCOPE
from trackflow_mcp.token_exchange import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT,
    KeycloakTokenExchangeClient,
    TokenExchangeConfig,
    TokenExchangeError,
    exchange_for_downstream,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def config() -> TokenExchangeConfig:
    return TokenExchangeConfig(
        token_endpoint="http://127.0.0.1:8080/realms/trackflow/protocol/openid-connect/token",
        client_id="trackflow-mcp-confidential",
        client_secret=SecretStr("client-secret-value"),
    )


def test_keycloak_v2_exchange_uses_fixed_audience_and_no_resource_parameter() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["form"] = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "distinct-downstream-token",
                "token_type": "Bearer",
                "expires_in": 120,
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await KeycloakTokenExchangeClient(config(), client=http).exchange(
                "external-subject-token", scopes=(INCIDENTS_READ_SCOPE,)
            )

    token = run(exercise())
    form = captured["form"]

    assert form == {
        "grant_type": [TOKEN_EXCHANGE_GRANT],
        "subject_token": ["external-subject-token"],
        "subject_token_type": [ACCESS_TOKEN_TYPE],
        "requested_token_type": [ACCESS_TOKEN_TYPE],
        "audience": ["trackflow-api"],
        "scope": [INCIDENTS_READ_SCOPE],
    }
    assert "resource" not in form
    expected_basic = base64.b64encode(
        b"trackflow-mcp-confidential:client-secret-value"
    ).decode()
    assert captured["authorization"] == f"Basic {expected_basic}"
    assert token.access_token.get_secret_value() == "distinct-downstream-token"
    assert "distinct-downstream-token" not in repr(token)


def test_exchange_rejects_passthrough_token() -> None:
    subject_token = "same-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": subject_token, "token_type": "Bearer", "expires_in": 60},
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await KeycloakTokenExchangeClient(config(), client=http).exchange(
                subject_token, scopes=()
            )

    with pytest.raises(TokenExchangeError) as rejected:
        run(exercise())
    assert rejected.value.category == "invalid_response"
    assert subject_token not in str(rejected.value)


def test_provider_error_is_sanitized() -> None:
    provider_body = "provider-secret-diagnostic"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=provider_body)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await KeycloakTokenExchangeClient(config(), client=http).exchange(
                "external-token", scopes=()
            )

    with pytest.raises(TokenExchangeError) as rejected:
        run(exercise())
    assert rejected.value.category == "provider_rejected"
    assert rejected.value.status_code == 403
    assert provider_body not in str(rejected.value)
    assert "external-token" not in str(rejected.value)
    assert rejected.value.__cause__ is None


def test_unsupported_scope_is_rejected_before_network() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await KeycloakTokenExchangeClient(config(), client=http).exchange(
                "external-token", scopes=("admin",)
            )

    with pytest.raises(ValueError, match="Unsupported"):
        run(exercise())
    assert called is False


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"audience": "other-api"}, "audience"),
        ({"token_endpoint": "http://idp.example/token"}, "HTTPS"),
        ({"token_endpoint": "ftp://127.0.0.1/token"}, "HTTPS"),
        ({"token_endpoint": "https://client:secret@idp.example/token"}, "absolute"),
        ({"timeout_seconds": 0}, "positive"),
    ],
)
def test_exchange_configuration_is_fail_closed(
    changes: dict[str, Any], message: str
) -> None:
    values = {**config().__dict__, **changes}
    with pytest.raises(ValueError, match=message):
        TokenExchangeConfig(**values)


def test_exchange_config_repr_does_not_disclose_secret() -> None:
    assert "client-secret-value" not in repr(config())


def test_contextual_exchange_keeps_raw_token_out_of_identity_and_result_repr() -> None:
    raw_token = "raw-external-token"
    captured_subject_token: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_subject_token.extend(parse_qs(request.content.decode())["subject_token"])
        return httpx.Response(
            200,
            json={
                "access_token": "downstream-token",
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    async def exercise():
        settings = MCPSettings.local_qa()
        runtime = build_auth_runtime(settings)
        reset = runtime.context.set(
            AuthInfo(
                token=raw_token,
                issuer=settings.issuer,
                client_id="mcp-playground",
                scopes=[INCIDENTS_READ_SCOPE],
                subject="external-subject",
                audience=settings.audience,
                claims={"trackflow_user_id": "trackflow-user"},
            )
        )
        try:
            identity = external_identity(runtime)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                result = await exchange_for_downstream(
                    runtime,
                    KeycloakTokenExchangeClient(config(), client=http),
                    scopes=(INCIDENTS_READ_SCOPE,),
                )
            return identity, result
        finally:
            runtime.context.reset(reset)

    identity, result = run(exercise())

    assert captured_subject_token == [raw_token]
    assert not hasattr(identity, "token")
    assert raw_token not in repr(identity)
    assert raw_token not in repr(result)
    assert "downstream-token" not in repr(result)


def test_contextual_exchange_requires_authenticated_context() -> None:
    runtime = build_auth_runtime(MCPSettings.local_qa())

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500))) as http:
            return await exchange_for_downstream(
                runtime,
                KeycloakTokenExchangeClient(config(), client=http),
                scopes=(INCIDENTS_READ_SCOPE,),
            )

    with pytest.raises(MCPIdentityError) as missing:
        run(exercise())
    assert missing.value.status_code == 403


def test_contextual_exchange_cannot_escalate_beyond_subject_scopes() -> None:
    called = False
    settings = MCPSettings.local_qa()
    runtime = build_auth_runtime(settings)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def exercise():
        reset = runtime.context.set(
            AuthInfo(
                token="external-token",
                issuer=settings.issuer,
                client_id="mcp-playground",
                scopes=[INCIDENTS_READ_SCOPE],
                subject="external-subject",
                audience=settings.audience,
                claims={"trackflow_user_id": "trackflow-user"},
            )
        )
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                return await exchange_for_downstream(
                    runtime,
                    KeycloakTokenExchangeClient(config(), client=http),
                    scopes=("incidents:status",),
                )
        finally:
            runtime.context.reset(reset)

    with pytest.raises(MCPPermissionDenied) as denied:
        run(exercise())
    assert denied.value.status_code == 403
    assert denied.value.missing_scopes == ("incidents:status",)
    assert called is False
