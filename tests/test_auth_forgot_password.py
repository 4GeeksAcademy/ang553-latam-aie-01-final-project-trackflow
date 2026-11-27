"""Functional unit tests for AUTH-088 forgot-password flow (POST /auth/forgot-password)."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tinydb import Query

from services.api.auth_models import ForgotPasswordRequest, UserCreate
from services.api.auth_security import decode_password_reset_token
from services.api import auth_services, email_service
from services.api.auth_services import create_user, validate_password_reset_token
from services.api.routes import auth as auth_route


GENERIC_FORGOT_PASSWORD_MESSAGE = (
    "If that email address is in our system, "
    "you will receive a password reset link."
)


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def _request(request_id: str = "550e8400-e29b-41d4-a716-446655440020"):
    return SimpleNamespace(state=SimpleNamespace(request_id=request_id))


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
        auth_route.forgot_password(
            _request(), ForgotPasswordRequest(email="forgot.hp@example.com")
        )
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
            _request("550e8400-e29b-41d4-a716-446655440021"),
            ForgotPasswordRequest(email="forgot.edge.exists@example.com")
        )
    )
    token_count_after_happy = len(auth_services.password_reset_tokens.all())

    edge_response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440022"),
            ForgotPasswordRequest(email="not-registered@example.com"),
        )
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

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440023"),
            ForgotPasswordRequest(email="forgot.fail.send@example.com")
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
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

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440024"),
            ForgotPasswordRequest(email="forgot.fail.issue@example.com")
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert send_invocations == 0
    assert auth_services.password_reset_tokens.all() == before_records


def test_forgot_password_captures_account_not_found_event(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440025"),
            ForgotPasswordRequest(email="telemetry.unknown@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "auth_password_reset_requested"
    assert event.properties == {"request_outcome_class": "account_not_found"}
    assert event.userId is None
    assert event.sessionId is None
    assert event.requestId == "550e8400-e29b-41d4-a716-446655440025"
    assert event.schemaVersion == "1.0"
    assert event.eventId.version == 4


def test_forgot_password_captures_reset_email_sent_event(monkeypatch) -> None:
    created = create_user(
        UserCreate(email="telemetry.sent@example.com", password="StrongPass123")
    )
    captured = []
    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)
    monkeypatch.setattr(
        auth_route,
        "send_password_reset_email",
        lambda *, to_email, reset_token: True,
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440026"),
            ForgotPasswordRequest(email="telemetry.sent@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(captured) == 1
    assert captured[0].event_type == "auth_password_reset_requested"
    assert captured[0].properties == {"request_outcome_class": "reset_email_sent"}
    assert captured[0].userId == created.id


def test_forgot_password_captures_one_delivery_failed_event_for_false(monkeypatch) -> None:
    created = create_user(
        UserCreate(email="telemetry.false@example.com", password="StrongPass123")
    )
    captured = []
    invalidated = []
    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)
    monkeypatch.setattr(
        auth_route,
        "send_password_reset_email",
        lambda *, to_email, reset_token: False,
    )
    monkeypatch.setattr(
        auth_route,
        "invalidate_password_reset_token",
        lambda token: invalidated.append(token),
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440027"),
            ForgotPasswordRequest(email="telemetry.false@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(invalidated) == 1
    assert len(captured) == 1
    assert captured[0].properties == {"request_outcome_class": "delivery_failed"}
    assert captured[0].userId == created.id


def test_forgot_password_captures_one_delivery_failed_event_for_exception(monkeypatch) -> None:
    created = create_user(
        UserCreate(email="telemetry.exception@example.com", password="StrongPass123")
    )
    captured = []
    invalidated = []

    def send_failure(*, to_email, reset_token):
        raise RuntimeError("provider secret text")

    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)
    monkeypatch.setattr(auth_route, "send_password_reset_email", send_failure)
    monkeypatch.setattr(
        auth_route,
        "invalidate_password_reset_token",
        lambda token: invalidated.append(token),
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440028"),
            ForgotPasswordRequest(email="telemetry.exception@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(invalidated) == 1
    assert len(captured) == 1
    assert captured[0].properties == {"request_outcome_class": "delivery_failed"}
    assert "provider secret text" not in repr(captured[0].model_dump())
    assert captured[0].userId == created.id


def test_forgot_password_invalidation_failure_does_not_duplicate_event(monkeypatch) -> None:
    create_user(UserCreate(email="telemetry.invalidate@example.com", password="StrongPass123"))
    captured = []
    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)
    monkeypatch.setattr(
        auth_route,
        "send_password_reset_email",
        lambda *, to_email, reset_token: False,
    )
    monkeypatch.setattr(
        auth_route,
        "invalidate_password_reset_token",
        lambda token: (_ for _ in ()).throw(RuntimeError("invalidation failure")),
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440029"),
            ForgotPasswordRequest(email="telemetry.invalidate@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(captured) == 1
    assert captured[0].properties == {"request_outcome_class": "delivery_failed"}


def test_forgot_password_token_issuance_failure_captures_no_event(monkeypatch) -> None:
    create_user(UserCreate(email="telemetry.issue@example.com", password="StrongPass123"))
    captured = []
    monkeypatch.setattr(auth_route, "capture_telemetry_events", captured.extend)
    monkeypatch.setattr(
        auth_route,
        "issue_password_reset_token",
        lambda *, user_id: (_ for _ in ()).throw(RuntimeError("token failure")),
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440030"),
            ForgotPasswordRequest(email="telemetry.issue@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE
    assert len(captured) == 0


def test_forgot_password_preserves_response_when_telemetry_capture_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: (_ for _ in ()).throw(RuntimeError("capture failure")),
    )

    response = _run(
        auth_route.forgot_password(
            _request("550e8400-e29b-41d4-a716-446655440031"),
            ForgotPasswordRequest(email="telemetry.capture-failure@example.com"),
        )
    )

    assert response.message == GENERIC_FORGOT_PASSWORD_MESSAGE


def test_forgot_password_public_http_response_and_provider_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    registered_email = "forgot.http@example.com"
    create_user(UserCreate(email=registered_email, password="StrongPass123"))
    monkeypatch.setattr(email_service, "RESEND_API_KEY", "synthetic-resend-key")
    monkeypatch.setattr(email_service, "FRONTEND_URL", "https://trackflow.example")
    monkeypatch.setattr(email_service.resend, "api_key", None)
    delivered: list[dict] = []

    def send(payload: dict) -> dict[str, str]:
        delivered.append(payload)
        if len(delivered) == 2:
            raise RuntimeError(f"provider diagnostic: {payload['to']} {payload['text']}")
        return {"id": "synthetic-provider-message-id"}

    monkeypatch.setattr(email_service.resend.Emails, "send", send)
    app = FastAPI()
    app.include_router(auth_route.router)
    with TestClient(app) as client:
        responses = [
            client.post("/auth/forgot-password", json={"email": email})
            for email in (registered_email, registered_email, "missing.http@example.com")
        ]

    assert len(delivered) == 2
    for response in responses:
        assert response.status_code == 200
        assert response.json() == {"message": GENERIC_FORGOT_PASSWORD_MESSAGE}

    accepted, failed = auth_services.password_reset_tokens.all()
    assert accepted["used"] is False
    assert failed["used"] is True
    assert "Password reset email delivery failed" in caplog.text
    assert "provider diagnostic" not in caplog.text
    assert registered_email not in caplog.text
    assert "reset-password?token=" not in caplog.text
