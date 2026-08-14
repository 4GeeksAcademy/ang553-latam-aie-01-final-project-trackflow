"""
TrackFlow Auth services — users & profiles CRUD.

Provides a thin business-logic layer over TinyDB.
"""
from __future__ import annotations

from tinydb import Query

from services.api.auth_database import profiles, users
from services.api.auth_models import (
    ProfileCreate,
    ProfileInDB,
    ProfileResponse,
    ProfileUpdate,
    Role,
    UserCreate,
    UserInDB,
    UserResponse,
    UserUpdate,
)
from services.api.auth_security import hash_password


def _user_to_response(user: UserInDB) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        role=user.role,
        created_at=user.created_at,
    )


def _profile_to_response(profile: ProfileInDB) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        user_id=profile.user_id,
        name=profile.name,
        phone=profile.phone,
        address=profile.address,
    )


_UserQuery = Query()
_ProfileQuery = Query()


def _find_user_doc_by_id(user_id: str) -> dict | None:
    """Return the raw TinyDB document for a user, or None."""
    results = users.search(_UserQuery.id == user_id)
    return results[0] if results else None


def _find_user_doc_by_email(email: str) -> dict | None:
    """Return the raw TinyDB document for a user by email, or None."""
    results = users.search(_UserQuery.email == email.strip().lower())
    return results[0] if results else None


def _find_profile_doc_by_user_id(user_id: str) -> dict | None:
    """Return the raw TinyDB document for a profile by user_id, or None."""
    results = profiles.search(_ProfileQuery.user_id == user_id)
    return results[0] if results else None


# ── User services ────────────────────────────────────────────────────────────


def create_user(payload: UserCreate) -> UserResponse:
    """Create a new user. Password is hashed with bcrypt before storage.

    New users always get ``role=Role.user`` — clients cannot self-assign roles.
    """
    existing = _find_user_doc_by_email(payload.email)
    if existing:
        raise ValueError(f"User with email '{payload.email}' already exists")

    hashed = hash_password(payload.password)
    user = UserInDB(
        email=payload.email,
        hashed_password=hashed,
        role=Role.user,
    )
    users.insert(user.model_dump(mode="json"))
    return _user_to_response(user)


def get_user_by_id(user_id: str) -> UserResponse | None:
    """Retrieve a user by UUID. Returns None if not found."""
    doc = _find_user_doc_by_id(user_id)
    if doc is None:
        return None
    return _user_to_response(UserInDB(**doc))


def get_user_by_email(email: str) -> UserResponse | None:
    """Retrieve a user by email. Returns None if not found."""
    doc = _find_user_doc_by_email(email)
    if doc is None:
        return None
    return _user_to_response(UserInDB(**doc))


def get_user_in_db_by_email(email: str) -> UserInDB | None:
    """Retrieve full UserInDB (with hashed_password) by email. Internal use."""
    doc = _find_user_doc_by_email(email)
    if doc is None:
        return None
    return UserInDB(**doc)


def update_user(user_id: str, payload: UserUpdate) -> UserResponse:
    """Update a user's mutable fields. Raises ValueError if not found.

    If ``payload.password`` is provided, it is hashed with bcrypt before
    storage as ``hashed_password``.
    """
    doc = _find_user_doc_by_id(user_id)
    if doc is None:
        raise ValueError(f"User '{user_id}' not found")

    update_data: dict = {}
    if payload.email is not None:
        # Check uniqueness
        existing = _find_user_doc_by_email(payload.email)
        if existing and existing["id"] != user_id:
            raise ValueError(f"Email '{payload.email}' already in use")
        update_data["email"] = payload.email.strip().lower()
    if payload.password is not None:
        update_data["hashed_password"] = hash_password(payload.password)
    if payload.is_active is not None:
        update_data["is_active"] = payload.is_active
    if payload.role is not None:
        update_data["role"] = payload.role.value

    if update_data:
        users.update(update_data, _UserQuery.id == user_id)

    updated = _find_user_doc_by_id(user_id)
    return _user_to_response(UserInDB(**updated))  # type: ignore[arg-type]


def delete_user(user_id: str) -> None:
    """Delete a user and their associated profile. Silent if not found."""
    # Delete profile first if it exists
    profile_doc = _find_profile_doc_by_user_id(user_id)
    if profile_doc:
        profiles.remove(_ProfileQuery.user_id == user_id)

    users.remove(_UserQuery.id == user_id)


# ── Profile services ─────────────────────────────────────────────────────────


def create_profile(payload: ProfileCreate) -> ProfileResponse:
    """Create a new profile for a user."""
    # Verify user exists
    user_doc = _find_user_doc_by_id(payload.user_id)
    if user_doc is None:
        raise ValueError(f"User '{payload.user_id}' not found")

    # Check profile doesn't already exist for this user
    existing = _find_profile_doc_by_user_id(payload.user_id)
    if existing:
        raise ValueError(f"Profile already exists for user '{payload.user_id}'")

    profile = ProfileInDB(
        user_id=payload.user_id,
        name=payload.name,
        phone=payload.phone,
        address=payload.address,
    )
    profiles.insert(profile.model_dump(mode="json"))
    return _profile_to_response(profile)


def get_profile_by_user_id(user_id: str) -> ProfileResponse | None:
    """Retrieve a profile by user_id. Returns None if not found."""
    doc = _find_profile_doc_by_user_id(user_id)
    if doc is None:
        return None
    return _profile_to_response(ProfileInDB(**doc))


def update_profile(user_id: str, payload: ProfileUpdate) -> ProfileResponse:
    """Update a profile by user_id. Raises ValueError if not found."""
    doc = _find_profile_doc_by_user_id(user_id)
    if doc is None:
        raise ValueError(f"Profile for user '{user_id}' not found")

    update_data: dict = {}
    if payload.name is not None:
        update_data["name"] = payload.name
    if payload.phone is not None:
        update_data["phone"] = payload.phone
    if payload.address is not None:
        update_data["address"] = payload.address

    if update_data:
        profiles.update(update_data, _ProfileQuery.user_id == user_id)

    updated = _find_profile_doc_by_user_id(user_id)
    return _profile_to_response(ProfileInDB(**updated))  # type: ignore[arg-type]


def delete_profile(user_id: str) -> None:
    """Delete a profile by user_id. Silent if not found."""
    profiles.remove(_ProfileQuery.user_id == user_id)