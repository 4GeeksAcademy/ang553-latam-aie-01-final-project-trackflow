"""Functional unit tests for AUTH-088 access token and /auth/me flow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

from services.api.auth_models import UserCreate, UserUpdate
from services.api.auth_security import create_access_token, get_current_user
from services.api.auth_services import create_user, delete_user, get_user_in_db_by_email, update_user
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY
from services.api.routes import auth as auth_route


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def test_auth_me_with_valid_access_token_returns_expected_identity() -> None:
    """AUTH-ME-HP-01: valid access token resolves active user identity."""
    created = create_user(UserCreate(email="me.active@example.com", password="StrongPass123"))

    access_token = create_access_token(sub=created.id)
    payload = jwt.decode(access_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    header = jwt.get_unverified_header(access_token)

    assert header["alg"] == JWT_ALGORITHM
    assert payload["sub"] == created.id
    assert "exp" in payload
    assert "iat" in payload
    assert payload["exp"] > payload["iat"]

    current_user = get_current_user(access_token)
    assert current_user.id == created.id

    me = _run(auth_route.read_users_me(current_user=current_user))
    assert me.id == created.id
    assert me.email == "me.active@example.com"
    assert me.is_active is True


def test_auth_me_rejects_valid_token_if_user_was_deleted_after_issuance() -> None:
    """AUTH-ME-EDGE-01: token is valid but subject user no longer exists."""
    created = create_user(UserCreate(email="me.deleted@example.com", password="StrongPass123"))
    access_token = create_access_token(sub=created.id)

    payload = jwt.decode(access_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == created.id

    delete_user(created.id)

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(access_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_auth_me_rejects_expired_access_token_for_existing_active_user() -> None:
    """AUTH-ME-FAIL-01: expired token is rejected even for active existing user."""
    created = create_user(UserCreate(email="me.expired@example.com", password="StrongPass123"))

    persisted_user = get_user_in_db_by_email("me.expired@example.com")
    assert persisted_user is not None
    assert persisted_user.id == created.id
    assert persisted_user.is_active is True

    expired_token = create_access_token(sub=created.id, expires_delta=timedelta(minutes=-1))

    unverified_exp_payload = jwt.decode(
        expired_token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        options={"verify_exp": False},
    )
    assert unverified_exp_payload["sub"] == created.id

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(expired_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_auth_me_rejects_token_with_invalid_signature() -> None:
    """AUTH-ME-FAIL-02: token signed with a different secret is rejected."""
    created = create_user(UserCreate(email="me.bad-signature@example.com", password="StrongPass123"))

    now = datetime.now(timezone.utc)
    invalid_signature_token = jwt.encode(
        {
            "sub": created.id,
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        "different-test-secret",
        algorithm=JWT_ALGORITHM,
    )

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(invalid_signature_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_auth_me_rejects_user_deactivated_after_token_issuance() -> None:
    """AUTH-ME-FAIL-03: token remains valid but inactive user must be rejected."""
    created = create_user(UserCreate(email="me.inactive@example.com", password="StrongPass123"))
    access_token = create_access_token(sub=created.id)

    payload = jwt.decode(access_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == created.id

    update_user(created.id, UserUpdate(is_active=False))

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(access_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"