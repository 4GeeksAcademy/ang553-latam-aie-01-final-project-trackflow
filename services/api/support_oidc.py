"""Exclusive Keycloak identity boundary for ``POST /support/query``.

The endpoint deliberately does not accept TrackFlow's legacy HS256 access
token.  Its bearer is issued for the MCP resource and is forwarded only to
that resource; roles and tenant memberships remain authoritative in TrackFlow.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from pydantic import SecretStr

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import _find_user_by_id
from services.api.operational_oidc import _validate_https_url


SUPPORT_AUDIENCE = "trackflow-mcp"
SUPPORT_ALGORITHM = "RS256"
SUPPORT_SCOPES = frozenset({"incidents:read", "inventory:read"})
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class SupportOIDCSettings:
    issuer: str
    jwks_uri: str
    audience: str = SUPPORT_AUDIENCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _validate_https_url("issuer", self.issuer))
        object.__setattr__(
            self, "jwks_uri", _validate_https_url("jwks_uri", self.jwks_uri)
        )
        if self.audience != SUPPORT_AUDIENCE:
            raise ValueError("support audience must remain trackflow-mcp")

    @classmethod
    def from_env(cls) -> "SupportOIDCSettings":
        return cls(
            issuer=os.environ["TRACKFLOW_OIDC_ISSUER"],
            jwks_uri=os.environ["TRACKFLOW_OIDC_JWKS_URI"],
        )


@dataclass(frozen=True)
class SupportIdentity:
    user: UserInDB
    subject: str
    scopes: frozenset[str]
    _token: SecretStr

    def bearer_for_mcp(self) -> SecretStr:
        """Return the bearer only to the narrow MCP client factory."""

        return self._token


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str = "support access is not authorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@lru_cache(maxsize=8)
def _jwk_client(uri: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(uri)


def authenticate_support_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> SupportIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    token = credentials.credentials
    try:
        settings = SupportOIDCSettings.from_env()
        header = jwt.get_unverified_header(token)
        if header.get("alg") != SUPPORT_ALGORITHM:
            raise _unauthorized()
        signing_key = _jwk_client(settings.jwks_uri).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[SUPPORT_ALGORITHM],
            issuer=settings.issuer,
            audience=settings.audience,
            options={"require": ["exp", "sub", "trackflow_user_id", "scope"]},
        )
    except (KeyError, ValueError, jwt.PyJWTError):
        raise _unauthorized() from None

    subject = claims.get("sub")
    trackflow_user_id = claims.get("trackflow_user_id")
    scope_claim = claims.get("scope")
    if not isinstance(subject, str) or not subject.strip():
        raise _unauthorized()
    if not isinstance(trackflow_user_id, str) or not trackflow_user_id.strip():
        raise _unauthorized()
    if not isinstance(scope_claim, str):
        raise _unauthorized()
    scopes = frozenset(scope_claim.split())
    if not SUPPORT_SCOPES.issubset(scopes):
        raise _forbidden("required support scopes are missing")

    user = _find_user_by_id(trackflow_user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return SupportIdentity(user, subject, scopes, SecretStr(token))


def require_support_reader(
    identity: Annotated[SupportIdentity, Depends(authenticate_support_identity)],
) -> SupportIdentity:
    if identity.user.role not in {Role.admin, Role.manager}:
        raise _forbidden("admin or manager role required")
    return identity
