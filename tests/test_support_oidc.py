from __future__ import annotations

import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import jwt
import pytest

import services.api.support_oidc as support_oidc
from services.api.auth_models import Role, UserInDB
from services.api.support_oidc import (
    SUPPORT_SCOPES,
    SupportOIDCSettings,
    authenticate_support_identity,
    require_support_reader,
)


ISSUER = "http://127.0.0.1:18082/realms/trackflow-qa"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


def user(*, role: Role = Role.manager, active: bool = True) -> UserInDB:
    return UserInDB(
        id="trackflow-user-42",
        email="linked@example.invalid",
        hashed_password="not-used-for-oidc",
        role=role,
        is_active=active,
    )


@pytest.fixture
def rsa_material(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TRACKFLOW_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("TRACKFLOW_OIDC_JWKS_URI", JWKS_URI)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(), as_dict=True
    )
    public_jwk.update({"kid": "support-key", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(
        jwt.PyJWKClient, "fetch_data", lambda _self: {"keys": [public_jwk]}
    )
    support_oidc._jwk_client.cache_clear()
    return private_key


def token(
    key: Any,
    *,
    audience: str = "trackflow-mcp",
    scopes: frozenset[str] = SUPPORT_SCOPES,
    user_id: str = "trackflow-user-42",
    expires: int = 300,
    extra: dict[str, object] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": audience,
        "sub": "keycloak-subject",
        "trackflow_user_id": user_id,
        "scope": " ".join(sorted(scopes)),
        "iat": now,
        "exp": now + expires,
    }
    if extra:
        claims.update(extra)
    return jwt.encode(
        claims, key, algorithm="RS256", headers={"kid": "support-key"}
    )


def credentials(raw: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)


def error_status(expected: int, call) -> HTTPException:
    with pytest.raises(HTTPException) as raised:
        call()
    assert raised.value.status_code == expected
    return raised.value


def test_support_requires_mcp_audience_both_scopes_and_active_link(
    monkeypatch: pytest.MonkeyPatch, rsa_material: Any
) -> None:
    stored = user()
    monkeypatch.setattr(support_oidc, "_find_user_by_id", lambda value: stored)
    raw = token(rsa_material)

    identity = authenticate_support_identity(credentials(raw))

    assert identity.user is stored
    assert identity.subject == "keycloak-subject"
    assert identity.scopes.issuperset(SUPPORT_SCOPES)
    assert raw not in repr(identity)
    assert require_support_reader(identity) is identity


@pytest.mark.parametrize(
    "audience,scopes,status_code",
    [
        ("trackflow-api", SUPPORT_SCOPES, 401),
        ("trackflow-mcp", frozenset({"incidents:read"}), 403),
        ("trackflow-mcp", frozenset({"inventory:read"}), 403),
    ],
)
def test_wrong_audience_or_missing_scope_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    rsa_material: Any,
    audience: str,
    scopes: frozenset[str],
    status_code: int,
) -> None:
    lookup_called = False

    def lookup(_value: str) -> UserInDB:
        nonlocal lookup_called
        lookup_called = True
        return user()

    monkeypatch.setattr(support_oidc, "_find_user_by_id", lookup)
    error_status(
        status_code,
        lambda: authenticate_support_identity(
            credentials(token(rsa_material, audience=audience, scopes=scopes))
        ),
    )
    assert lookup_called is False


@pytest.mark.parametrize("stored", [None, user(active=False)])
def test_missing_or_inactive_link_is_401(
    monkeypatch: pytest.MonkeyPatch, rsa_material: Any, stored: UserInDB | None
) -> None:
    monkeypatch.setattr(support_oidc, "_find_user_by_id", lambda value: stored)
    error_status(
        401,
        lambda: authenticate_support_identity(credentials(token(rsa_material))),
    )


def test_token_role_and_tenant_claims_never_override_trackflow_role(
    monkeypatch: pytest.MonkeyPatch, rsa_material: Any
) -> None:
    stored = user(role=Role.user)
    monkeypatch.setattr(support_oidc, "_find_user_by_id", lambda value: stored)
    identity = authenticate_support_identity(
        credentials(
            token(
                rsa_material,
                extra={
                    "role": "admin",
                    "roles": ["admin"],
                    "allowed_client_ids": ["all"],
                },
            )
        )
    )

    assert identity.user.role is Role.user
    error_status(403, lambda: require_support_reader(identity))


def test_local_hs256_token_has_no_support_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKFLOW_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("TRACKFLOW_OIDC_JWKS_URI", JWKS_URI)
    raw = jwt.encode(
        {"sub": "trackflow-user-42", "exp": int(time.time()) + 300},
        "local-secret-with-more-than-thirty-two-bytes",
        algorithm="HS256",
    )
    error_status(401, lambda: authenticate_support_identity(credentials(raw)))


def test_missing_bearer_and_expired_rs256_are_401(
    monkeypatch: pytest.MonkeyPatch, rsa_material: Any
) -> None:
    monkeypatch.setattr(support_oidc, "_find_user_by_id", lambda value: user())
    error_status(401, lambda: authenticate_support_identity(None))
    error_status(
        401,
        lambda: authenticate_support_identity(
            credentials(token(rsa_material, expires=-1))
        ),
    )


def test_support_oidc_configuration_rejects_unsafe_urls() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        SupportOIDCSettings("http://idp.example/realm", JWKS_URI)
    with pytest.raises(ValueError, match="HTTPS"):
        SupportOIDCSettings(ISSUER, "https://user:secret@idp.example/certs")
    with pytest.raises(ValueError, match="audience"):
        SupportOIDCSettings(ISSUER, JWKS_URI, audience="trackflow-api")
