"""Investigation test for token-purpose separation in authentication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

from services.api.auth_models import UserCreate
from services.api.auth_security import decode_password_reset_token, get_current_user
from services.api.auth_services import create_user, issue_password_reset_token
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY


def test_password_reset_token_is_rejected_by_get_current_user() -> None:
    """AI-assisted case: reset tokens must not authenticate as session access tokens."""
    created = create_user(
        UserCreate(email="token.purpose@example.com", password="StrongPass123")
    )

    reset_token = issue_password_reset_token(user_id=created.id)

    payload = decode_password_reset_token(reset_token)
    assert payload["sub"] == created.id
    assert payload["type"] == "password_reset"

    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    assert expires_at > datetime.now(timezone.utc)

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(reset_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_legacy_access_token_without_type_is_accepted_by_get_current_user() -> None:
    """Legacy compatibility: access token without type claim should still authenticate."""
    created = create_user(
        UserCreate(email="token.legacy@example.com", password="StrongPass123")
    )

    now = datetime.now(timezone.utc)
    legacy_access_token = jwt.encode(
        {
            "sub": created.id,
            "exp": now + timedelta(minutes=30),
            "iat": now,
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    current_user = get_current_user(legacy_access_token)
    assert current_user.id == created.id