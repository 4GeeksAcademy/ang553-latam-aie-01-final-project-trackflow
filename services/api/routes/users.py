"""
TrackFlow User management routes.

All routes live under ``/users``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from services.api.auth_models import (
    ProfileCreate,
    Role,
    UserInDB,
    UserRegister,
    UserResponse,
    UserUpdate,
)
from services.api.auth_security import get_current_user
from services.api.auth_services import (
    create_profile,
    create_user,
    delete_user,
    get_user_by_id,
    list_users as list_all_users,
    update_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Users"])


# ── POST /users (public) ─────────────────────────────────────────────────────


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_user(payload: UserRegister) -> UserResponse:
    """Register a new user.

    Public endpoint. Creates a user with ``role=user``.
    If profile data (``name``, ``phone``, ``address``) is provided,
    a linked Profile is also created.
    """
    # Build UserCreate
    from services.api.auth_models import UserCreate as _UserCreate

    user_payload = _UserCreate(email=payload.email, password=payload.password)
    try:
        user_resp = create_user(user_payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    # If profile data was provided, create profile
    if payload.name is not None or payload.phone is not None or payload.address is not None:
        profile_payload = ProfileCreate(
            user_id=user_resp.id,
            name=payload.name,
            phone=payload.phone,
            address=payload.address,
        )
        try:
            create_profile(profile_payload)
        except ValueError as e:
            # Rollback: delete the user that was just created
            try:
                delete_user(user_resp.id)
            except Exception:
                logger.exception(
                    "Rollback failed while cleaning up user '%s' after profile creation error",
                    user_resp.id,
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    return user_resp


# ── GET /users (admin only) ──────────────────────────────────────────────────


@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> list[UserResponse]:
    """List all users. Admin only."""
    if current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to list users",
        )
    return list_all_users()


# ── GET /users/{user_id} ─────────────────────────────────────────────────────


@router.get("/{user_id}", response_model=UserResponse)
async def read_user(
    user_id: str,
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> UserResponse:
    """Get a user by ID.

    Accessible by:
    - the user themselves
    - admin
    """
    if current_user.id != user_id and current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this user",
        )

    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


# ── PUT /users/{user_id} ─────────────────────────────────────────────────────


@router.put("/{user_id}", response_model=UserResponse)
async def update_user_endpoint(
    user_id: str,
    payload: UserUpdate,
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> UserResponse:
    """Update a user.

    Accessible by:
    - the user themselves
    - admin

    Rules:
    - Non-admin users CANNOT change their own ``role``.
    - Only admin can change ``role``.
    """
    # Authorization: only owner or admin
    if current_user.id != user_id and current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this user",
        )

    # Non-admin cannot change role
    if payload.role is not None and current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to change role",
        )

    # Non-admin cannot change is_active
    if payload.is_active is not None and current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to change is_active",
        )

    # Fetch existing user to verify existence
    existing = get_user_by_id(user_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    try:
        return update_user(user_id, payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


# ── DELETE /users/{user_id} ──────────────────────────────────────────────────


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_endpoint(
    user_id: str,
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> None:
    """Delete a user and their associated profile.

    Accessible by:
    - the user themselves
    - admin
    """
    if current_user.id != user_id and current_user.role != Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this user",
        )

    existing = get_user_by_id(user_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    delete_user(user_id)
    return None