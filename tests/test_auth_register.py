"""Functional unit tests for AUTH-088 register flow (POST /users)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from tinydb import Query

from services.api.auth_models import UserRegister
from services.api import auth_services
from services.api.auth_security import verify_password
from services.api.routes import users as users_route


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def test_register_user_creates_standard_active_user() -> None:
    """AUTH-USERS-HP-01: valid signup creates active standard user."""
    payload = UserRegister(
        email="  New.User@Example.com  ",
        password="StrongPass123",
    )

    created = _run(users_route.register_user(payload))

    assert created.email == "new.user@example.com"
    assert created.role.value == "user"
    assert created.is_active is True

    q = Query()
    stored = auth_services.users.search(q.id == created.id)
    assert len(stored) == 1
    stored_doc = stored[0]

    assert "password" not in stored_doc
    assert stored_doc["hashed_password"] != "StrongPass123"
    assert verify_password("StrongPass123", stored_doc["hashed_password"]) is True


def test_register_user_with_partial_optional_profile_data() -> None:
    """AUTH-USERS-EDGE-01: registration supports partial optional profile."""
    payload = UserRegister(
        email="partial.profile@example.com",
        password="StrongPass123",
        name="Partial Name",
    )

    created = _run(users_route.register_user(payload))

    q = Query()
    users_found = auth_services.users.search(q.id == created.id)
    assert len(users_found) == 1

    profiles_found = auth_services.profiles.search(q.user_id == created.id)
    assert len(profiles_found) == 1
    profile_doc = profiles_found[0]
    assert profile_doc["name"] == "Partial Name"
    assert profile_doc["phone"] is None
    assert profile_doc["address"] is None


def test_register_user_rejects_duplicate_email_and_preserves_original() -> None:
    """AUTH-USERS-FAIL-01: duplicate email is rejected."""
    first_payload = UserRegister(
        email="duplicate@example.com",
        password="StrongPass123",
    )
    first_created = _run(users_route.register_user(first_payload))

    duplicate_payload = UserRegister(
        email="  DUPLICATE@EXAMPLE.COM  ",
        password="AnotherPass123",
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(users_route.register_user(duplicate_payload))

    assert exc_info.value.status_code == 409

    q = Query()
    duplicate_records = auth_services.users.search(q.email == "duplicate@example.com")
    assert len(duplicate_records) == 1
    assert duplicate_records[0]["id"] == first_created.id


def test_register_user_rolls_back_when_profile_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-USERS-FAIL-02: user creation is rolled back if profile fails."""

    def failing_create_profile(_payload):
        raise ValueError("profile creation failed")

    monkeypatch.setattr(users_route, "create_profile", failing_create_profile)

    payload = UserRegister(
        email="rollback@example.com",
        password="StrongPass123",
        name="Needs Profile",
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(users_route.register_user(payload))

    assert exc_info.value.status_code == 400
    assert "profile creation failed" in str(exc_info.value.detail)

    q = Query()
    leftovers = auth_services.users.search(q.email == "rollback@example.com")
    assert leftovers == []
