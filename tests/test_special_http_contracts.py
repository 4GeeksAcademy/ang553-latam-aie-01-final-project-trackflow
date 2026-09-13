"""Functional coverage for the special 204 and CSV HTTP contracts."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import Response

from services.api.auth_models import Role, UserInDB
from services.api.main import export_results
from services.api.routes import suppliers as suppliers_route
from services.api.routes import users as users_route


def _run(coro):
    return asyncio.run(coro)


def test_delete_user_returns_empty_204_response(monkeypatch) -> None:
    user = UserInDB(id="user-1", email="user@example.com", hashed_password="hash")
    monkeypatch.setattr(users_route, "get_user_by_id", lambda user_id: user)
    deleted: list[str] = []
    monkeypatch.setattr(users_route, "delete_user", deleted.append)

    response = _run(users_route.delete_user_endpoint("user-1", user))

    assert isinstance(response, Response)
    assert response.status_code == 204
    assert response.body == b""
    assert deleted == ["user-1"]


def test_delete_user_preserves_forbidden_and_not_found(monkeypatch) -> None:
    owner = UserInDB(id="owner", email="owner@example.com", hashed_password="hash")
    admin = UserInDB(
        id="admin", email="admin@example.com", hashed_password="hash", role=Role.admin
    )
    other = UserInDB(id="other", email="other@example.com", hashed_password="hash")

    with pytest.raises(HTTPException) as forbidden:
        _run(users_route.delete_user_endpoint("owner", other))
    assert forbidden.value.status_code == 403

    monkeypatch.setattr(users_route, "get_user_by_id", lambda user_id: None)
    with pytest.raises(HTTPException) as missing:
        _run(users_route.delete_user_endpoint("missing", admin))
    assert missing.value.status_code == 404


def test_delete_supplier_returns_empty_204_response(monkeypatch) -> None:
    class FakeTable:
        def get(self, *, doc_id):
            return {"name": "Supplier"}

        def remove(self, *, doc_ids):
            assert doc_ids == [1]

    monkeypatch.setattr(suppliers_route, "suppliers", FakeTable())

    response = suppliers_route.delete_supplier(1)

    assert isinstance(response, Response)
    assert response.status_code == 204
    assert response.body == b""


def test_delete_supplier_preserves_not_found(monkeypatch) -> None:
    class EmptyTable:
        def get(self, *, doc_id):
            return None

    monkeypatch.setattr(suppliers_route, "suppliers", EmptyTable())

    with pytest.raises(HTTPException) as missing:
        suppliers_route.delete_supplier(999)
    assert missing.value.status_code == 404


def test_export_results_returns_csv_attachment(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.api.main._last_result",
        {
            "total_records": 1,
            "valid_records": 1,
            "invalid_records": 0,
            "invalid_breakdown": {},
            "category_breakdown": {"DELIVERY_DELAY": 1},
            "status_breakdown": {"CLOSED": 1},
            "country_breakdown": {"CO": 1},
            "closed_scored": 1,
            "score_distribution": {5: 1},
            "average_satisfaction": 5.0,
        },
    )

    response = _run(export_results())

    assert response.media_type == "text/csv"
    assert response.headers["content-disposition"] == 'attachment; filename="results.csv"'
    assert response.body.startswith(b"section,metric,value")
    assert b"general,total_records,1" in response.body


def test_export_results_preserves_not_found(monkeypatch) -> None:
    monkeypatch.setattr("services.api.main._last_result", None)

    with pytest.raises(HTTPException) as missing:
        _run(export_results())
    assert missing.value.status_code == 404