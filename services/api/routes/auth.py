"""
TrackFlow Auth routes — login and token-verified identity.

All routes live under ``/auth``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from services.api.auth_models import UserInDB, UserResponse
from services.api.auth_security import (
    create_access_token,
    get_current_user,
    verify_password,
)
from services.api.auth_services import get_user_in_db_by_email

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login")
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> dict[str, str]:
    """OAuth2-compatible login.

    Accepts ``username`` (email) and ``password`` via form data.

    Returns a signed JWT access token on success.
    """
    # 1. Look up user by email
    user = get_user_in_db_by_email(form_data.username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Verify password
    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. Reject inactive users
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. Generate JWT
    access_token = create_access_token(sub=user.id)

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserResponse)
async def read_users_me(
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> UserResponse:
    """Return the authenticated user's public profile.

    Requires a valid Bearer token.
    """
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        is_active=current_user.is_active,
        role=current_user.role,
        created_at=current_user.created_at,
    )