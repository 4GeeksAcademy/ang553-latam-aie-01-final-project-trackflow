"""
TrackFlow Auth routes — login and token-verified identity.

All routes live under ``/auth``.
"""

from __future__ import annotations

import logging
from typing import Annotated
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from services.api.auth_models import (
    ChangePasswordRequest,
    AuthMeResponse,
    ForgotPasswordRequest,
    MessageResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserInDB,
)
from services.api.auth_security import (
    create_access_token,
    get_current_user,
    verify_password,
)
from services.api.auth_services import (
    change_password,
    get_user_in_db_by_email,
    invalidate_password_reset_token,
    issue_password_reset_token,
    reset_password,
)
from services.api.email_service import send_password_reset_email
from services.api.telemetry_capture import capture_telemetry_events
from services.api.telemetry_schemas import TelemetryEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _capture_login_failed(
    request: Request,
    failure_reason: str,
    user: UserInDB | None = None,
) -> None:
    """Capture a failed login without affecting the authentication response."""
    try:
        properties = {"failure_reason": failure_reason}
        if failure_reason == "inactive_account" and user is not None:
            properties["role_if_known"] = user.role.value

        login_event = TelemetryEvent(
            eventId=uuid4(),
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            sessionId=None,
            userId=user.id if user is not None else None,
            event_type="auth_login_failed",
            schemaVersion="1.0",
            requestId=getattr(request.state, "request_id", None),
            properties=properties,
        )
        capture_telemetry_events([login_event])
    except Exception:
        logger.warning("Telemetry capture failed for failed login")


def _capture_password_reset_requested(
    request: Request,
    outcome_class: str,
    user: UserInDB | None = None,
) -> None:
    """Capture a password-reset request without affecting its response."""
    try:
        reset_event = TelemetryEvent(
            eventId=uuid4(),
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            sessionId=None,
            userId=user.id if user is not None else None,
            event_type="auth_password_reset_requested",
            schemaVersion="1.0",
            requestId=getattr(request.state, "request_id", None),
            properties={"request_outcome_class": outcome_class},
        )
        capture_telemetry_events([reset_event])
    except Exception:
        logger.warning("Telemetry capture failed for password reset request")


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    """OAuth2-compatible login.

    Accepts ``username`` (email) and ``password`` via form data.

    Returns a signed JWT access token on success.
    """
    # 1. Look up user by email
    user = get_user_in_db_by_email(form_data.username)
    if user is None:
        _capture_login_failed(
            request,
            failure_reason="account_not_found",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Verify password
    if not verify_password(form_data.password, user.hashed_password):
        _capture_login_failed(
            request,
            failure_reason="invalid_password",
            user=user,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. Reject inactive users
    if not user.is_active:
        _capture_login_failed(
            request,
            failure_reason="inactive_account",
            user=user,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. Generate JWT
    access_token = create_access_token(sub=user.id)

    try:
        login_event = TelemetryEvent(
            eventId=uuid4(),
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            sessionId=None,
            userId=user.id,
            event_type="auth_login_succeeded",
            schemaVersion="1.0",
            requestId=getattr(request.state, "request_id", None),
            properties={"role": user.role.value},
        )
        capture_telemetry_events([login_event])
    except Exception:
        logger.warning("Telemetry capture failed for successful login")

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=AuthMeResponse)
async def read_users_me(
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> AuthMeResponse:
    """Return the authenticated user's public profile.

    Requires a valid Bearer token.
    """
    return AuthMeResponse(
        id=current_user.id,
        email=current_user.email,
        is_active=current_user.is_active,
        role=current_user.role,
    )


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    request: Request,
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
        _capture_password_reset_requested(
            request,
            outcome_class="account_not_found",
        )
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
            _capture_password_reset_requested(
                request,
                outcome_class="delivery_failed",
                user=user,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not send password reset email. Please try again later.",
            ) from inv_exc
        _capture_password_reset_requested(
            request,
            outcome_class="delivery_failed",
            user=user,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send password reset email. Please try again later.",
        )

    if not sent:
        # Email returned False — invalidate the just-issued token.
        try:
            invalidate_password_reset_token(token)
        except Exception as inv_exc:
            _capture_password_reset_requested(
                request,
                outcome_class="delivery_failed",
                user=user,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not send password reset email. Please try again later.",
            ) from inv_exc
        _capture_password_reset_requested(
            request,
            outcome_class="delivery_failed",
            user=user,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send password reset email. Please try again later.",
        )

    _capture_password_reset_requested(
        request,
        outcome_class="reset_email_sent",
        user=user,
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


@router.post("/change-password", response_model=MessageResponse)
async def change_password_endpoint(
    payload: ChangePasswordRequest,
    current_user: Annotated[UserInDB, Depends(get_current_user)],
) -> MessageResponse:
    """Change the authenticated user's password.

    Requires a valid Bearer token. The user must provide their current
    password for verification.

    Request body:
        - ``current_password``: the user's current password.
        - ``new_password``: the new password (min 8 characters).

    Returns HTTP 400 if the current password is incorrect.
    Returns HTTP 401 if not authenticated.
    """
    try:
        change_password(
            user_id=current_user.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return MessageResponse(message="Password changed successfully.")