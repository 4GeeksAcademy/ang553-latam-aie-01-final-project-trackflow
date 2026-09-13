"""Functional tests for the authenticated profile projection routes."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from services.api import auth_services
from services.api.auth_models import (
    ProfileCreate,
    ProfileMeResponse,
    ProfileUpdate,
    UserCreate,
    UserInDB,
)
from services.api.auth_services import create_profile, create_user
from services.api.routes import profiles as profiles_route


def _run(coro):
    return asyncio.run(coro)


def _create_current_user() -> UserInDB:
    created = create_user(
        UserCreate(email="profile.routes@example.com", password="StrongPass123")
    )
    current_user = auth_services.get_user_in_db_by_email("profile.routes@example.com")
    assert current_user is not None
    assert current_user.id == created.id
    return current_user


def test_get_my_profile_projects_existing_profile() -> None:
    current_user = _create_current_user()
    create_profile(
        ProfileCreate(
            user_id=current_user.id,
            name="Profile Name",
            phone="555-0100",
            address="Profile Address",
        )
    )

    result = _run(profiles_route.read_my_profile(current_user=current_user))

    assert isinstance(result, ProfileMeResponse)
    assert result.model_dump() == {
        "name": "Profile Name",
        "phone": "555-0100",
        "address": "Profile Address",
    }
    assert set(result.model_dump()) == {"name", "phone", "address"}
    assert "id" not in result.model_dump()
    assert "user_id" not in result.model_dump()


def test_get_my_profile_preserves_not_found_status() -> None:
    current_user = _create_current_user()

    with pytest.raises(HTTPException) as exc_info:
        _run(profiles_route.read_my_profile(current_user=current_user))

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Profile not found"


def test_put_my_profile_updates_existing_profile_and_projects_fields() -> None:
    current_user = _create_current_user()
    create_profile(
        ProfileCreate(
            user_id=current_user.id,
            name="Before",
            phone="555-0100",
            address="Before Address",
        )
    )

    result = _run(
        profiles_route.update_my_profile(
            payload=ProfileUpdate(
                name="After",
                phone="555-0101",
                address="After Address",
            ),
            current_user=current_user,
        )
    )

    assert isinstance(result, ProfileMeResponse)
    assert result.model_dump() == {
        "name": "After",
        "phone": "555-0101",
        "address": "After Address",
    }


def test_put_my_profile_creates_missing_profile_and_preserves_nullability() -> None:
    current_user = _create_current_user()

    result = _run(
        profiles_route.update_my_profile(
            payload=ProfileUpdate(name="Created", phone=None, address=None),
            current_user=current_user,
        )
    )

    assert isinstance(result, ProfileMeResponse)
    assert result.model_dump() == {
        "name": "Created",
        "phone": None,
        "address": None,
    }