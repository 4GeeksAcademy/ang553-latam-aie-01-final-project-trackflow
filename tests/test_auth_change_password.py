"""Functional unit tests for AUTH-088 change-password flow (POST /auth/change-password)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from tinydb import Query

from services.api import auth_services
from services.api.auth_models import ChangePasswordRequest, UserCreate
from services.api.auth_security import verify_password
from services.api.auth_services import change_password, create_user
from services.api.routes import auth as auth_route


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def _get_user_doc(user_id: str) -> dict:
    q = Query()
    matches = auth_services.users.search(q.id == user_id)
    assert len(matches) == 1
    return matches[0]


def test_change_password_changes_hash_and_preserves_user_identity_fields() -> None:
    """AUTH-CHPASS-HP-01: correct current password updates only password state."""
    created = create_user(UserCreate(email="chpass.hp@example.com", password="OldPass123"))
    before_user = _get_user_doc(created.id)
    before_hash = before_user["hashed_password"]

    change_password(
        user_id=created.id,
        current_password="OldPass123",
        new_password="NewPass456",
    )

    after_user = _get_user_doc(created.id)
    assert after_user["id"] == before_user["id"]
    assert after_user["email"] == before_user["email"]
    assert after_user["role"] == before_user["role"]
    assert after_user["is_active"] == before_user["is_active"]
    assert after_user["created_at"] == before_user["created_at"]

    assert after_user["hashed_password"] != before_hash
    assert verify_password("NewPass456", after_user["hashed_password"]) is True
    assert verify_password("OldPass123", after_user["hashed_password"]) is False


def test_change_password_rejects_empty_current_password_without_changing_hash() -> None:
    """AUTH-CHPASS-EDGE-01: empty current_password reaches domain logic and is rejected."""
    created = create_user(
        UserCreate(email="chpass.edge.empty@example.com", password="EdgePass123")
    )
    before_user = _get_user_doc(created.id)
    before_hash = before_user["hashed_password"]

    with pytest.raises(ValueError, match="Current password is incorrect\\."):
        change_password(
            user_id=created.id,
            current_password="",
            new_password="ShouldNotApply456",
        )

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] == before_hash
    assert verify_password("EdgePass123", after_user["hashed_password"]) is True
    assert verify_password("ShouldNotApply456", after_user["hashed_password"]) is False


def test_change_password_rejects_incorrect_current_password_without_state_changes() -> None:
    """AUTH-CHPASS-FAIL-01: incorrect non-empty current password is rejected."""
    created = create_user(
        UserCreate(email="chpass.fail.incorrect@example.com", password="FailPass123")
    )
    before_user = _get_user_doc(created.id)
    before_hash = before_user["hashed_password"]

    with pytest.raises(ValueError, match="Current password is incorrect\\."):
        change_password(
            user_id=created.id,
            current_password="WrongCurrent999",
            new_password="ShouldNotApply789",
        )

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] == before_hash
    assert verify_password("FailPass123", after_user["hashed_password"]) is True
    assert verify_password("ShouldNotApply789", after_user["hashed_password"]) is False


def test_change_password_rejects_when_user_does_not_exist() -> None:
    """AUTH-CHPASS-OPT-01: missing user id follows service-defined rejection behavior."""
    with pytest.raises(ValueError, match="User not found"):
        change_password(
            user_id="missing-user-id",
            current_password="AnyCurrent123",
            new_password="AnyNew12345",
        )


def test_change_password_endpoint_maps_domain_valueerror_to_http_400() -> None:
    """Router mapping: service ValueError is translated to endpoint HTTP 400."""
    created = create_user(
        UserCreate(email="chpass.router@example.com", password="RouterPass123")
    )
    current_user = auth_services.get_user_in_db_by_email(created.email)
    assert current_user is not None

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.change_password_endpoint(
                payload=ChangePasswordRequest(
                    current_password="WrongCurrentForRouter",
                    new_password="RouterNewPass456",
                ),
                current_user=current_user,
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Current password is incorrect."

    user_after = _get_user_doc(created.id)
    assert verify_password("RouterPass123", user_after["hashed_password"]) is True
    assert verify_password("RouterNewPass456", user_after["hashed_password"]) is False