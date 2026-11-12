from __future__ import annotations

import time
import logging
from typing import Any
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

import services.api.auth_security as local_auth
import services.api.operational_oidc as operational_oidc
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import create_access_token
from services.api.operational_oidc import (
    INCIDENTS_CREATE_SCOPE,
    INCIDENTS_READ_SCOPE,
    INCIDENTS_STATUS_SCOPE,
    INVENTORY_READ_SCOPE,
    OperationalOIDCSettings,
    get_operational_user,
)

ISSUER = "http://127.0.0.1:8080/realms/trackflow"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


def request(method: str, path: str, route_template: str | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "route": SimpleNamespace(path=route_template or path),
        }
    )


def user(*, active: bool = True, role: Role = Role.user) -> UserInDB:
    return UserInDB(
        id="trackflow-user-42",
        email="linked@example.com",
        hashed_password="stored-hash",
        is_active=active,
        role=role,
    )


@pytest.fixture
def rsa_material(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, Any]]:
    monkeypatch.setenv("TRACKFLOW_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("TRACKFLOW_OIDC_JWKS_URI", JWKS_URI)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "keycloak-qa", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "fetch_data",
        lambda _self: {"keys": [jwk]},
    )
    operational_oidc._jwk_client.cache_clear()
    return private_key, jwk


def external_token(
    private_key: Any,
    *,
    issuer: str = ISSUER,
    audience: str = "trackflow-api",
    expires_delta: int = 300,
    scopes: tuple[str, ...] = (INCIDENTS_READ_SCOPE,),
    user_id: str | None = "trackflow-user-42",
    subject: str = "keycloak-subject",
    extra: dict[str, object] | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": now,
        "exp": now + expires_delta,
        "scope": " ".join(scopes),
    }
    if user_id is not None:
        payload["trackflow_user_id"] = user_id
    if extra:
        payload.update(extra)
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": "keycloak-qa"},
    )


def assert_http_error(expected_status: int, call) -> HTTPException:
    with pytest.raises(HTTPException) as raised:
        call()
    assert raised.value.status_code == expected_status
    return raised.value


def test_local_hs256_token_keeps_existing_decoder_and_user_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_user = user(role=Role.manager)
    monkeypatch.setattr(local_auth, "_find_user_by_id", lambda user_id: stored_user)
    token = create_access_token(stored_user.id)

    authenticated = get_operational_user(
        request("GET", "/inventory/products"), token
    )

    assert authenticated is stored_user
    assert authenticated.role is Role.manager


@pytest.mark.parametrize(
    "method,path,route_template,scope",
    [
        ("POST", "/api/incidents", "/api/incidents", INCIDENTS_CREATE_SCOPE),
        ("GET", "/api/incidents/TRF-123456", "/api/incidents/{incident_id}", INCIDENTS_READ_SCOPE),
        ("PATCH", "/api/incidents/TRF-123456/status", "/api/incidents/{incident_id}/status", INCIDENTS_STATUS_SCOPE),
        ("GET", "/inventory/products", "/inventory/products", INVENTORY_READ_SCOPE),
        ("GET", "/inventory/products/17", "/inventory/products/{id}", INVENTORY_READ_SCOPE),
    ],
)
def test_external_token_accepts_only_approved_route_scope_pairs(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
    method: str,
    path: str,
    route_template: str,
    scope: str,
) -> None:
    private_key, _jwk = rsa_material
    stored_user = user(role=Role.manager)
    monkeypatch.setattr(operational_oidc, "_find_user_by_id", lambda user_id: stored_user)

    authenticated = get_operational_user(
        request(method, path, route_template), external_token(private_key, scopes=(scope,))
    )

    assert authenticated is stored_user


@pytest.mark.parametrize("failure", ["signature", "issuer", "audience", "expired"])
def test_external_token_rejects_invalid_signature_or_required_claims(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
    failure: str,
) -> None:
    private_key, _jwk = rsa_material
    signing_key = private_key
    kwargs: dict[str, object] = {"scopes": (INCIDENTS_READ_SCOPE,)}
    if failure == "signature":
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif failure == "issuer":
        kwargs["issuer"] = "http://127.0.0.1:8080/realms/wrong"
    elif failure == "audience":
        kwargs["audience"] = "trackflow-mcp"
    else:
        kwargs["expires_delta"] = -1
    monkeypatch.setattr(operational_oidc, "_find_user_by_id", lambda user_id: user())

    error = assert_http_error(
        401,
        lambda: get_operational_user(
            request("GET", "/api/incidents/TRF-123456", "/api/incidents/{incident_id}"),
            external_token(signing_key, **kwargs),
        ),
    )

    assert error.detail == "Could not validate credentials"


def test_external_token_requires_trackflow_mapping_claim(
    rsa_material: tuple[Any, dict[str, Any]],
) -> None:
    private_key, _jwk = rsa_material
    assert_http_error(
        401,
        lambda: get_operational_user(
            request("GET", "/api/incidents/TRF-123456", "/api/incidents/{incident_id}"),
            external_token(private_key, user_id=None),
        ),
    )


