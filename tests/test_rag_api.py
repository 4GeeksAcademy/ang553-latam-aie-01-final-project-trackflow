from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from agents.customer_support import CustomerSupportAgent
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.routes import rag
from services.rag.contracts import (
    KnowledgeChunk,
    QueryResult,
    RAGProviderError,
    RetrievalResult,
    SourceReference,
)


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
        self.default_k = 3
        self.default_min_score = 0.5
        self.questions: list[str] = []
        self.request_ids: list[str | None] = []
        self.generation_calls: list[
            tuple[str, tuple[RetrievalResult, ...]]
        ] = []

    def retrieve(
        self,
        question: str,
        k: int,
        min_score: float | None,
        *,
        request_id: str | None = None,
    ) -> tuple[RetrievalResult, ...]:
        self.questions.append(question)
        self.request_ids.append(request_id)
        if isinstance(self.result, Exception):
            raise self.result
        return tuple(
            RetrievalResult(
                KnowledgeChunk(
                    point_id=f"fake-{index}",
                    source_document=source.source_document,
                    section=source.section,
                    chunk_index=index,
                    text="Local fake context.",
                ),
                score=0.9,
                retrieval_kind=source.retrieval_kind,
            )
            for index, source in enumerate(self.result.sources)
        )

    def generate_answer(
        self, question: str, context: tuple[RetrievalResult, ...]
    ) -> str:
        self.generation_calls.append((question, context))
        assert not isinstance(self.result, Exception)
        return self.result.answer


def auth_user(role: Role) -> UserInDB:
    return UserInDB(
        id=str(uuid4()),
        email=f"{role.value}@example.com",
        hashed_password="hash",
        role=role,
    )


def app_with(
    role: Role | None,
    service: FakeRAGService,
    *,
    agent: CustomerSupportAgent | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def add_request_id(request, call_next):
        request.state.request_id = "550e8400-e29b-41d4-a716-446655440097"
        return await call_next(request)

    app.include_router(rag.router)
    if role is not None:
        app.dependency_overrides[get_current_user] = lambda: auth_user(role)
    app.dependency_overrides[rag.get_rag_service] = lambda: service
    if agent is not None:
        app.dependency_overrides[rag.get_customer_support_agent] = lambda: agent
    return app


def assert_correlated_trace(response, agent: CustomerSupportAgent):
    run_id = response.headers["X-TrackFlow-Run-ID"]
    assert UUID(run_id).version == 4
    trace = agent.tracer.get(run_id)
    assert trace is not None
    assert trace["run_id"] == run_id
    return trace


def test_query_requires_authentication_and_authorized_role():
    service = FakeRAGService(QueryResult("answer", (), False))
    agent = CustomerSupportAgent()
    missing = ASGIClient(app_with(None, service, agent=agent)).post(
        "/knowledge/query", json={"question": "Question?"}
    )
    invalid = ASGIClient(app_with(None, service, agent=agent)).post(
        "/knowledge/query",
        json={"question": "Question?"},
        headers={"Authorization": "Bearer invalid-token"},
    )
    forbidden = ASGIClient(app_with(Role.user, service, agent=agent)).post(
        "/knowledge/query", json={"question": "Question?"}
    )
    assert [missing.status_code, invalid.status_code, forbidden.status_code] == [
        401,
        401,
        403,
    ]
    assert all(
        "X-TrackFlow-Run-ID" not in response.headers
        for response in (missing, invalid, forbidden)
    )
    assert service.questions == []
    assert service.generation_calls == []
    assert agent.tracer.export() == []


def test_query_returns_answer_and_source_traceability_for_manager():
    service = FakeRAGService(
        QueryResult(
            "TrackFlow does not guarantee the SLA during Black Friday.",
            (SourceReference("sla-delivery", "high-demand-periods"),),
            False,
        )
    )
    agent = CustomerSupportAgent()
    response = ASGIClient(app_with(Role.manager, service, agent=agent)).post(
        "/knowledge/query", json={"question": "  Black Friday?  "}
    )
    assert response.status_code == 200
    trace = assert_correlated_trace(response, agent)
    assert trace["outcome"] == "answered"
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
    assert len(service.generation_calls) == 1


def test_query_supports_controlled_abstention_for_admin():
    service = FakeRAGService(
        QueryResult("There is not enough information.", (), True)
    )
    agent = CustomerSupportAgent()
    response = ASGIClient(app_with(Role.admin, service, agent=agent)).post(
        "/knowledge/query", json={"question": "Hazardous materials?"}
    )
    assert response.status_code == 200
    trace = assert_correlated_trace(response, agent)
    assert trace["outcome"] == "abstained"
    assert response.json()["insufficient_context"] is True
    assert response.json()["sources"] == []
    assert len(service.questions) == 1
    assert service.generation_calls == []


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
    agent = CustomerSupportAgent()
    response = ASGIClient(app_with(Role.admin, service, agent=agent)).post(
        "/knowledge/query", json={"question": "Storage price?"}
    )
    assert response.status_code == 503
    trace = assert_correlated_trace(response, agent)
    assert trace["status"] == "failed"
    assert trace["outcome"] == "technical_error"
    assert trace["error"] == {"category": category, "status_code": status_code}
    assert response.json() == {"detail": "knowledge service unavailable"}
    assert "secret" not in response.text
    assert len(service.questions) == 1
    assert service.generation_calls == []


def test_live_query_is_gated_until_threshold_is_calibrated(monkeypatch):
    monkeypatch.setenv("FOUR_GEEKS_API_KEY", "test-only-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.delenv("RAG_MIN_SCORE", raising=False)
    app = FastAPI()
    app.include_router(rag.router)
    agent = CustomerSupportAgent()
    app.dependency_overrides[get_current_user] = lambda: auth_user(Role.admin)
    app.dependency_overrides[rag.get_customer_support_agent] = lambda: agent

    response = ASGIClient(app).post(
        "/knowledge/query", json={"question": "Storage price?"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "knowledge service unavailable"}
    assert "X-TrackFlow-Run-ID" not in response.headers
    assert agent.tracer.export() == []


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
