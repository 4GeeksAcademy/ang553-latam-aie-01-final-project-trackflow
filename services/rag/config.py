"""Environment-backed RAG configuration without secret disclosure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from services.rag.contracts import COLLECTION_NAME, RAGConfigurationError


DEFAULT_BASE_URL = "https://llm.4geeks.ai/v1"
DEFAULT_EMBEDDING_MODEL = (
    "online/openrouter/perplexity/pplx-embed-v1-0.6b"
)
DEFAULT_GENERATION_MODEL = (
    "online/openrouter/deepseek/deepseek-v4-flash"
)


@dataclass(frozen=True)
class RAGSettings:
    base_url: str
    api_key: str
    embedding_model: str
    generation_model: str
    qdrant_url: str
    collection_name: str = COLLECTION_NAME
    request_timeout_seconds: float = 30.0
    min_request_interval_seconds: float = 0.0
    min_score: float | None = None
    expected_vector_size: int | None = None


def load_settings(values: Mapping[str, str] | None = None) -> RAGSettings:
    source = os.environ if values is None else values
    api_key = source.get("FOUR_GEEKS_API_KEY", "").strip()
    qdrant_url = source.get("QDRANT_URL", "").strip()
    if not api_key:
        raise RAGConfigurationError("FOUR_GEEKS_API_KEY is required")
    if not qdrant_url:
        raise RAGConfigurationError("QDRANT_URL is required")
    try:
        timeout = float(source.get("RAG_REQUEST_TIMEOUT_SECONDS", "30"))
    except ValueError as exc:
        raise RAGConfigurationError("RAG_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if timeout <= 0:
        raise RAGConfigurationError("RAG_REQUEST_TIMEOUT_SECONDS must be positive")
    try:
        min_request_interval = float(
            source.get("RAG_REQUEST_INTERVAL_SECONDS", "2")
        )
    except ValueError as exc:
        raise RAGConfigurationError(
            "RAG_REQUEST_INTERVAL_SECONDS must be numeric"
        ) from exc
    if min_request_interval < 0:
        raise RAGConfigurationError(
            "RAG_REQUEST_INTERVAL_SECONDS must not be negative"
        )
    min_score_value = source.get("RAG_MIN_SCORE", "").strip()
    try:
        min_score = float(min_score_value) if min_score_value else None
    except ValueError as exc:
        raise RAGConfigurationError("RAG_MIN_SCORE must be numeric") from exc
    if min_score is not None and not 0 <= min_score <= 1:
        raise RAGConfigurationError("RAG_MIN_SCORE must be between 0 and 1")
    vector_size_value = source.get("RAG_VECTOR_SIZE", "").strip()
    try:
        expected_vector_size = int(vector_size_value) if vector_size_value else None
    except ValueError as exc:
        raise RAGConfigurationError("RAG_VECTOR_SIZE must be an integer") from exc
    if expected_vector_size is not None and expected_vector_size <= 0:
        raise RAGConfigurationError("RAG_VECTOR_SIZE must be positive")
    collection = source.get("QDRANT_COLLECTION", COLLECTION_NAME).strip()
    if collection != COLLECTION_NAME:
        raise RAGConfigurationError(f"QDRANT_COLLECTION must be {COLLECTION_NAME}")
    embedding_model = source.get("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    generation_model = source.get("RAG_GENERATION_MODEL", DEFAULT_GENERATION_MODEL).strip()
    if not embedding_model or not generation_model:
        raise RAGConfigurationError("Both RAG model identifiers are required")
    if embedding_model == generation_model:
        raise RAGConfigurationError("Embedding and generation models must be different")
    return RAGSettings(
        base_url=source.get("FOUR_GEEKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        api_key=api_key,
        embedding_model=embedding_model,
        generation_model=generation_model,
        qdrant_url=qdrant_url.rstrip("/"),
        collection_name=collection,
        request_timeout_seconds=timeout,
        min_request_interval_seconds=min_request_interval,
        min_score=min_score,
        expected_vector_size=expected_vector_size,
    )
