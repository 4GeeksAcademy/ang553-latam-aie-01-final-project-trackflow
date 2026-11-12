"""Isolated OIDC authentication for MCP-approved operational API routes.

The existing local HS256 JWT flow remains authoritative for browser and
first-party callers. RS256 tokens are handled exclusively by this dependency
and never fall back to the local decoder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from services.api.auth_models import UserInDB
from services.api.auth_security import _find_user_by_id, get_current_user

OIDC_AUDIENCE = "trackflow-api"
OIDC_ALGORITHM = "RS256"

INCIDENTS_CREATE_SCOPE = "incidents:create"
INCIDENTS_READ_SCOPE = "incidents:read"
INCIDENTS_STATUS_SCOPE = "incidents:status"
INVENTORY_READ_SCOPE = "inventory:read"

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="External token is not authorized for this operation",
    )


def _validate_https_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{name} must use HTTPS outside loopback QA")
    return value.rstrip("/")


@dataclass(frozen=True)
class OperationalOIDCSettings:
    issuer: str
    jwks_uri: str
    audience: str = OIDC_AUDIENCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _validate_https_url("issuer", self.issuer))
        object.__setattr__(self, "jwks_uri", _validate_https_url("jwks_uri", self.jwks_uri))
        if self.audience != OIDC_AUDIENCE:
            raise ValueError("OIDC audience must remain trackflow-api")

    @classmethod
    def from_env(cls) -> "OperationalOIDCSettings":
        return cls(
            issuer=os.environ["TRACKFLOW_OIDC_ISSUER"],
            jwks_uri=os.environ["TRACKFLOW_OIDC_JWKS_URI"],
        )


@lru_cache(maxsize=8)
def _jwk_client(jwks_uri: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_uri)


_ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/incidents"): INCIDENTS_CREATE_SCOPE,
    ("GET", "/api/incidents/{incident_id}"): INCIDENTS_READ_SCOPE,
    ("PATCH", "/api/incidents/{incident_id}/status"): INCIDENTS_STATUS_SCOPE,
    ("GET", "/inventory/products"): INVENTORY_READ_SCOPE,
    ("GET", "/inventory/products/{id}"): INVENTORY_READ_SCOPE,
}


def _required_scope(request: Request) -> str | None:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if not isinstance(route_path, str):
        return None
    return _ROUTE_SCOPES.get((request.method.upper(), route_path))


def _unverified_header(token: str) -> dict[str, object]:
    try:
        return jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return {}


def _has_external_issuer_claim(token: str) -> bool:
    try:
        payload = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
            algorithms=[OIDC_ALGORITHM],
        )
    except jwt.PyJWTError:
        return False
    return "iss" in payload


def _decode_external_token(
    token: str, settings: OperationalOIDCSettings
) -> dict[str, object]:
    try:
        signing_key = _jwk_client(settings.jwks_uri).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[OIDC_ALGORITHM],
            audience=settings.audience,
            issuer=settings.issuer,
            options={"require": ["exp", "sub", "trackflow_user_id"]},
        )
    except jwt.PyJWTError:
        raise _credentials_exception() from None


def _external_user(request: Request, token: str) -> UserInDB:
    try:
        settings = OperationalOIDCSettings.from_env()
    except (KeyError, ValueError):
        raise _credentials_exception() from None

    claims = _decode_external_token(token, settings)
    subject = claims.get("sub")
    trackflow_user_id = claims.get("trackflow_user_id")
    if not isinstance(subject, str) or not subject.strip():
        raise _credentials_exception()
    if not isinstance(trackflow_user_id, str) or not trackflow_user_id.strip():
        raise _credentials_exception()

    required_scope = _required_scope(request)
    if required_scope is None:
        raise _forbidden_exception()

    scope_claim = claims.get("scope")
    if not isinstance(scope_claim, str) or required_scope not in scope_claim.split():
        raise _forbidden_exception()

    user = _find_user_by_id(trackflow_user_id)
    if user is None or not user.is_active:
        raise _credentials_exception()
    return user


def get_operational_user(
    request: Request,
    token: Annotated[str, Depends(_oauth2_scheme)],
) -> UserInDB:
    """Authenticate local tokens unchanged, or strict Keycloak RS256 tokens."""

    header = _unverified_header(token)
    algorithm = header.get("alg")
    if algorithm == OIDC_ALGORITHM:
        return _external_user(request, token)
    if _has_external_issuer_claim(token):
        # An issuer-bearing token is never eligible for local HS256 fallback.
        raise _credentials_exception()
    return get_current_user(token)
