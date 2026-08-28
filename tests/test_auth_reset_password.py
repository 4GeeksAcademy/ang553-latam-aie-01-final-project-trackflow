"""Functional unit tests for AUTH-088 reset-password flow (POST /auth/reset-password)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt
from tinydb import Query

from services.api import auth_services
from services.api.auth_models import ResetPasswordRequest, UserCreate
from services.api.auth_security import (
    decode_password_reset_token,
    verify_password,
)
from services.api.auth_services import (
    create_user,
    delete_user,
    invalidate_password_reset_token,
    issue_password_reset_token,
    reset_password,
    validate_password_reset_token,
)
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY
from services.api.routes import auth as auth_route


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def _get_user_doc(user_id: str) -> dict:
    q = Query()
    matches = auth_services.users.search(q.id == user_id)
    assert len(matches) == 1
    return matches[0]


def _get_token_doc_by_jti_hash(jti_hash: str) -> dict:
    q = Query()
    matches = auth_services.password_reset_tokens.search(q.jti_hash == jti_hash)
    assert len(matches) == 1
    return matches[0]


def _hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def test_reset_password_valid_token_changes_password_and_enforces_single_use() -> None:
    """AUTH-RESET-HP-01: valid persisted unused token resets password once and cannot be reused."""
    created = create_user(UserCreate(email="reset.hp@example.com", password="OldStrongPass123"))
    before_user = _get_user_doc(created.id)
    old_hash = before_user["hashed_password"]

    token = issue_password_reset_token(user_id=created.id)
    payload = decode_password_reset_token(token)
    token_jti_hash = _hash_jti(payload["jti"])

    token_record_before = _get_token_doc_by_jti_hash(token_jti_hash)
    assert token_record_before["used"] is False

    reset_password(token=token, new_password="NewStrongPass456")

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] != old_hash
    assert verify_password("NewStrongPass456", after_user["hashed_password"]) is True
    assert verify_password("OldStrongPass123", after_user["hashed_password"]) is False

    token_record_after_first_use = _get_token_doc_by_jti_hash(token_jti_hash)
    assert token_record_after_first_use["used"] is True

    with pytest.raises(ValueError):
        reset_password(token=token, new_password="AnotherStrongPass789")

    after_reuse_attempt = _get_user_doc(created.id)
    assert after_reuse_attempt["hashed_password"] == after_user["hashed_password"]


def test_reset_password_rejects_valid_but_already_used_token_without_changing_password() -> None:
    """AUTH-RESET-EDGE-01: cryptographically valid token already marked used is rejected."""
    created = create_user(
        UserCreate(email="reset.edge.used@example.com", password="EdgeOldPass123")
    )
    before_user = _get_user_doc(created.id)

    token = issue_password_reset_token(user_id=created.id)
    payload = decode_password_reset_token(token)
    token_jti_hash = _hash_jti(payload["jti"])

    invalidate_password_reset_token(token)
    token_record = _get_token_doc_by_jti_hash(token_jti_hash)
    assert token_record["used"] is True

    with pytest.raises(ValueError):
        reset_password(token=token, new_password="EdgeNewPass456")

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] == before_user["hashed_password"]


def test_reset_password_rejects_expired_tokens_for_jwt_and_persisted_expiration() -> None:
    """AUTH-RESET-FAIL-01: expired token is rejected via JWT exp and persisted expires_at checks."""
    created = create_user(
        UserCreate(email="reset.fail.expired@example.com", password="ExpiredOldPass123")
    )
    before_user = _get_user_doc(created.id)
    old_hash = before_user["hashed_password"]

    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": created.id,
        "jti": "expired-jwt-jti",
        "type": "password_reset",
        "iat": now - timedelta(minutes=10),
        "exp": now - timedelta(minutes=5),
    }
    expired_jwt_token = jwt.encode(expired_payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    auth_services.password_reset_tokens.insert(
        {
            "jti_hash": _hash_jti("expired-jwt-jti"),
            "user_id": created.id,
            "expires_at": (now + timedelta(minutes=20)).isoformat(),
            "used": False,
        }
    )

    with pytest.raises(ValueError):
        validate_password_reset_token(expired_jwt_token)
    with pytest.raises(ValueError):
        reset_password(token=expired_jwt_token, new_password="ExpiredNewPass456")

    valid_token = issue_password_reset_token(user_id=created.id)
    valid_payload = decode_password_reset_token(valid_token)
    valid_jti_hash = _hash_jti(valid_payload["jti"])

    q = Query()
    auth_services.password_reset_tokens.update(
        {"expires_at": (now - timedelta(minutes=1)).isoformat(), "used": False},
        q.jti_hash == valid_jti_hash,
    )

    with pytest.raises(ValueError, match="Token has expired"):
        validate_password_reset_token(valid_token)
    with pytest.raises(ValueError):
        reset_password(token=valid_token, new_password="ExpiredNewPass789")

    token_record_after_persisted_exp = _get_token_doc_by_jti_hash(valid_jti_hash)
    assert token_record_after_persisted_exp["used"] is False

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] == old_hash


def test_reset_password_rejects_token_with_wrong_type_claim() -> None:
    """AUTH-RESET-FAIL-02: signed JWT with valid sub/jti but wrong type is rejected."""
    created = create_user(
        UserCreate(email="reset.fail.type@example.com", password="TypeOldPass123")
    )
    before_user = _get_user_doc(created.id)

    now = datetime.now(timezone.utc)
    wrong_type_jti = "wrong-type-jti"
    wrong_type_token = jwt.encode(
        {
            "sub": created.id,
            "jti": wrong_type_jti,
            "type": "access_token",
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )

    auth_services.password_reset_tokens.insert(
        {
            "jti_hash": _hash_jti(wrong_type_jti),
            "user_id": created.id,
            "expires_at": (now + timedelta(minutes=25)).isoformat(),
            "used": False,
        }
    )

    with pytest.raises(ValueError):
        validate_password_reset_token(wrong_type_token)
    with pytest.raises(ValueError):
        reset_password(token=wrong_type_token, new_password="TypeNewPass456")

    token_record_after = _get_token_doc_by_jti_hash(_hash_jti(wrong_type_jti))
    assert token_record_after["used"] is False

    after_user = _get_user_doc(created.id)
    assert after_user["hashed_password"] == before_user["hashed_password"]


def test_reset_password_rejects_valid_token_if_user_no_longer_exists() -> None:
    """AUTH-RESET-FAIL-03: valid token for deleted user is rejected without recreating user."""
    created = create_user(
        UserCreate(email="reset.fail.deleted@example.com", password="DeletedOldPass123")
    )
    token = issue_password_reset_token(user_id=created.id)
    payload = decode_password_reset_token(token)
    token_jti_hash = _hash_jti(payload["jti"])

    delete_user(created.id)

    with pytest.raises(ValueError, match="User not found"):
        reset_password(token=token, new_password="DeletedNewPass456")

    q = Query()
    assert auth_services.users.search(q.id == created.id) == []

    token_record_after = _get_token_doc_by_jti_hash(token_jti_hash)
    assert token_record_after["used"] is False


def test_reset_password_endpoint_maps_domain_valueerror_to_http_400() -> None:
    """Router mapping: business ValueError is translated to endpoint HTTP 400 response."""
    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.reset_password_endpoint(
                ResetPasswordRequest(
                    token="invalid.token.value",
                    new_password="RouterStrongPass123",
                )
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid or expired password reset token."