"""Defensive invariants for signed inventory dispatch confirmations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
import logging
from uuid import uuid4

import pytest

from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.api.inventory_dispatch_review import (
    _ConfirmationFormatter,
    issue_review_proof,
    verify_review_proof,
)
from services.api.inventory_schemas import StockExitCreate


def _access(user_id: str = "manager-a", client_id: str = "client-a") -> AccessContext:
    return AccessContext(
        user_id=user_id,
        role=Role.manager,
        allowed_client_ids=frozenset({client_id}),
        sensitive_client_ids=frozenset(),
    )


def _payload(quantity: int = 2) -> StockExitCreate:
    return StockExitCreate(
        sku_id=7,
        quantity=quantity,
        exit_type="dispatch",
        tracking_number="SYNTHETIC-CONFIRMATION",
        warehouse="LA",
    )


def _issue(*, now=None):
    key = uuid4()
    token, expires = issue_review_proof(
        actor_id="manager-a",
        access=_access(),
        tenant_id="client-a",
        operation="standalone_dispatch",
        endpoint="/inventory/orders/outbound",
        payload=_payload(),
        idempotency_key=key,
        now=now,
    )
    return token, expires, key


def test_review_proof_binds_actor_scope_operation_payload_and_key():
    token, _expires, key = _issue()
    verified = verify_review_proof(
        token,
        actor_id="manager-a",
        access=_access(),
        tenant_id="client-a",
        operation="standalone_dispatch",
        endpoint="/inventory/orders/outbound",
        payload=_payload(),
        idempotency_key=key,
    )
    assert verified["sub"] == "manager-a"
    assert verified["jti"]

    mismatches = (
        {"actor_id": "manager-b"},
        {"access": _access(client_id="client-b")},
        {"tenant_id": "client-b"},
        {"operation": "commercial_order_create"},
        {"endpoint": "/inventory/commercial-orders"},
        {"payload": _payload(quantity=3)},
        {"idempotency_key": uuid4()},
    )
    baseline = {
        "actor_id": "manager-a",
        "access": _access(),
        "tenant_id": "client-a",
        "operation": "standalone_dispatch",
        "endpoint": "/inventory/orders/outbound",
        "payload": _payload(),
        "idempotency_key": key,
    }
    for change in mismatches:
        with pytest.raises(ValueError):
            verify_review_proof(token, **{**baseline, **change})


def test_review_proof_rejects_missing_modified_and_expired_values():
    token, _expires, key = _issue(
        now=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    kwargs = {
        "actor_id": "manager-a", "access": _access(),
        "tenant_id": "client-a", "operation": "standalone_dispatch",
        "endpoint": "/inventory/orders/outbound", "payload": _payload(),
        "idempotency_key": key,
    }
    for value in (None, token + "x", token):
        with pytest.raises(ValueError):
            verify_review_proof(value, **kwargs)


def test_confirmation_log_is_content_free_and_correlatable():
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_ConfirmationFormatter())
    logger = logging.getLogger("trackflow.inventory.dispatch_confirmation")
    logger.addHandler(handler)
    try:
        token, _expires, _key = _issue()
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "reviewed" in output
    assert "confirmation_id" in output
    assert token not in output
    assert "SYNTHETIC-CONFIRMATION" not in output
