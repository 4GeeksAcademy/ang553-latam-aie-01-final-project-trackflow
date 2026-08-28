"""Functional unit tests for AUTH-088 login flow (POST /auth/login)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt

from services.api.auth_models import UserCreate, UserUpdate
from services.api.auth_security import get_current_user
from services.api.auth_services import create_user, update_user
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY
from services.api.routes import auth as auth_route


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def _form(email: str, password: str) -> OAuth2PasswordRequestForm:
    return OAuth2PasswordRequestForm(username=email, password=password, scope="")


def test_login_with_valid_active_credentials_returns_decodable_access_token() -> None:
    """AUTH-LOGIN-HP-01: active user + correct password produces access token."""
    created = create_user(
        UserCreate(email="active.user@example.com", password="StrongPass123")
    )

    result = _run(auth_route.login(_form("active.user@example.com", "StrongPass123")))

    assert result["token_type"] == "bearer"
    assert isinstance(result["access_token"], str)
    assert result["access_token"]

    payload = jwt.decode(
        result["access_token"],
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
    )
    assert payload["sub"] == created.id
    assert "exp" in payload
    assert "iat" in payload

    current_user = get_current_user(result["access_token"])
    assert current_user.id == created.id


def test_login_normalizes_email_with_spaces_and_case_differences() -> None:
    """AUTH-LOGIN-EDGE-01: login lookup normalizes email."""
    created = create_user(
        UserCreate(email="case.user@example.com", password="StrongPass123")
    )

    result = _run(auth_route.login(_form("  CASE.USER@EXAMPLE.COM  ", "StrongPass123")))

    current_user = get_current_user(result["access_token"])
    assert current_user.id == created.id


def test_login_rejects_incorrect_password_with_generic_credentials_error() -> None:
    """AUTH-LOGIN-FAIL-01: wrong password is rejected."""
    create_user(UserCreate(email="wrong.password@example.com", password="StrongPass123"))

    with pytest.raises(HTTPException) as exc_info:
        _run(auth_route.login(_form("wrong.password@example.com", "BadPass999")))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"


def test_login_rejects_non_existent_user_with_generic_credentials_error() -> None:
    """AUTH-LOGIN-FAIL-02: unknown user is rejected generically."""
    with pytest.raises(HTTPException) as exc_info:
        _run(auth_route.login(_form("not.found@example.com", "StrongPass123")))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"


def test_login_rejects_inactive_user_even_with_correct_password() -> None:
    """AUTH-LOGIN-FAIL-03: inactive users cannot log in."""
    created = create_user(UserCreate(email="inactive@example.com", password="StrongPass123"))
    update_user(created.id, UserUpdate(is_active=False))

    with pytest.raises(HTTPException) as exc_info:
        _run(auth_route.login(_form("inactive@example.com", "StrongPass123")))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Account is inactive"
