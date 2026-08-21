"""
TrackFlow Auth routes — login and token-verified identity.

All routes live under ``/auth``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from services.api.auth_models import (
    ForgotPasswordRequest,
    MessageResponse,
    ResetPasswordRequest,
    UserInDB,
    UserResponse,
)
from services.api.auth_security import (
    create_access_token,
    get_current_user,
    verify_password,
)
from services.api.auth_services import (
    get_user_in_db_by_email,
    invalidate_password_reset_token,
    issue_password_reset_token,
    reset_password,
)
from services.api.email_service import send_password_reset_email

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


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
) -> MessageResponse:
    """Request a password-reset email.

    Always returns HTTP 200 with a generic message regardless of whether the
    email exists, to prevent user enumeration.

    If the email exists:
        - Generates a single-use password-reset JWT.
        - Persists its ``jti`` hash in TinyDB.
        - Sends the reset link via Resend.

    If the email does not exist:
        - Silently returns the generic message (no token, no email).
        - This prevents leaking account existence.

    If Resend fails for an existing user:
        - Returns HTTP 500 — a genuine infrastructure error is not hidden.
    """
    generic_message = (
        "If that email address is in our system, "
        "you will receive a password reset link."
    )

    user = get_user_in_db_by_email(payload.email)

    if user is None:
        return MessageResponse(message=generic_message)

    # User exists — generate token, persist, and send email.
    # If email delivery fails, invalidate the token so it cannot be used.
    try:
        token = issue_password_reset_token(user_id=user.id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send password reset email. Please try again later.",
        ) from exc

    try:
        sent = send_password_reset_email(to_email=user.email, reset_token=token)
    except Exception:
        # Email delivery failed — invalidate the just-issued token.
        # If invalidation itself fails, treat it as an internal error too.
        try:
            invalidate_password_reset_token(token)
        except Exception as inv_exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not send password reset email. Please try again later.",
            ) from inv_exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send password reset email. Please try again later.",
        )

    if not sent:
        # Email returned False — invalidate the just-issued token.
        try:
            invalidate_password_reset_token(token)
        except Exception as inv_exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not send password reset email. Please try again later.",
            ) from inv_exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send password reset email. Please try again later.",
        )

    return MessageResponse(message=generic_message)


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password_endpoint(
    payload: ResetPasswordRequest,
) -> MessageResponse:
    """Reset a forgotten password using a single-use reset token.

    Expects:
        - ``token``: a valid password-reset JWT obtained via /auth/forgot-password.
        - ``new_password``: the new password (min 8 characters).

    The token is validated for signature, expiration, purpose (``password_reset``),
    and single-use state. On success the user's password is updated and the token
    is invalidated.

    Returns HTTP 400 for invalid, expired, or already-used tokens.
    """
    try:
        reset_password(token=payload.token, new_password=payload.new_password)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset token.",
        )

    return MessageResponse(message="Password reset successfully.")