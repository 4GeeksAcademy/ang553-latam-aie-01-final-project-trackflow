"""Functional unit tests for AUTH-088 login flow (POST /auth/login)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt

from services.api.auth_models import UserCreate, UserUpdate
from services.api.auth_security import get_current_user
from services.api.auth_services import create_user, update_user
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY
from services.api.routes import auth as auth_route
from services.api.main import app


def _run(coro):
    """Run async route handlers in sync pytest tests without TestClient."""
    return asyncio.run(coro)


def _form(email: str, password: str) -> OAuth2PasswordRequestForm:
    return OAuth2PasswordRequestForm(username=email, password=password, scope="")


def _request(request_id: str):
    return SimpleNamespace(state=SimpleNamespace(request_id=request_id))


async def _http_login(email: str, password: str, request_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/auth/login",
            data={"username": email, "password": password},
            headers={"X-Request-ID": request_id},
        )


def test_http_login_captures_one_success_event_with_contract_fields(monkeypatch) -> None:
    created = create_user(
        UserCreate(email="telemetry.login@example.com", password="StrongPass123")
    )
    captured = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    request_id = "550e8400-e29b-41d4-a716-446655440010"

    response = asyncio.run(
        _http_login("telemetry.login@example.com", "StrongPass123", request_id)
    )

    assert response.status_code == 200
    assert set(response.json()) == {"access_token", "token_type"}
    assert response.headers["X-Request-ID"] == request_id
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "auth_login_succeeded"
    assert event.eventId.version == 4
    assert event.timestamp.tzinfo is not None
    assert event.sessionId is None
    assert event.userId == created.id
    assert event.requestId == request_id
    assert event.schemaVersion == "1.0"
    assert event.properties == {"role": "user"}
    assert all(item.event_type != "auth_login_failed" for item in captured)


def test_login_succeeds_when_telemetry_capture_fails(monkeypatch) -> None:
    create_user(UserCreate(email="telemetry.failure@example.com", password="StrongPass123"))
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )

    response = asyncio.run(
        _http_login(
            "telemetry.failure@example.com",
            "StrongPass123",
            "550e8400-e29b-41d4-a716-446655440011",
        )
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert isinstance(response.json()["access_token"], str)
    assert response.json()["access_token"]


def test_login_with_valid_active_credentials_returns_decodable_access_token() -> None:
    """AUTH-LOGIN-HP-01: active user + correct password produces access token."""
    created = create_user(
        UserCreate(email="active.user@example.com", password="StrongPass123")
    )

    result = _run(
        auth_route.login(
            _request("550e8400-e29b-41d4-a716-446655440000"),
            _form("active.user@example.com", "StrongPass123"),
        )
    )

    assert result["token_type"] == "bearer"
    assert isinstance(result["access_token"], str)
    assert result["access_token"]

    payload = jwt.decode(
        result["access_token"],
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
    )
    assert payload["sub"] == created.id
    assert "exp" in payload
    assert "iat" in payload

    current_user = get_current_user(result["access_token"])
    assert current_user.id == created.id


def test_login_normalizes_email_with_spaces_and_case_differences() -> None:
    """AUTH-LOGIN-EDGE-01: login lookup normalizes email."""
    created = create_user(
        UserCreate(email="case.user@example.com", password="StrongPass123")
    )

    result = _run(
        auth_route.login(
            _request("550e8400-e29b-41d4-a716-446655440001"),
            _form("  CASE.USER@EXAMPLE.COM  ", "StrongPass123"),
        )
    )

    current_user = get_current_user(result["access_token"])
    assert current_user.id == created.id


def test_login_rejects_incorrect_password_with_generic_credentials_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-LOGIN-FAIL-01: wrong password is rejected."""
    created = create_user(
        UserCreate(email="wrong.password@example.com", password="StrongPass123")
    )
    captured = []
    persisted = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    monkeypatch.setattr(
        auth_route,
        "persist_backend_telemetry_best_effort",
        lambda events: persisted.extend(events),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440002"),
                _form("wrong.password@example.com", "BadPass999"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "auth_login_failed"
    assert event.userId == created.id
    assert event.requestId == "550e8400-e29b-41d4-a716-446655440002"
    assert event.sessionId is None
    assert event.schemaVersion == "1.0"
    assert event.eventId.version == 4
    assert event.properties == {"failure_reason": "invalid_password"}
    assert persisted == [event]


def test_login_rejects_non_existent_user_with_generic_credentials_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-LOGIN-FAIL-02: unknown user is rejected generically."""
    captured = []
    persisted = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    monkeypatch.setattr(
        auth_route,
        "persist_backend_telemetry_best_effort",
        lambda events: persisted.extend(events),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440003"),
                _form("not.found@example.com", "StrongPass123"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "auth_login_failed"
    assert event.userId is None
    assert event.requestId == "550e8400-e29b-41d4-a716-446655440003"
    assert event.sessionId is None
    assert event.schemaVersion == "1.0"
    assert event.eventId.version == 4
    assert event.properties == {"failure_reason": "account_not_found"}
    assert persisted == [event]


def test_login_rejects_inactive_user_even_with_correct_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-LOGIN-FAIL-03: inactive users cannot log in."""
    created = create_user(UserCreate(email="inactive@example.com", password="StrongPass123"))
    update_user(created.id, UserUpdate(is_active=False))
    captured = []
    persisted = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    monkeypatch.setattr(
        auth_route,
        "persist_backend_telemetry_best_effort",
        lambda events: persisted.extend(events),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440004"),
                _form("inactive@example.com", "StrongPass123"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Account is inactive"
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "auth_login_failed"
    assert event.userId == created.id
    assert event.requestId == "550e8400-e29b-41d4-a716-446655440004"
    assert event.sessionId is None
    assert event.schemaVersion == "1.0"
    assert event.eventId.version == 4
    assert event.properties == {
        "failure_reason": "inactive_account",
        "role_if_known": "user",
    }
    assert persisted == [event]


def test_failed_login_preserves_401_when_telemetry_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_user(UserCreate(email="telemetry.failed.login@example.com", password="StrongPass123"))
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    persisted = []
    monkeypatch.setattr(
        auth_route,
        "persist_backend_telemetry_best_effort",
        lambda events: persisted.extend(events),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440005"),
                _form("telemetry.failed.login@example.com", "BadPass999"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}
    assert len(persisted) == 1
    assert persisted[0].event_type == "auth_login_failed"


def test_failed_login_preserves_401_when_telemetry_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_user(UserCreate(email="telemetry.failed.persistence@example.com", password="StrongPass123"))
    captured = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    monkeypatch.setattr(
        auth_route,
        "persist_backend_telemetry_best_effort",
        lambda events: (_ for _ in ()).throw(RuntimeError("storage failed")),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440006"),
                _form("telemetry.failed.persistence@example.com", "BadPass999"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert len(captured) == 1
    assert captured[0].properties == {"failure_reason": "invalid_password"}


def test_failed_login_does_not_require_database_when_telemetry_session_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_user(UserCreate(email="telemetry.no.database@example.com", password="StrongPass123"))
    captured = []
    monkeypatch.setattr(
        auth_route,
        "capture_telemetry_events",
        lambda events: captured.extend(events),
    )
    monkeypatch.setattr(
        "services.api.telemetry_storage.open_db_session",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(HTTPException) as exc_info:
        _run(
            auth_route.login(
                _request("550e8400-e29b-41d4-a716-446655440007"),
                _form("telemetry.no.database@example.com", "BadPass999"),
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Incorrect email or password"
    assert len(captured) == 1
    assert captured[0].properties == {"failure_reason": "invalid_password"}
