"""Environment-backed RAG configuration without secret disclosure."""

from __future__ import annotations

import math
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
    total_budget_seconds: float = 80.0
    embedding_attempt_timeout_seconds: float = 25.0
    embedding_min_retry_timeout_seconds: float = 22.0
    generation_timeout_seconds: float = 20.0
    qdrant_timeout_seconds: float = 5.0
    response_reserve_seconds: float = 3.0
    retry_backoff_seconds: float = 0.5
    retry_max_backoff_seconds: float = 2.0
    retry_jitter_seconds: float = 0.25
    retry_after_cap_seconds: float = 5.0
    embedding_cache_capacity: int = 256
    embedding_cache_ttl_seconds: float = 600.0


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
    if not math.isfinite(timeout) or timeout <= 0:
        raise RAGConfigurationError("RAG_REQUEST_TIMEOUT_SECONDS must be positive")
    try:
        min_request_interval = float(
            source.get("RAG_REQUEST_INTERVAL_SECONDS", "2")
        )
    except ValueError as exc:
        raise RAGConfigurationError(
            "RAG_REQUEST_INTERVAL_SECONDS must be numeric"
        ) from exc
    if not math.isfinite(min_request_interval) or min_request_interval < 0:
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

    def positive_float(name: str, default: str) -> float:
        try:
            value = float(source.get(name, default))
        except ValueError as exc:
            raise RAGConfigurationError(f"{name} must be numeric") from exc
        if not math.isfinite(value) or value <= 0:
            raise RAGConfigurationError(f"{name} must be positive")
        return value

    def nonnegative_float(name: str, default: str) -> float:
        try:
            value = float(source.get(name, default))
        except ValueError as exc:
            raise RAGConfigurationError(f"{name} must be numeric") from exc
        if not math.isfinite(value) or value < 0:
            raise RAGConfigurationError(f"{name} must not be negative")
        return value

    try:
        cache_capacity = int(source.get("RAG_EMBEDDING_CACHE_CAPACITY", "256"))
    except ValueError as exc:
        raise RAGConfigurationError("RAG_EMBEDDING_CACHE_CAPACITY must be an integer") from exc
    if cache_capacity < 0:
        raise RAGConfigurationError("RAG_EMBEDDING_CACHE_CAPACITY must not be negative")

    total_budget = positive_float("RAG_TOTAL_BUDGET_SECONDS", "80")
    generation_timeout = positive_float("RAG_GENERATION_TIMEOUT_SECONDS", "20")
    qdrant_timeout = positive_float("RAG_QDRANT_TIMEOUT_SECONDS", "5")
    response_reserve = positive_float("RAG_RESPONSE_RESERVE_SECONDS", "3")
    embedding_attempt_timeout = positive_float(
        "RAG_EMBEDDING_ATTEMPT_TIMEOUT_SECONDS", "25"
    )
    embedding_min_retry_timeout = positive_float(
        "RAG_EMBEDDING_MIN_RETRY_TIMEOUT_SECONDS", "22"
    )
    if embedding_min_retry_timeout > embedding_attempt_timeout:
        raise RAGConfigurationError(
            "RAG_EMBEDDING_MIN_RETRY_TIMEOUT_SECONDS must not exceed "
            "RAG_EMBEDDING_ATTEMPT_TIMEOUT_SECONDS"
        )
    if (
        generation_timeout
        + qdrant_timeout
        + response_reserve
        + embedding_min_retry_timeout
        > total_budget
    ):
        raise RAGConfigurationError(
            "RAG stage reservations exceed the total query budget"
        )
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
        total_budget_seconds=total_budget,
        embedding_attempt_timeout_seconds=embedding_attempt_timeout,
        embedding_min_retry_timeout_seconds=embedding_min_retry_timeout,
        generation_timeout_seconds=generation_timeout,
        qdrant_timeout_seconds=qdrant_timeout,
        response_reserve_seconds=response_reserve,
        retry_backoff_seconds=nonnegative_float("RAG_RETRY_BACKOFF_SECONDS", "0.5"),
        retry_max_backoff_seconds=positive_float(
            "RAG_RETRY_MAX_BACKOFF_SECONDS", "2"
        ),
        retry_jitter_seconds=nonnegative_float("RAG_RETRY_JITTER_SECONDS", "0.25"),
        retry_after_cap_seconds=positive_float("RAG_RETRY_AFTER_CAP_SECONDS", "5"),
        embedding_cache_capacity=cache_capacity,
        embedding_cache_ttl_seconds=positive_float(
            "RAG_EMBEDDING_CACHE_TTL_SECONDS", "600"
        ),
    )
