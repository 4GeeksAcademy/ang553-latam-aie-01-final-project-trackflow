"""
TrackFlow Auth services — users & profiles CRUD.

Provides a thin business-logic layer over TinyDB.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from tinydb import Query

from services.api.auth_database import password_reset_tokens, profiles, users
from services.api.auth_models import (
    PasswordResetTokenInDB,
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
from services.api.auth_security import (
    create_password_reset_token,
    decode_password_reset_token,
    hash_password,
    verify_password,
)


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


def list_users() -> list[UserResponse]:
    """Return all users as public ``UserResponse`` objects.

    Reads from TinyDB and converts each stored user to a safe response
    (``hashed_password`` is never exposed).
    """
    docs = users.all()
    result: list[UserResponse] = []
    for doc in docs:
        result.append(_user_to_response(UserInDB(**doc)))
    return result


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


# ── Password-reset services ─────────────────────────────────────────────────

_PasswordResetQuery = Query()


def _hash_jti(jti: str) -> str:
    """Return the SHA-256 hex digest of a JWT ``jti`` claim.

    We store only the hash — never the raw JWT — to avoid persisting
    sensitive information unnecessarily.
    """
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def issue_password_reset_token(user_id: str) -> str:
    """Create a password-reset JWT and persist its ``jti`` hash in TinyDB.

    Args:
        user_id: The TinyDB user id.

    Returns:
        The signed password-reset JWT.
    """
    token = create_password_reset_token(sub=user_id)

    # Decode to extract the ``jti`` (we just created it, so it's valid)
    payload = decode_password_reset_token(token)
    jti = payload["jti"]

    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

    record = PasswordResetTokenInDB(
        jti_hash=_hash_jti(jti),
        user_id=user_id,
        expires_at=expires_at.isoformat(),
    )
    password_reset_tokens.insert(record.model_dump(mode="json"))

    return token


def validate_password_reset_token(token: str) -> str:
    """Validate a password-reset JWT and check its persisted state.

    Verifies:
        - JWT signature and expiration (via ``decode_password_reset_token``).
        - Purpose (``type: "password_reset"``).
        - The ``jti`` hash exists in TinyDB and is not marked as ``used``.

    Args:
        token: The raw password-reset JWT.

    Returns:
        The user id (``sub``) if the token is valid.

    Raises:
        ValueError: If the token is invalid, expired, already used, or not found.
    """
    try:
        payload = decode_password_reset_token(token)
    except Exception as exc:
        raise ValueError(f"Invalid or expired token: {exc}") from exc

    jti = payload["jti"]
    user_id = payload["sub"]
    jti_hash = _hash_jti(jti)

    results = password_reset_tokens.search(_PasswordResetQuery.jti_hash == jti_hash)
    if not results:
        raise ValueError("Token not found in persistence store")

    record = PasswordResetTokenInDB(**results[0])

    if record.used:
        raise ValueError("Token has already been used")

    # Double-check expiration from persisted record
    expires = datetime.fromisoformat(record.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if _now_utc() > expires:
        raise ValueError("Token has expired")

    return user_id


def invalidate_password_reset_token(token: str) -> None:
    """Mark a password-reset token as used so it cannot be reused.

    Args:
        token: The raw password-reset JWT.

    Raises:
        ValueError: If the token is invalid or the persisted record is missing.
    """
    try:
        payload = decode_password_reset_token(token)
    except Exception as exc:
        raise ValueError(f"Invalid token: {exc}") from exc

    jti = payload["jti"]
    jti_hash = _hash_jti(jti)

    results = password_reset_tokens.search(_PasswordResetQuery.jti_hash == jti_hash)
    if not results:
        raise ValueError("Token not found in persistence store")

    password_reset_tokens.update(
        {"used": True}, _PasswordResetQuery.jti_hash == jti_hash
    )


def reset_password(token: str, new_password: str) -> None:
    """Validate a password-reset token, update the user's password, and
    invalidate the token (single-use enforcement).

    The token is consumed **before** the password is updated. If the
    password update fails, the token remains invalidated — the user
    simply requests a new reset.

    Args:
        token: The raw password-reset JWT.
        new_password: The new plain-text password.

    Raises:
        ValueError: If the token is invalid, expired, already used,
                    or the user no longer exists.
    """
    # 1. Validate the token — raises ValueError on any issue
    user_id = validate_password_reset_token(token)

    # 2. Verify user still exists before consuming the token
    user_doc = _find_user_doc_by_id(user_id)
    if user_doc is None:
        raise ValueError("User not found")

    # 3. Hash the new password
    hashed = hash_password(new_password)

    # 4. Invalidate/consume the token FIRST — single-use guarantee.
    #    If this fails, the password is never changed, preserving consistency.
    invalidate_password_reset_token(token)

    # 5. Update the user's password
    users.update(
        {"hashed_password": hashed}, _UserQuery.id == user_id
    )


def change_password(user_id: str, current_password: str, new_password: str) -> None:
    """Change the password for an authenticated user.

    Verifies the current password before updating to the new one.

    Args:
        user_id: The TinyDB user id of the authenticated user.
        current_password: The user's current plain-text password.
        new_password: The new plain-text password.

    Raises:
        ValueError: If the current password is incorrect or the user
                    is not found.
    """
    doc = _find_user_doc_by_id(user_id)
    if doc is None:
        raise ValueError("User not found")

    user = UserInDB(**doc)

    # Verify current password
    if not verify_password(current_password, user.hashed_password):
        raise ValueError("Current password is incorrect.")

    # Hash and update
    hashed = hash_password(new_password)
    users.update({"hashed_password": hashed}, _UserQuery.id == user_id)