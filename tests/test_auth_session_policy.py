"""Regression coverage for the local token policy shared by all transports."""

import base64
from datetime import datetime, timezone
import hmac
import json

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.exceptions import InvalidTokenError
from starlette.websockets import WebSocketDisconnect

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import create_access_token, decode_password_reset_token
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY, _load_jwt_secret, _positive_minutes
from services.api.chat.router import router as chat_router
from services.api.routes.auth import router as auth_router
from services.api.routes.rfp_notifications import router as notifications_router


@pytest.fixture
def auth_client(isolated_auth_db):
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(notifications_router)
    app.include_router(chat_router)
    user = UserInDB(email="local-auth-qa@example.test", hashed_password="unused", role=Role.admin)
    isolated_auth_db.users.insert(user.model_dump(mode="json"))
    return TestClient(app), user.id


@pytest.mark.parametrize(
    "claim_case",
    ["no_expiration", "past", "decimal", "numeric_text", "boolean", "no_purpose", "password_reset"],
)
def test_all_transports_reject_invalid_local_session_claims(auth_client, claim_case):
    client, user_id = auth_client
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"sub": user_id, "type": "access", "exp": now + 60}
    if claim_case == "no_expiration":
        del claims["exp"]
    elif claim_case == "no_purpose":
        del claims["type"]
    elif claim_case == "password_reset":
        claims["type"] = "password_reset"
    else:
        claims["exp"] = {
            "past": now - 1,
            "decimal": now + 60.5,
            "numeric_text": str(now + 60),
            "boolean": True,
        }[claim_case]
    token = jwt.encode(claims, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": "0"}
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/notifications/stream", headers=headers).status_code == 401
    with client.websocket_connect("/chat/ws") as socket:
        socket.send_json({
            "event": "authenticate",
            "data": {"session_id": "chat_1", "access_token": token},
        })
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 4401


def test_issued_local_access_token_authenticates_active_user(auth_client):
    client, user_id = auth_client
    token = create_access_token(user_id)
    result = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 200
    assert result.json()["id"] == user_id


def _sign_pre_migration_token(claims):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = encode(json.dumps({"alg": JWT_ALGORITHM, "typ": "JWT"}).encode())
    payload = encode(json.dumps(claims).encode())
    body = b".".join((header, payload))
    signature = hmac.digest(JWT_SECRET_KEY.encode(), body, "sha" + JWT_ALGORITHM[2:])
    return b".".join((body, encode(signature))).decode()


def test_3b_hmac_wire_format_remains_compatible(auth_client):
    client, user_id = auth_client
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"sub": user_id, "type": "access", "exp": now + 60, "iat": now}
    token = _sign_pre_migration_token(claims)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    claims.update(type="password_reset", jti="synthetic-pre-migration-reset")
    reset = decode_password_reset_token(_sign_pre_migration_token(claims))
    assert reset["jti"] == "synthetic-pre-migration-reset"


@pytest.mark.parametrize("claims", [{"aud": []}, {"at_hash": "synthetic"}, {"iat": "invalid"}])
def test_local_session_rejects_incompatible_optional_claims(auth_client, claims):
    client, user_id = auth_client
    now = int(datetime.now(timezone.utc).timestamp())
    token = _sign_pre_migration_token({"sub": user_id, "type": "access", "exp": now + 60, **claims})
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_reset_requires_expiration():
    token = jwt.encode(
        {"sub": "synthetic-user", "type": "password_reset", "jti": "synthetic-reset"},
        JWT_SECRET_KEY, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(InvalidTokenError):
        decode_password_reset_token(token)


@pytest.mark.parametrize("secret", [None, "short", "a" * 64, "Change-me-to-a-long-random-string", "abcd1234" * 8])
def test_jwt_secret_policy_rejects_unsafe_values_without_disclosing_them(secret):
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY") as exc:
        _load_jwt_secret(secret, "HS256")
    assert not secret or secret not in str(exc.value)


@pytest.mark.parametrize(("algorithm", "size"), [("HS256", 32), ("HS384", 48), ("HS512", 64)])
def test_jwt_secret_minimum_depends_on_configured_algorithm(algorithm, size):
    synthetic = "abcdefghijkmnoprstuvwxzABCDEF0123456789-_QRSTUVWXZlOY!@#%&?)(=+}[]"
    assert _load_jwt_secret(synthetic[:size], algorithm) == synthetic[:size]
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        _load_jwt_secret(synthetic[:size - 1], algorithm)


def test_local_jwt_rejects_external_algorithms():
    with pytest.raises(RuntimeError, match="JWT_ALGORITHM"):
        _load_jwt_secret(None, "RS256")


@pytest.mark.parametrize("value", ["-1", "0", "invalid"])
def test_configured_session_duration_must_be_positive(monkeypatch, value):
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", value)
    with pytest.raises(RuntimeError, match="JWT_EXPIRE_MINUTES"):
        _positive_minutes("JWT_EXPIRE_MINUTES", 60)
