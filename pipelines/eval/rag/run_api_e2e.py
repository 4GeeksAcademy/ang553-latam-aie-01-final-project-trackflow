"""One authenticated live API E2E using isolated in-memory auth state."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from tinydb import TinyDB
from tinydb.storages import MemoryStorage


OUTPUT_PATH = Path(__file__).with_name("api-e2e-result.json")
QUESTION = "Does TrackFlow guarantee its delivery SLA during Black Friday?"


def _persist(value: dict[str, Any]) -> None:
    pending = OUTPUT_PATH.with_suffix(".json.pending")
    pending.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    pending.replace(OUTPUT_PATH)


async def _run() -> dict[str, Any]:
    os.environ.setdefault(
        "JWT_SECRET_KEY", "rag-e2e-isolated-test-secret-without-production-value"
    )
    from services.api import auth_security
    from services.api.auth_models import Role, UserInDB
    from services.api.auth_security import create_access_token
    from services.api.routes import rag
    from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
    from services.rag.config import load_settings
    from services.rag.service import RAGService

    settings = load_settings()
    if settings.min_score is None:
        raise RuntimeError("RAG_MIN_SCORE is required")

    isolated_users = TinyDB(storage=MemoryStorage)
    manager = UserInDB(
        email="rag-e2e-manager@trackflow.test",
        hashed_password="not-used",
        role=Role.manager,
    )
    regular_user = UserInDB(
        email="rag-e2e-user@trackflow.test",
        hashed_password="not-used",
        role=Role.user,
    )
    isolated_users.insert(manager.model_dump(mode="json"))
    isolated_users.insert(regular_user.model_dump(mode="json"))
    auth_security._users_db = isolated_users

    gateway = FourGeeksHTTPClient(settings)
    qdrant = QdrantHTTPVectorStore(
        settings.qdrant_url, timeout=settings.request_timeout_seconds
    )
    service = RAGService(
        gateway,
        gateway,
        qdrant,
        default_min_score=settings.min_score,
        expected_vector_size=settings.expected_vector_size,
    )
    app = FastAPI()
    app.include_router(rag.router)
    app.dependency_overrides[rag.get_rag_service] = lambda: service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            manager_response = await client.post(
                "/knowledge/query",
                json={"question": QUESTION},
                headers={"Authorization": f"Bearer {create_access_token(manager.id)}"},
            )
            user_response = await client.post(
                "/knowledge/query",
                json={"question": QUESTION},
                headers={"Authorization": f"Bearer {create_access_token(regular_user.id)}"},
            )
            missing_response = await client.post(
                "/knowledge/query", json={"question": QUESTION}
            )
            invalid_response = await client.post(
                "/knowledge/query",
                json={"question": QUESTION},
                headers={"Authorization": "Bearer invalid-token"},
            )
        body = manager_response.json() if manager_response.status_code == 200 else {}
        return {
            "question": QUESTION,
            "manager_status": manager_response.status_code,
            "user_status": user_response.status_code,
            "missing_token_status": missing_response.status_code,
            "invalid_token_status": invalid_response.status_code,
            "valid_response": (
                isinstance(body.get("answer"), str)
                and bool(body.get("answer", "").strip())
                and isinstance(body.get("sources"), list)
            ),
            "answer": body.get("answer"),
            "insufficient_context": body.get("insufficient_context"),
            "sources": body.get("sources"),
            "embedding_requests": gateway.embedding_request_count,
            "generation_requests": gateway.generation_request_count,
            "embedding_usage": gateway.embedding_usage or None,
            "generation_usage": gateway.generation_usage or None,
        }
    finally:
        gateway.close()
        qdrant.close()
        isolated_users.close()


def main() -> int:
    artifact = asyncio.run(_run())
    _persist(artifact)
    success = (
        artifact["manager_status"] == 200
        and artifact["user_status"] == 403
        and artifact["missing_token_status"] == 401
        and artifact["invalid_token_status"] == 401
        and artifact["valid_response"]
        and artifact["embedding_requests"] == 1
        and artifact["generation_requests"] == 1
    )
    print(
        json.dumps(
            {
                "status": "succeeded" if success else "failed",
                "manager_status": artifact["manager_status"],
                "user_status": artifact["user_status"],
                "missing_token_status": artifact["missing_token_status"],
                "invalid_token_status": artifact["invalid_token_status"],
                "valid_response": artifact["valid_response"],
                "embedding_requests": artifact["embedding_requests"],
                "generation_requests": artifact["generation_requests"],
                "embedding_usage": artifact["embedding_usage"],
                "generation_usage": artifact["generation_usage"],
                "output": str(OUTPUT_PATH),
            },
            separators=(",", ":"),
        )
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