@pytest.mark.parametrize("stored_user", [None, user(active=False)])
def test_external_token_requires_real_active_trackflow_user(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
    stored_user: UserInDB | None,
) -> None:
    private_key, _jwk = rsa_material
    monkeypatch.setattr(
        operational_oidc, "_find_user_by_id", lambda user_id: stored_user
    )

    assert_http_error(
        401,
        lambda: get_operational_user(
            request("GET", "/api/incidents/TRF-123456", "/api/incidents/{incident_id}"),
            external_token(private_key),
        ),
    )


def test_external_token_scope_mismatch_is_403_before_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
) -> None:
    private_key, _jwk = rsa_material
    lookup_called = False

    def lookup(_user_id: str) -> UserInDB:
        nonlocal lookup_called
        lookup_called = True
        return user()

    monkeypatch.setattr(operational_oidc, "_find_user_by_id", lookup)

    assert_http_error(
        403,
        lambda: get_operational_user(
            request("PATCH", "/api/incidents/TRF-123456/status", "/api/incidents/{incident_id}/status"),
            external_token(private_key, scopes=(INCIDENTS_READ_SCOPE,)),
        ),
    )
    assert lookup_called is False


@pytest.mark.parametrize(
    "method,path,route_template",
    [
        ("PATCH", "/api/incidents/TRF-123456", "/api/incidents/{incident_id}"),
        ("GET", "/api/incidents/analyze", "/api/incidents/analyze"),
        ("POST", "/inventory/products", "/inventory/products"),
        ("PATCH", "/inventory/products/17/threshold", "/inventory/products/{id}/threshold"),
        ("POST", "/inventory/physical-counts", "/inventory/physical-counts"),
        ("POST", "/inventory/commercial-orders", "/inventory/commercial-orders"),
        (
            "POST",
            "/inventory/commercial-orders/00000000-0000-0000-0000-000000000001/dispatches",
            "/inventory/commercial-orders/{order_id}/dispatches",
        ),
        ("POST", "/inventory/orders/inbound", "/inventory/orders/inbound"),
        ("POST", "/inventory/orders/outbound", "/inventory/orders/outbound"),
        ("GET", "/inventory/orders", "/inventory/orders"),
    ],
)
def test_external_token_is_403_on_cross_route_and_inventory_mutations(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
    method: str,
    path: str,
    route_template: str,
) -> None:
    private_key, _jwk = rsa_material
    lookup_called = False

    def lookup(_user_id: str) -> UserInDB:
        nonlocal lookup_called
        lookup_called = True
        return user()

    monkeypatch.setattr(operational_oidc, "_find_user_by_id", lookup)

    assert_http_error(
        403,
        lambda: get_operational_user(
            request(method, path, route_template),
            external_token(
                private_key,
                scopes=(
                    INCIDENTS_CREATE_SCOPE,
                    INCIDENTS_READ_SCOPE,
                    INCIDENTS_STATUS_SCOPE,
                    INVENTORY_READ_SCOPE,
                ),
            ),
        ),
    )
    assert lookup_called is False


def test_external_role_and_tenant_claims_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: tuple[Any, dict[str, Any]],
) -> None:
    private_key, _jwk = rsa_material
    stored_user = user(role=Role.user)
    monkeypatch.setattr(operational_oidc, "_find_user_by_id", lambda user_id: stored_user)
    token = external_token(
        private_key,
        scopes=(INVENTORY_READ_SCOPE,),
        extra={
            "role": "admin",
            "roles": ["admin"],
            "client_id": "unauthorized-client",
            "allowed_client_ids": ["all-clients"],
        },
    )

    authenticated = get_operational_user(
        request("GET", "/inventory/products", "/inventory/products"), token
    )

    assert authenticated is stored_user
    assert authenticated.role is Role.user


def test_issuer_bearing_hs256_token_never_falls_back_to_local_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = jwt.encode(
        {
            "sub": "trackflow-user-42",
            "iss": ISSUER,
            "exp": int(time.time()) + 300,
        },
        "untrusted-secret-with-at-least-32-bytes",
        algorithm="HS256",
    )
    local_called = False

    def local_decoder(_token: str) -> UserInDB:
        nonlocal local_called
        local_called = True
        return user()

    monkeypatch.setattr(operational_oidc, "get_current_user", local_decoder)

    assert_http_error(
        401,
        lambda: get_operational_user(request("GET", "/inventory/products"), token),
    )
    assert local_called is False


def test_invalid_external_token_is_not_disclosed_in_logs_or_error(
    caplog: pytest.LogCaptureFixture,
    rsa_material: tuple[Any, dict[str, Any]],
) -> None:
    _private_key, _jwk = rsa_material
    raw_token = "eyJhbGciOiJSUzI1NiJ9.sensitive-payload.sensitive-signature"
    caplog.set_level(logging.DEBUG)

    error = assert_http_error(
        401,
        lambda: get_operational_user(
            request("GET", "/inventory/products", "/inventory/products"), raw_token
        ),
    )

    assert raw_token not in str(error.detail)
    assert raw_token not in caplog.text


def test_oidc_configuration_rejects_wrong_audience_and_unsafe_urls() -> None:
    with pytest.raises(ValueError, match="audience"):
        OperationalOIDCSettings(ISSUER, JWKS_URI, audience="trackflow-mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        OperationalOIDCSettings("ftp://127.0.0.1/realm", JWKS_URI)
    with pytest.raises(ValueError, match="HTTPS"):
        OperationalOIDCSettings("http://idp.example/realm", "https://idp.example/certs")
