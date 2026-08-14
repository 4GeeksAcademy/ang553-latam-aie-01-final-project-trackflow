"""
TrackFlow Auth security — password hashing and JWT utilities.

Uses bcrypt via passlib for password operations and python-jose for JWT.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.hash import bcrypt

from services.api.auth_database import users as _users_db
from services.api.auth_models import UserInDB
from services.api.auth_settings import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET_KEY

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
        "exp": now + expires_delta,
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# ── OAuth2 scheme & dependency ───────────────────────────────────────────────

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_UserQuery = None  # lazy import handled inline


def _find_user_by_id(user_id: str) -> UserInDB | None:
    """Look up a user by their TinyDB id string."""
    from tinydb import Query as _TinyQuery

    q = _TinyQuery()
    results = _users_db.search(q.id == user_id)
    if not results:
        return None
    return UserInDB(**results[0])


def get_current_user(token: Annotated[str, Depends(_oauth2_scheme)]) -> UserInDB:
    """Dependency — extract and validate the JWT, return the authenticated user.

    Raises ``401 Unauthorized`` if:
    - no token provided
    - token is invalid (bad signature, malformed)
    - token is expired
    - ``sub`` claim is missing
    - ``sub`` does not match an existing user
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        sub: str | None = payload.get("sub")
        if sub is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = _find_user_by_id(sub)
    if user is None:
        raise credentials_exception

    return user