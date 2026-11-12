"""Opt-in real Keycloak/PostgreSQL/API/MCP integration gate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from html import unescape
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.skipif(
    os.getenv("TRACKFLOW_MCP_REAL_QA") != "1",
    reason="requires disposable real QA stack",
)

SCOPES = (
    "incidents:create",
    "incidents:read",
    "incidents:status",
    "inventory:read",
)
VISIBLE_CLIENT_ID = "7f6e3d21-8c4a-4b92-a1d7-5e9f2c6b1048"
VISIBLE_PRODUCT_ID = 91001
HIDDEN_PRODUCT_ID = 91002
BACKOFFICE_CALLBACK = "http://127.0.0.1:3001/backoffice/support"


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AssertionError(f"Missing integration environment variable: {name}")
    return value


def password_token(scopes: tuple[str, ...]) -> str:
    response = httpx.post(
        env("QA_TOKEN_ENDPOINT"),
        data={
            "grant_type": "password",
            "client_id": env("QA_KEYCLOAK_PUBLIC_CLIENT_ID"),
            "username": env("QA_KEYCLOAK_USERNAME"),
            "password": env("QA_KEYCLOAK_PASSWORD"),
            "scope": " ".join(scopes),
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def pkce_support_token() -> str:
    """Complete a real Authorization Code + PKCE S256 flow without a browser."""

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    expected_state = secrets.token_urlsafe(32)
    authorization_endpoint = (
        f"{env('QA_ISSUER')}/protocol/openid-connect/auth"
    )
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        login_page = client.get(
            authorization_endpoint,
            params={
                "response_type": "code",
                "client_id": env("QA_KEYCLOAK_PUBLIC_CLIENT_ID"),
                "redirect_uri": BACKOFFICE_CALLBACK,
                "scope": "openid incidents:read inventory:read",
                "state": expected_state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=True,
        )
        login_page.raise_for_status()
        form = re.search(
            r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"',
            login_page.text,
        )
        assert form is not None
        action = unescape(form.group(1))
        action_url = urlparse(action)
        login_url = urlparse(str(login_page.url))
        assert action_url.scheme == login_url.scheme == "http"
        assert action_url.hostname == login_url.hostname in {"127.0.0.1", "localhost"}
        assert action_url.port == login_url.port == 18082
        # Keycloak 26 emits Secure authentication-session cookies even in its
        # loopback-only development mode. Browsers treat loopback as a secure
        # context, while Python's http.cookiejar refuses to resend them over
        # HTTP. Forward the cookies explicitly to reproduce browser semantics;
        # their values remain process-local and are never logged.
        browser_cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar
        )
        assert browser_cookie_header
        authenticated = client.post(
            action,
            data={
                "username": env("QA_KEYCLOAK_USERNAME"),
                "password": env("QA_KEYCLOAK_PASSWORD"),
                "credentialId": "",
            },
            headers={
                "Cookie": browser_cookie_header,
                "Origin": f"{action_url.scheme}://{action_url.netloc}",
                "Referer": str(login_page.url),
            },
            follow_redirects=False,
        )
        assert authenticated.status_code in {302, 303}
        redirect = authenticated.headers["location"]
        parsed = urlparse(redirect)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == BACKOFFICE_CALLBACK
        callback = parse_qs(parsed.query)
        assert callback.get("state") == [expected_state]
        assert "error" not in callback
        code = callback["code"][0]
        exchanged = client.post(
            env("QA_TOKEN_ENDPOINT"),
            data={
                "grant_type": "authorization_code",
                "client_id": env("QA_KEYCLOAK_PUBLIC_CLIENT_ID"),
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": BACKOFFICE_CALLBACK,
            },
        )
        exchanged.raise_for_status()
        payload = exchanged.json()
        assert str(payload["token_type"]).lower() == "bearer"
        return payload["access_token"]


def exchange_token(subject_token: str, scopes: tuple[str, ...]) -> str:
    response = httpx.post(
        env("QA_TOKEN_ENDPOINT"),
        auth=(
            env("QA_KEYCLOAK_MCP_CLIENT_ID"),
            env("QA_KEYCLOAK_MCP_CLIENT_SECRET"),
        ),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "trackflow-api",
            "scope": " ".join(scopes),
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def decode(token: str, audience: str) -> dict[str, object]:
    signing_key = jwt.PyJWKClient(env("QA_JWKS_URI")).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=env("QA_ISSUER"),
        options={"require": ["exp", "sub", "trackflow_user_id"]},
    )


async def call_real_tools(token: str) -> tuple[set[str], str]:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=15
    ) as http:
        async with streamable_http_client(
            env("QA_MCP_URL"), http_client=http
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                discovery = await session.list_tools()
                names = {tool.name for tool in discovery.tools}
                assert names == {
                    "create_incident",
                    "get_incident",
                    "update_incident_status",
                    "list_inventory_products",
                    "get_inventory_product",
                }
                created = await session.call_tool(
                    "create_incident",
                    {
                        "input": {
                            "client_id": VISIBLE_CLIENT_ID,
                            "incident_date": "2026-11-09",
                            "country": "US",
                            "customer_type": "B2C",
                            "tracking_number": "MCP-QA-TRACKING",
                            "carrier": "UPS",
                            "category": "DELAYED_DELIVERY",
                            "description": "Ephemeral real integration incident",
                            "customer_email": "qa@example.invalid",
                            "source_channel": "backoffice",
                            "source_reference": "mcp-real-qa",
                        }
                    },
                )
                assert not created.isError
                incident = created.structuredContent
                assert incident is not None and incident["version"] == 1
                assert set(incident) == {
                    "incident_id",
                    "category",
                    "status",
                    "created_at",
                    "updated_at",
                    "version",
                }
                incident_id = incident["incident_id"]

                read = await session.call_tool(
                    "get_incident", {"input": {"incident_id": incident_id}}
                )
                assert not read.isError and read.structuredContent["version"] == 1
                updated = await session.call_tool(
                    "update_incident_status",
                    {
                        "input": {
                            "incident_id": incident_id,
                            "expected_version": 1,
                            "status": "CLOSED",
                            "reason": "QA lifecycle complete",
                            "satisfaction_score": 5,
                        }
                    },
                )
                assert not updated.isError
                assert updated.structuredContent["status"] == "CLOSED"
                assert updated.structuredContent["version"] == 2
                stale = await session.call_tool(
                    "update_incident_status",
                    {
                        "input": {
                            "incident_id": incident_id,
                            "expected_version": 1,
                            "status": "CLOSED",
                            "reason": "Stale retry must conflict",
                            "satisfaction_score": 5,
                        }
                    },
                )
                assert stale.isError
                assert "conflict" in stale.content[0].text

                products = await session.call_tool("list_inventory_products", {})
                assert not products.isError
                listed = products.structuredContent["result"]
                assert [item["id"] for item in listed] == [VISIBLE_PRODUCT_ID]
                assert set(listed[0]) == {
                    "id",
                    "name",
                    "sku",
                    "category",
                    "warehouse",
                    "current_stock",
                }
                product = await session.call_tool(
                    "get_inventory_product", {"input": {"product_id": VISIBLE_PRODUCT_ID}}
                )
                assert not product.isError
                assert product.structuredContent["current_stock"] == 7
                hidden = await session.call_tool(
                    "get_inventory_product", {"input": {"product_id": HIDDEN_PRODUCT_ID}}
                )
                assert hidden.isError
                assert "not_found" in hidden.content[0].text
                assert "tenant" not in hidden.content[0].text.lower()
                return names, incident_id


async def call_with_insufficient_scope(token: str) -> None:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=15
    ) as http:
        async with streamable_http_client(
            env("QA_MCP_URL"), http_client=http
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                denied = await session.call_tool(
                    "create_incident",
                    {
                        "input": {
                            "client_id": VISIBLE_CLIENT_ID,
                            "incident_date": "2026-11-09",
                            "country": "US",
                            "customer_type": "B2C",
                            "tracking_number": "MCP-QA-DENIED",
                            "carrier": "UPS",
                            "category": "DELAYED_DELIVERY",
                            "description": "Must be rejected before downstream",
                            "source_channel": "backoffice",
                        }
                    },
                )
                assert denied.isError
                assert "forbidden" in denied.content[0].text


def test_real_keycloak_api_and_mcp_gate() -> None:
    # The Backoffice shell retains the existing local JWT session. Its runtime
    # password is hashed only into disposable TinyDB and is never printed.
    local_login = httpx.post(
        f"{env('QA_API_URL')}/auth/login",
        data={
            "username": "mcp-qa-user@example.invalid",
            "password": env("QA_TRACKFLOW_LOCAL_PASSWORD"),
        },
        timeout=10,
    )
    assert local_login.status_code == 200
    local_token = local_login.json()["access_token"]
    local_me = httpx.get(
        f"{env('QA_API_URL')}/auth/me",
        headers={"Authorization": f"Bearer {local_token}"},
        timeout=10,
    )
    assert local_me.status_code == 200
    assert local_me.json()["id"] == env("QA_TRACKFLOW_USER_ID")
    assert local_me.json()["role"] == "manager"
    local_support = httpx.post(
        f"{env('QA_API_URL')}/support/query",
        headers={"Authorization": f"Bearer {local_token}"},
        json={"question": "Explain the returns policy."},
        timeout=10,
    )
    assert local_support.status_code == 401

    full_token = password_token(SCOPES)
    claims = decode(full_token, "trackflow-mcp")
    assert claims["trackflow_user_id"] == env("QA_TRACKFLOW_USER_ID")
    assert int(claims["exp"]) > int(datetime.now(timezone.utc).timestamp())
    assert set(SCOPES).issubset(set(str(claims["scope"]).split()))

    prm = httpx.get(env("QA_PRM_URL"), timeout=10)
    assert prm.status_code == 200
    assert prm.json()["resource"] == env("QA_MCP_URL")
    assert set(prm.json()["scopes_supported"]) == set(SCOPES)
    assert httpx.get(env("QA_MCP_URL"), timeout=10).status_code == 401

    # The subject token is for the MCP resource and must fail at the API.
    subject_at_api = httpx.get(
        f"{env('QA_API_URL')}/inventory/products",
        headers={"Authorization": f"Bearer {full_token}"},
        timeout=10,
    )
    assert subject_at_api.status_code == 401

    downstream_token = exchange_token(full_token, SCOPES)
    assert downstream_token != full_token
    downstream_claims = decode(downstream_token, "trackflow-api")
    assert downstream_claims["trackflow_user_id"] == env("QA_TRACKFLOW_USER_ID")
    audience = downstream_claims["aud"]
    assert "trackflow-api" in ([audience] if isinstance(audience, str) else audience)

    headers = {"Authorization": f"Bearer {downstream_token}"}
    inventory_before = httpx.get(
        f"{env('QA_API_URL')}/inventory/products", headers=headers, timeout=10
    )
    assert inventory_before.status_code == 200
    assert [item["id"] for item in inventory_before.json()] == [VISIBLE_PRODUCT_ID]

    rejected_mutation = httpx.post(
        f"{env('QA_API_URL')}/inventory/products",
        headers=headers,
        json={
            "name": "Must Not Exist",
            "sku": "MCP-QA-MUTATION",
            "client_id": VISIBLE_CLIENT_ID,
            "client_name": "PureStep Footwear",
            "category": "fashion",
            "warehouse": "LA",
        },
        timeout=10,
    )
    assert rejected_mutation.status_code == 403

    names, incident_id = asyncio.run(call_real_tools(full_token))
    assert not any("create_inventory" in name or "update_inventory" in name for name in names)

    # The API support endpoint authenticates the MCP audience token and the
    # async LangGraph calls ticket/inventory only through langchain-mcp-adapters.
    support_token = pkce_support_token()
    support_claims = decode(support_token, "trackflow-mcp")
    assert support_claims["trackflow_user_id"] == env("QA_TRACKFLOW_USER_ID")
    assert support_claims["trackflow_user_id"] == local_me.json()["id"]
    assert {"incidents:read", "inventory:read"}.issubset(
        set(str(support_claims["scope"]).split())
    )
    oidc_at_local_me = httpx.get(
        f"{env('QA_API_URL')}/auth/me",
        headers={"Authorization": f"Bearer {support_token}"},
        timeout=10,
    )
    assert oidc_at_local_me.status_code == 401
    support = httpx.post(
        f"{env('QA_API_URL')}/support/query",
        headers={"Authorization": f"Bearer {support_token}"},
        json={
            "question": (
                f"Check ticket {incident_id} and product {VISIBLE_PRODUCT_ID} stock."
            )
        },
        timeout=20,
    )
    assert support.status_code == 200
    support_body = support.json()
    assert support_body["route"] == "mixed"
    assert support_body["partial"] is False
    assert support_body["ticket"]["incident_id"] == incident_id
    assert support_body["ticket"]["incident_status"] == "CLOSED"
    assert support_body["inventory"]["product_id"] == VISIBLE_PRODUCT_ID
    assert support_body["inventory"]["current_stock"] == 7

    hidden_support = httpx.post(
        f"{env('QA_API_URL')}/support/query",
        headers={"Authorization": f"Bearer {support_token}"},
        json={"question": f"Check product {HIDDEN_PRODUCT_ID} stock."},
        timeout=20,
    )
    assert hidden_support.status_code == 200
    assert hidden_support.json()["inventory"]["status"] == "not_found"

    limited = password_token(("incidents:read",))
    asyncio.run(call_with_insufficient_scope(limited))
    support_denied = httpx.post(
        f"{env('QA_API_URL')}/support/query",
        headers={"Authorization": f"Bearer {limited}"},
        json={"question": f"Check ticket {incident_id}."},
        timeout=10,
    )
    assert support_denied.status_code == 403

    inventory_after = httpx.get(
        f"{env('QA_API_URL')}/inventory/products", headers=headers, timeout=10
    )
    assert inventory_after.status_code == 200
    assert [item["id"] for item in inventory_after.json()] == [VISIBLE_PRODUCT_ID]
