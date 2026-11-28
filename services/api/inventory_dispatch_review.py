"""Signed, short-lived confirmation proofs for inventory dispatch commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError

from services.api.access_control import AccessContext
from services.api.auth_settings import JWT_ALGORITHM, JWT_SECRET_KEY


PURPOSE = "trackflow:inventory:dispatch-review:v1"
ISSUER = "trackflow-inventory"
AUDIENCE = "trackflow-inventory-dispatch"
TTL = timedelta(minutes=5)
PROOF_HEADER = "X-TrackFlow-Dispatch-Review"
logger = logging.getLogger("trackflow.inventory.dispatch_confirmation")
logger.setLevel(logging.INFO)


class _ConfirmationFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "event": "inventory_dispatch_confirmation",
            "actor_id": getattr(record, "actor_id", None),
            "operation": getattr(record, "operation", None),
            "result": getattr(record, "result", None),
            "occurred_at": getattr(record, "occurred_at", None),
            "confirmation_id": getattr(record, "confirmation_id", None),
        }, separators=(",", ":"))


if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(_ConfirmationFormatter())
    logger.addHandler(_handler)
logger.propagate = False
_SIGNING_KEY = hmac.new(
    JWT_SECRET_KEY.encode("utf-8"), PURPOSE.encode("utf-8"), hashlib.sha512
).digest()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_hash(access: AccessContext) -> str:
    return _digest({
        "global": access.global_access,
        "role": access.role.value,
        "clients": sorted(access.allowed_client_ids),
    })


def command_digest(
    *, operation: str, endpoint: str, payload: object,
    idempotency_key: UUID | None = None,
) -> str:
    return _digest({
        "operation": operation,
        "endpoint": endpoint,
        "payload": payload.model_dump(mode="json"),
        "idempotency_key": str(idempotency_key) if idempotency_key else None,
    })


def issue_review_proof(
    *, actor_id: str, access: AccessContext, tenant_id: str,
    operation: str, endpoint: str, payload: object,
    idempotency_key: UUID | None = None, now: datetime | None = None,
) -> tuple[str, datetime]:
    issued = now or datetime.now(timezone.utc)
    expires = issued + TTL
    confirmation_id = str(uuid4())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "type": PURPOSE,
        "sub": actor_id,
        "tenant": tenant_id,
        "scope_sha256": _scope_hash(access),
        "operation": operation,
        "endpoint": endpoint,
        "command_sha256": command_digest(
            operation=operation, endpoint=endpoint, payload=payload,
            idempotency_key=idempotency_key,
        ),
        "idempotency_key": str(idempotency_key) if idempotency_key else None,
        "jti": confirmation_id,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, _SIGNING_KEY, algorithm=JWT_ALGORITHM)
    audit_confirmation(
        actor_id, operation, "reviewed", issued,
        confirmation_id=confirmation_id,
    )
    return token, expires


def verify_review_proof(
    token: str | None,
    *, actor_id: str, access: AccessContext, tenant_id: str,
    operation: str, endpoint: str, payload: object,
    idempotency_key: UUID | None = None,
) -> dict:
    if not token:
        raise ValueError("dispatch review proof required")
    try:
        claims = jwt.decode(
            token,
            _SIGNING_KEY,
            algorithms=[JWT_ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": [
                "exp", "iat", "jti", "sub", "type", "tenant",
                "scope_sha256", "operation", "endpoint", "command_sha256",
            ]},
        )
    except InvalidTokenError as exc:
        raise ValueError("dispatch review proof invalid or expired") from exc
    expected_key = str(idempotency_key) if idempotency_key else None
    expected_digest = command_digest(
        operation=operation, endpoint=endpoint, payload=payload,
        idempotency_key=idempotency_key,
    )
    if not (
        claims.get("type") == PURPOSE
        and claims.get("sub") == actor_id
        and claims.get("tenant") == tenant_id
        and claims.get("scope_sha256") == _scope_hash(access)
        and claims.get("operation") == operation
        and claims.get("endpoint") == endpoint
        and claims.get("command_sha256") == expected_digest
        and claims.get("idempotency_key") == expected_key
    ):
        raise ValueError("dispatch review proof does not match this command")
    return claims


def audit_confirmation(
    actor_id: str, operation: str, result: str,
    occurred_at: datetime | None = None,
    *, confirmation_id: str | None = None,
) -> None:
    logger.info(
        "inventory_dispatch_confirmation",
        extra={
            "actor_id": actor_id,
            "operation": operation,
            "result": result,
            "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
            "confirmation_id": confirmation_id,
        },
    )
