"""
TrackFlow Profile management routes.

All routes live under ``/profiles``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from services.api.auth_models import (
    ProfileCreate,
    ProfileMeResponse,
    ProfileUpdate,
    UserInDB,
)
from services.api.auth_security import get_current_user
from services.api.auth_services import (
    create_profile,
    get_profile_by_user_id,
    update_profile,
)

router = APIRouter(prefix="/profiles", tags=["Profiles"])


# ── GET /profiles/me ─────────────────────────────────────────────────────────


@router.get("/me", response_model=ProfileMeResponse)
async def read_my_profile(
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> ProfileMeResponse:
    """Return the authenticated user's profile.

    Requires a valid Bearer token.
    """
    profile = get_profile_by_user_id(current_user.id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found",
        )
    return ProfileMeResponse(
        name=profile.name,
        phone=profile.phone,
        address=profile.address,
    )


# ── PUT /profiles/me ─────────────────────────────────────────────────────────


@router.put("/me", response_model=ProfileMeResponse)
async def update_my_profile(
    payload: ProfileUpdate,
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> ProfileMeResponse:
    """Update the authenticated user's profile.

    Only the authenticated user's own profile can be modified.
    ``user_id`` cannot be changed.
    """
    existing_profile = get_profile_by_user_id(current_user.id)

    if existing_profile is None:
        try:
            profile = create_profile(
                ProfileCreate(
                    user_id=current_user.id,
                    name=payload.name,
                    phone=payload.phone,
                    address=payload.address,
                )
            )
            return ProfileMeResponse(
                name=profile.name,
                phone=profile.phone,
                address=profile.address,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    try:
        profile = update_profile(current_user.id, payload)
        return ProfileMeResponse(
            name=profile.name,
            phone=profile.phone,
            address=profile.address,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )