from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.routes import rag
from services.rag.contracts import QueryResult, RAGProviderError, SourceReference


class ASGIClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def post(self, url: str, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as session:
                return await session.post(url, **kwargs)

        return asyncio.run(send())


class FakeRAGService:
    def __init__(self, result: QueryResult | Exception):
        self.result = result
        self.questions: list[str] = []
        self.request_ids: list[str | None] = []

    def query(self, question: str, *, request_id: str | None = None) -> QueryResult:
        self.questions.append(question)
        self.request_ids.append(request_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def auth_user(role: Role) -> UserInDB:
    return UserInDB(
        id=str(uuid4()),
        email=f"{role.value}@example.com",
        hashed_password="hash",
        role=role,
    )


def app_with(role: Role | None, service: FakeRAGService) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def add_request_id(request, call_next):
        request.state.request_id = "550e8400-e29b-41d4-a716-446655440097"
        return await call_next(request)

    app.include_router(rag.router)
    if role is not None:
        app.dependency_overrides[get_current_user] = lambda: auth_user(role)
    app.dependency_overrides[rag.get_rag_service] = lambda: service
    return app


def test_query_requires_authentication_and_authorized_role():
    service = FakeRAGService(QueryResult("answer", (), False))
    assert ASGIClient(app_with(None, service)).post(
        "/knowledge/query", json={"question": "Question?"}
    ).status_code == 401
    assert ASGIClient(app_with(None, service)).post(
        "/knowledge/query",
        json={"question": "Question?"},
        headers={"Authorization": "Bearer invalid-token"},
    ).status_code == 401
    assert ASGIClient(app_with(Role.user, service)).post(
        "/knowledge/query", json={"question": "Question?"}
    ).status_code == 403
    assert service.questions == []


def test_query_returns_answer_and_source_traceability_for_manager():
    service = FakeRAGService(
        QueryResult(
            "TrackFlow does not guarantee the SLA during Black Friday.",
            (SourceReference("sla-delivery", "high-demand-periods"),),
            False,
        )
    )
    response = ASGIClient(app_with(Role.manager, service)).post(
        "/knowledge/query", json={"question": "  Black Friday?  "}
    )
    assert response.status_code == 200
    assert response.json() == {
        "answer": "TrackFlow does not guarantee the SLA during Black Friday.",
        "insufficient_context": False,
        "sources": [
            {
                "source_document": "sla-delivery",
                "section": "high-demand-periods",
                    "retrieval_kind": "primary",
            }
        ],
    }
    assert service.questions == ["Black Friday?"]
    assert service.request_ids == ["550e8400-e29b-41d4-a716-446655440097"]


def test_query_supports_controlled_abstention_for_admin():
    service = FakeRAGService(
        QueryResult("There is not enough information.", (), True)
    )
    response = ASGIClient(app_with(Role.admin, service)).post(
        "/knowledge/query", json={"question": "Hazardous materials?"}
    )
    assert response.status_code == 200
    assert response.json()["insufficient_context"] is True
    assert response.json()["sources"] == []


@pytest.mark.parametrize(
    ("category", "status_code"),
    [
        ("timeout", None),
        ("http_error", 429),
        ("network_error", None),
        ("validation_error", None),
        ("provider_error", None),
    ],
)
def test_provider_failure_is_sanitized(category, status_code):
    service = FakeRAGService(
        RAGProviderError(
            "Bearer secret-token postgresql://user:secret@host/db",
            category=category,
            status_code=status_code,
        )
    )
    response = ASGIClient(app_with(Role.admin, service)).post(
        "/knowledge/query", json={"question": "Storage price?"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "knowledge service unavailable"}
    assert "secret" not in response.text


def test_live_query_is_gated_until_threshold_is_calibrated(monkeypatch):
    monkeypatch.setenv("FOUR_GEEKS_API_KEY", "test-only-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.delenv("RAG_MIN_SCORE", raising=False)
    app = FastAPI()
    app.include_router(rag.router)
    app.dependency_overrides[get_current_user] = lambda: auth_user(Role.admin)

    response = ASGIClient(app).post(
        "/knowledge/query", json={"question": "Storage price?"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "knowledge service unavailable"}


def test_request_contract_forbids_blank_oversized_and_extra_fields():
    client = ASGIClient(
        app_with(Role.manager, FakeRAGService(QueryResult("answer", (), False)))
    )
    assert client.post("/knowledge/query", json={"question": "   "}).status_code == 422
    assert client.post(
        "/knowledge/query", json={"question": "x" * 2001}
    ).status_code == 422
    assert client.post(
        "/knowledge/query", json={"question": "ok", "api_key": "secret"}
    ).status_code == 422


def test_openapi_exposes_nominal_contracts():
    app = FastAPI()
    app.include_router(rag.router)
    schema = app.openapi()
    operation = schema["paths"]["/knowledge/query"]["post"]
    assert {"200", "401", "403", "422", "503"} <= set(operation["responses"])
    assert "KnowledgeQueryRequest" in schema["components"]["schemas"]
    assert "KnowledgeQueryResponse" in schema["components"]["schemas"]
