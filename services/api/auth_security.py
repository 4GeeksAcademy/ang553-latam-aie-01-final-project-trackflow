"""
TrackFlow Auth security — password hashing and JWT utilities.

Uses bcrypt via passlib for password operations and python-jose for JWT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.hash import bcrypt

from services.api.auth_database import auth_storage_access, users as _users_db
from services.api.auth_models import UserInDB
from services.api.auth_settings import (
    JWT_ALGORITHM,
    JWT_EXPIRE_MINUTES,
    JWT_SECRET_KEY,
    RESET_TOKEN_EXPIRE_MINUTES,
)

# ── Password utilities ───────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt.

    Args:
        password: The plain-text password to hash.

    Returns:
        The bcrypt-hashed password string.
    """
    return bcrypt.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a plain-text password against a bcrypt hash.

    Args:
        password: The plain-text password to check.
        hashed_password: The bcrypt hash to verify against.

    Returns:
        True if the password matches the hash, False otherwise.
    """
    return bcrypt.verify(password, hashed_password)


# ── JWT utilities ────────────────────────────────────────────────────────────


def create_access_token(sub: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token.

    Args:
        sub: The subject claim — must be the user's UUID (TinyDB id).
        expires_delta: Optional custom expiration delta.  Defaults to
            ``JWT_EXPIRE_MINUTES`` from settings.

    Returns:
        The encoded JWT string.
    """
    now = datetime.now(timezone.utc)
    if expires_delta is None:
        expires_delta = timedelta(minutes=JWT_EXPIRE_MINUTES)

    payload = {
        "sub": sub,
        "type": "access",
        "exp": now + expires_delta,
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# ── Password-reset token utilities ───────────────────────────────────────────
#
# The reset JWT contains:
#   - sub: user id
#   - jti: unique token id (used for single-use enforcement via TinyDB)
#   - type: "password_reset" (differentiates from access tokens)
#   - exp: expiration (default: 30 minutes)
#   - iat: issued at


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_password_reset_token(sub: str) -> str:
    """Create a signed JWT specifically for password reset.

    The token carries a ``jti`` (unique ID) that is persisted in TinyDB so
    it can be invalidated after use — a JWT with ``exp`` alone is not
    sufficient because the bootcamp requires single-use semantics.

    Args:
        sub: The user's TinyDB id.

    Returns:
        The encoded password-reset JWT string.
    """
    jti = str(uuid.uuid4())
    expires_delta = timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES)
    now = _now_utc()

    payload = {
        "sub": sub,
        "jti": jti,
        "type": "password_reset",
        "exp": now + expires_delta,
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_password_reset_token(token: str) -> dict[str, str]:
    """Decode and validate a password-reset JWT.

    Verifies signature, expiration, and purpose (``type`` claim).

    Args:
        token: The raw JWT string.

    Returns:
        The decoded payload dict with at least ``sub``, ``jti``, and ``type``.

    Raises:
        JWTError: If the token is invalid, expired, or not a password-reset token.
    """
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    if payload.get("type") != "password_reset":
        raise JWTError("Token is not a password-reset token")
    if "jti" not in payload:
        raise JWTError("Token is missing required jti claim")
    return payload


# ── OAuth2 scheme & dependency ───────────────────────────────────────────────

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@auth_storage_access()
def _find_user_by_id(user_id: str) -> UserInDB | None:
    """Look up a user by their TinyDB id string."""
    # Avoid TinyDB's query cache when checking current identity and role.
    for row in _users_db.all():
        if row.get("id") == user_id:
            return UserInDB(**row)
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

def _decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise _unauthorized()
        token_type = payload.get("type")
        if token_type not in (None, "access"):
            raise _unauthorized()
    except (JWTError, TypeError, ValueError):
        raise _unauthorized() from None
    return payload


def get_current_user(token: Annotated[str, Depends(_oauth2_scheme)]) -> UserInDB:
    """Validate the access JWT and resolve the existing active user."""
    user = _find_user_by_id(_decode_access_token(token)["sub"])
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


@dataclass(frozen=True)
class ExpiringAccessSession:
    user_id: str
    expires_at: int

    def current_user(self) -> UserInDB:
        if datetime.now(timezone.utc).timestamp() >= self.expires_at:
            raise _unauthorized()
        user = _find_user_by_id(self.user_id)
        if user is None or not user.is_active:
            raise _unauthorized()
        return user


def get_expiring_access_session(
    token: Annotated[str, Depends(_oauth2_scheme)],
) -> ExpiringAccessSession:
    """Verify a JWT whose explicit expiration bounds a long-lived response."""
    payload = _decode_access_token(token)
    expiry = payload.get("exp")
    if type(expiry) is not int:
        raise _unauthorized()
    return ExpiringAccessSession(user_id=payload["sub"], expires_at=expiry)
