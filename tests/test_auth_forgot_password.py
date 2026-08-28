"""Functional unit tests for AUTH-088 forgot-password flow (POST /auth/forgot-password)."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from fastapi import HTTPException
from tinydb import Query

from services.api.auth_models import ForgotPasswordRequest, UserCreate
from services.api.auth_security import decode_password_reset_token
from services.api import auth_services
from services.api.auth_services import create_user, validate_password_reset_token
from services.api.routes import auth as auth_route


GENERIC_FORGOT_PASSWORD_MESSAGE = (
    "If that email address is in our system, "
    "you will receive a password reset link."
)


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def test_forgot_password_existing_user_issues_persists_token_and_sends_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-FORGOT-HP-01: existing user gets token issued/persisted and email flow called."""
    created = create_user(UserCreate(email="forgot.hp@example.com", password="StrongPass123"))

    observed: dict[str, str] = {}

    def fake_send_password_reset_email(*, to_email: str, reset_token: str) -> bool:
        observed["to_email"] = to_email
        observed["reset_token"] = reset_token
        return True

    monkeypatch.setattr(auth_route, "send_password_reset_email", fake_send_password_reset_email)

    response = _run(
        auth_route.forgot_password(ForgotPasswordRequest(email="forgot.hp@example.com"))
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert observed["to_email"] == "forgot.hp@example.com"

    token = observed["reset_token"]
    payload = decode_password_reset_token(token)
    assert payload["sub"] == created.id

    expected_jti_hash = hashlib.sha256(payload["jti"].encode("utf-8")).hexdigest()
    q = Query()
    records = auth_services.password_reset_tokens.search(q.jti_hash == expected_jti_hash)

    assert len(records) == 1
    record = records[0]
    assert record["user_id"] == created.id
    assert record["used"] is False


def test_forgot_password_unknown_email_returns_same_generic_message_and_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-FORGOT-EDGE-01: unknown email keeps anti-enumeration and avoids token/email side effects."""
    create_user(UserCreate(email="forgot.edge.exists@example.com", password="StrongPass123"))

    sent_calls: list[tuple[str, str]] = []

    def fake_send_password_reset_email(*, to_email: str, reset_token: str) -> bool:
        sent_calls.append((to_email, reset_token))
        return True

    monkeypatch.setattr(auth_route, "send_password_reset_email", fake_send_password_reset_email)

    happy_response = _run(
        auth_route.forgot_password(
            ForgotPasswordRequest(email="forgot.edge.exists@example.com")
        )
    )
    token_count_after_happy = len(auth_services.password_reset_tokens.all())

    edge_response = _run(
        auth_route.forgot_password(ForgotPasswordRequest(email="not-registered@example.com"))
    )

    assert edge_response.message == happy_response.message
    assert edge_response.message == GENERIC_FORGOT_PASSWORD_MESSAGE

    assert len(sent_calls) == 1
    assert len(auth_services.password_reset_tokens.all()) == token_count_after_happy


def test_forgot_password_invalidates_issued_token_when_email_send_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-FORGOT-FAIL-01: if email provider returns False, issued reset token is invalidated."""
    create_user(UserCreate(email="forgot.fail.send@example.com", password="StrongPass123"))

    observed: dict[str, str] = {}

    def fake_send_password_reset_email(*, to_email: str, reset_token: str) -> bool:
        observed["to_email"] = to_email
        observed["reset_token"] = reset_token
        return False

    monkeypatch.setattr(auth_route, "send_password_reset_email", fake_send_password_reset_email)

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.forgot_password(
                ForgotPasswordRequest(email="forgot.fail.send@example.com")
            )
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Could not send password reset email. Please try again later."
    assert observed["to_email"] == "forgot.fail.send@example.com"

    token = observed["reset_token"]
    payload = decode_password_reset_token(token)
    expected_jti_hash = hashlib.sha256(payload["jti"].encode("utf-8")).hexdigest()

    q = Query()
    records = auth_services.password_reset_tokens.search(q.jti_hash == expected_jti_hash)
    assert len(records) == 1
    assert records[0]["used"] is True

    with pytest.raises(ValueError):
        validate_password_reset_token(token)


def test_forgot_password_handles_token_issuance_failure_without_email_or_partial_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-FORGOT-FAIL-02: token issuance failure is controlled; no email and no token persistence."""
    create_user(UserCreate(email="forgot.fail.issue@example.com", password="StrongPass123"))

    send_invocations = 0

    def fake_issue_password_reset_token(*, user_id: str) -> str:
        raise RuntimeError(f"cannot issue token for {user_id}")

    def fake_send_password_reset_email(*, to_email: str, reset_token: str) -> bool:
        nonlocal send_invocations
        send_invocations += 1
        return True

    monkeypatch.setattr(auth_route, "issue_password_reset_token", fake_issue_password_reset_token)
    monkeypatch.setattr(auth_route, "send_password_reset_email", fake_send_password_reset_email)

    before_records = auth_services.password_reset_tokens.all()

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.forgot_password(
                ForgotPasswordRequest(email="forgot.fail.issue@example.com")
            )
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Could not send password reset email. Please try again later."
    assert send_invocations == 0
    assert auth_services.password_reset_tokens.all() == before_records
