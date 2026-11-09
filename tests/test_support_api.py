"""Focused contract tests for POST /support/query."""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from agents.customer_support.external_graph import (
    ExternalSupportExecution,
    ExternalSupportResult,
    SupportUnavailable,
)
from agents.customer_support.tools import TicketLookupResult, ToolStatus
from services.api.auth_models import Role, UserInDB
from services.api.routes.support import (
    SupportQueryResponse,
    SupportQueryRequest,
    query_support,
    require_support_reader,
)
from services.rag.contracts import RAGConfigurationError


def user(role: Role) -> UserInDB:
    return UserInDB(
        id=f"{role.value}-id",
        email=f"{role.value}@example.com",
        hashed_password="test-only",
        role=role,
    )


class FakeSession:
    class Bind:
        class Dialect:
            name = "sqlite"

        dialect = Dialect()

    def get_bind(self):
        return self.Bind()

    def rollback(self):
        return None


def request() -> Request:
    value = Request({"type": "http", "method": "POST", "path": "/support/query", "headers": []})
    value.state.request_id = "550e8400-e29b-41d4-a716-446655440097"
    return value


def ticket_result() -> ExternalSupportResult:
    return ExternalSupportResult(
        answer="Ticket TRF-000482 is OPEN.",
        route="ticket",
        partial=False,
        needs_clarification=False,
        ticket=TicketLookupResult(
            ToolStatus.ok,
            incident_id="TRF-000482",
            incident_status="OPEN",
            category="DELAYED_DELIVERY",
            version=1,
            updated_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
        ),
        inventory=None,
        knowledge=None,
        knowledge_status=None,
    )


def test_support_role_policy_allows_admin_manager_and_denies_user():
    assert require_support_reader(user(Role.admin)).role is Role.admin
    assert require_support_reader(user(Role.manager)).role is Role.manager
    with pytest.raises(HTTPException) as exc_info:
        require_support_reader(user(Role.user))
    assert exc_info.value.status_code == 403


def test_support_handler_sets_run_header_and_starts_shared_80_second_deadline():
    captured = {}

    class Agent:
        def execute(self, question, **kwargs):
            captured.update(kwargs)
            captured["question"] = question
            return ExternalSupportExecution(kwargs["run_id"], ticket_result())

    response = Response()
    result = query_support(
        SupportQueryRequest(question="Check ticket 482"),
        request(),
        response,
        user(Role.admin),
        FakeSession(),
        Agent(),
    )
    assert result.route == "ticket"
    assert result.ticket.incident_id == "TRF-000482"
    assert response.headers["X-TrackFlow-Run-ID"] == captured["run_id"]
    assert 0 < captured["deadline"].remaining() <= 80
    assert captured["request_id"] == "550e8400-e29b-41d4-a716-446655440097"


def test_support_handler_returns_sanitized_503_with_same_run_id():
    captured = {}

    class Agent:
        def execute(self, question, **kwargs):
            captured.update(kwargs)
            raise SupportUnavailable("postgresql://private")

    with pytest.raises(HTTPException) as exc_info:
        query_support(
            SupportQueryRequest(question="Explain the returns policy"),
            request(),
            Response(),
            user(Role.admin),
            FakeSession(),
            Agent(),
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "support service unavailable"
    assert exc_info.value.headers["X-TrackFlow-Run-ID"] == captured["run_id"]
    assert "private" not in exc_info.value.detail


def test_support_question_remains_required_and_trimmed():
    with pytest.raises(ValueError):
        SupportQueryRequest()
    assert SupportQueryRequest(question="  policy  ").question == "policy"
    with pytest.raises(ValueError):
        SupportQueryRequest(question="policy", client_id="client-a")


def test_support_response_contract_has_explicit_multi_source_shape():
    assert set(SupportQueryResponse.model_fields) == {
        "answer",
        "route",
        "partial",
        "needs_clarification",
        "ticket",
        "inventory",
        "knowledge",
    }


def test_existing_knowledge_dependency_preserves_context_lifecycle(monkeypatch):
    from services.api.routes import rag as rag_route

    events = []
    service = object()

    class Context:
        def __enter__(self):
            events.append("enter")
            return service

        def __exit__(self, *args):
            events.append("exit")

    monkeypatch.setattr(rag_route, "rag_service_context", Context)
    dependency = rag_route.get_rag_service()
    assert next(dependency) is service
    with pytest.raises(StopIteration):
        next(dependency)
    assert events == ["enter", "exit"]


def test_existing_knowledge_dependency_keeps_sanitized_configuration_503(monkeypatch):
    from services.api.routes import rag as rag_route

    class Context:
        def __enter__(self):
            raise RAGConfigurationError("private configuration")

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(rag_route, "rag_service_context", Context)
    dependency = rag_route.get_rag_service()
    with pytest.raises(HTTPException) as exc_info:
        next(dependency)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "knowledge service unavailable"
