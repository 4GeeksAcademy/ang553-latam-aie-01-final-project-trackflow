"""Verified authentication adapter for RFP approval HTTP operations.

The public request never supplies issuer, subject, environment, principal or
binding identifiers.  Local JWT and OIDC credentials are verified first and
only then converted into the sealed approval authentication context.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from services.api.auth_models import UserInDB
from services.api.auth_security import get_current_user
from services.api.operational_oidc import (
    OIDC_ALGORITHM,
    OperationalOIDCSettings,
    _decode_external_token,
    _has_external_issuer_claim,
    _unverified_header,
)
from services.rfp_approval.identity import VerifiedAuthenticationContext


_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@dataclass(frozen=True, slots=True)
class RFPApprovalAuthentication:
    user: UserInDB
    context: VerifiedAuthenticationContext


def _environment() -> str:
    raw = os.getenv("TRACKFLOW_ENVIRONMENT")
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RFP approval authentication is not configured",
        )
    value = raw.strip().lower()
    if value not in {"production", "qa"}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RFP approval authentication is not configured",
        )
    return value


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _verified_local(token: str) -> RFPApprovalAuthentication:
    user = get_current_user(token)
    return RFPApprovalAuthentication(user=user, context=verified_local_context(user))


def verified_local_context(user: UserInDB) -> VerifiedAuthenticationContext:
    """Build the local binding context for an already JWT-verified user."""
    environment = _environment()
    issuer = (
        "trackflow-local"
        if environment == "production"
        else "urn:trackflow:qa:local"
    )
    return VerifiedAuthenticationContext._from_verified_authenticator(
        environment=environment,
        auth_source="local",
        issuer=issuer,
        subject=user.id,
        trackflow_user_id=user.id,
        general_role=user.role.value,
    )


def _verified_oidc(token: str) -> RFPApprovalAuthentication:
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
    # Reuse the authenticated local user projection only as the general-role
    # source. Department authority remains entirely PostgreSQL-backed.
    from services.api.auth_security import _find_user_by_id

    user = _find_user_by_id(trackflow_user_id)
    if user is None or not user.is_active:
        raise _credentials_exception()
    context = VerifiedAuthenticationContext._from_verified_authenticator(
        environment=_environment(),
        auth_source="oidc",
        issuer=settings.issuer,
        subject=subject,
        trackflow_user_id=trackflow_user_id,
        general_role=user.role.value,
    )
    return RFPApprovalAuthentication(user=user, context=context)


def get_rfp_approval_authentication(
    _request: Request,
    token: Annotated[str, Depends(_oauth2_scheme)],
) -> RFPApprovalAuthentication:
    """Return a sealed approval context from already verified credentials."""

    algorithm = _unverified_header(token).get("alg")
    if algorithm == OIDC_ALGORITHM:
        return _verified_oidc(token)
    if _has_external_issuer_claim(token):
        raise _credentials_exception()
    return _verified_local(token)
