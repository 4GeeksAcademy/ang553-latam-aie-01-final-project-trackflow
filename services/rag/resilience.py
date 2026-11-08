"""Deadline, retry, and process-local query-embedding cache primitives."""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.rag.contracts import RAGProviderError


RESILIENCE_VERSION = "rag-hybrid-v3.1-resilience"
_CACHE_REGISTRY: dict[tuple[int, float], "QueryEmbeddingCache"] = {}
_CACHE_REGISTRY_LOCK = threading.Lock()


class QueryDeadline:
    def __init__(
        self,
        budget_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if budget_seconds <= 0:
            raise ValueError("budget_seconds must be positive")
        self._clock = clock
        self._expires_at = clock() + budget_seconds

    def remaining(self) -> float:
        return max(0.0, self._expires_at - self._clock())

    def operation_timeout(self, cap: float, *, reserve: float = 0.0) -> float:
        available = self.remaining() - reserve
        if available <= 0:
            raise RAGProviderError(
                "RAG request deadline exhausted", category="deadline_exhausted"
            )
        return min(cap, available)


class QueryEmbeddingCache:
    """Bounded LRU/TTL cache with per-key single-flight computation."""

    def __init__(
        self,
        capacity: int,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 0 or ttl_seconds <= 0:
            raise ValueError("invalid cache bounds")
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, tuple[float, ...]]] = OrderedDict()
        self._flights: dict[str, threading.Condition] = {}
        self._lock = threading.RLock()

    def _purge_locked(self) -> None:
        now = self._clock()
        expired = [key for key, (expires, _) in self._entries.items() if expires <= now]
        for key in expired:
            self._entries.pop(key, None)

    def get_or_compute(
        self,
        key: str,
        compute: Callable[[], tuple[float, ...]],
        *,
        wait_seconds: Callable[[], float] | None = None,
    ) -> tuple[tuple[float, ...], str]:
        if self.capacity == 0:
            return compute(), "disabled"
        coalesced = False
        while True:
            with self._lock:
                self._purge_locked()
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached[1], "coalesced" if coalesced else "hit"
                flight = self._flights.get(key)
                if flight is None:
                    flight = threading.Condition(self._lock)
                    self._flights[key] = flight
                    break
                permitted_wait = None if wait_seconds is None else wait_seconds()
                if permitted_wait is not None and permitted_wait <= 0:
                    raise RAGProviderError(
                        "Embedding single-flight wait exceeded deadline",
                        category="deadline_exhausted",
                    )
                notified = flight.wait(timeout=permitted_wait)
                if permitted_wait is not None and not notified:
                    raise RAGProviderError(
                        "Embedding single-flight wait exceeded deadline",
                        category="deadline_exhausted",
                    )
                coalesced = True
        try:
            value = compute()
        except BaseException:
            with self._lock:
                self._flights.pop(key).notify_all()
            raise
        with self._lock:
            self._entries[key] = (self._clock() + self.ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
            self._flights.pop(key).notify_all()
        return value, "miss"

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._entries)


def process_query_embedding_cache(
    capacity: int, ttl_seconds: float
) -> QueryEmbeddingCache:
    """Return one process-local cache per configured capacity/TTL pair."""
    key = (capacity, ttl_seconds)
    with _CACHE_REGISTRY_LOCK:
        cache = _CACHE_REGISTRY.get(key)
        if cache is None:
            cache = QueryEmbeddingCache(capacity, ttl_seconds)
            _CACHE_REGISTRY[key] = cache
        return cache


def embedding_cache_key(
    query: str, *, model: str, dimension: int | None, isolation_scope: str
) -> str:
    # The API already performs conservative trim-only normalization. Preserve all
    # remaining bytes so semantically different questions never collapse.
    payload = json.dumps(
        [query, model, dimension, RESILIENCE_VERSION, isolation_scope],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class EmbeddingRetryPolicy:
    attempt_timeout_seconds: float
    downstream_reserve_seconds: float
    backoff_seconds: float
    max_backoff_seconds: float
    jitter_seconds: float
    retry_after_cap_seconds: float
    minimum_retry_timeout_seconds: float = 18.0
    maximum_attempts: int = 2


def _retryable(error: RAGProviderError) -> bool:
    if error.category in {"timeout", "network_error"}:
        return True
    return error.category == "http_error" and error.status_code in {
        429,
        500,
        502,
        503,
        504,
    }


class ResilientEmbeddingProvider:
    def __init__(
        self,
        provider: Any,
        *,
        deadline: QueryDeadline,
        cache: QueryEmbeddingCache,
        cache_key_context: Mapping[str, object],
        expected_dimension: int | None,
        policy: EmbeddingRetryPolicy,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self.deadline = deadline
        self.cache = cache
        self.cache_key_context = dict(cache_key_context)
        self.expected_dimension = expected_dimension
        self.policy = policy
        self.sleep = sleep
        self.random_source = random_source
        self.diagnostics: dict[str, object] = {"cache": "unchecked", "attempts": []}

    def _validated(self, text: str) -> tuple[float, ...]:
        attempts: list[dict[str, object]] = []
        self.diagnostics["attempts"] = attempts
        last_error: RAGProviderError | None = None
        for attempt in range(1, self.policy.maximum_attempts + 1):
            timeout = self.deadline.operation_timeout(
                self.policy.attempt_timeout_seconds,
                reserve=self.policy.downstream_reserve_seconds,
            )
            if timeout < self.policy.minimum_retry_timeout_seconds:
                raise RAGProviderError(
                    "Embedding attempt rejected by deadline",
                    category="deadline_exhausted",
                )
            started = time.monotonic()
            try:
                raw = self.provider.embed(text, timeout_seconds=timeout)
                vector = tuple(float(item) for item in raw)
                if (
                    not vector
                    or not all(math.isfinite(item) for item in vector)
                    or (
                        self.expected_dimension is not None
                        and len(vector) != self.expected_dimension
                    )
                ):
                    raise RAGProviderError(
                        "Embedding provider returned an invalid vector",
                        category="validation_error",
                    )
                attempts.append(
                    {
                        "attempt": attempt,
                        "result": "success",
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        "timeout_ms": round(timeout * 1000),
                    }
                )
                return vector
            except RAGProviderError as exc:
                last_error = exc
                event: dict[str, object] = {
                    "attempt": attempt,
                    "result": "failed",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "timeout_ms": round(timeout * 1000),
                    "category": exc.category,
                    "status_code": exc.status_code,
                    "retry": "not_scheduled",
                }
                attempts.append(event)
                if attempt >= self.policy.maximum_attempts or not _retryable(exc):
                    break
                if exc.retry_after_invalid:
                    event["retry"] = "rejected_invalid_retry_after"
                    raise RAGProviderError(
                        "Embedding retry delay is invalid",
                        category="retry_exhausted",
                        status_code=exc.status_code,
                    ) from exc
                exponential = min(
                    self.policy.max_backoff_seconds,
                    self.policy.backoff_seconds * (2 ** (attempt - 1)),
                )
                jitter = self.policy.jitter_seconds * self.random_source()
                backoff = exponential + jitter
                if exc.retry_after_seconds is not None:
                    if exc.retry_after_seconds > self.policy.retry_after_cap_seconds:
                        event["retry"] = "rejected_retry_after"
                        raise RAGProviderError(
                            "Embedding retry delay exceeds configured cap",
                            category="retry_exhausted",
                            status_code=exc.status_code,
                        ) from exc
                    backoff = max(
                        backoff,
                        exc.retry_after_seconds,
                    )
                retry_time = (
                    self.deadline.remaining()
                    - backoff
                    - self.policy.downstream_reserve_seconds
                )
                if retry_time < self.policy.minimum_retry_timeout_seconds:
                    event["retry"] = "rejected_by_deadline"
                    raise RAGProviderError(
                        "Embedding retry rejected by deadline",
                        category="deadline_exhausted",
                    ) from exc
                event["retry"] = "scheduled"
                event["backoff_ms"] = round(backoff * 1000)
                self.sleep(backoff)
        assert last_error is not None
        if _retryable(last_error):
            raise RAGProviderError(
                "Embedding retries exhausted",
                category="retry_exhausted",
                status_code=last_error.status_code,
            ) from last_error
        raise last_error

    def embed(self, text: str) -> Sequence[float]:
        self.diagnostics = {"cache": "unchecked", "attempts": []}
        initial_request_count = getattr(
            self.provider, "embedding_request_count", None
        )
        key = embedding_cache_key(
            text,
            model=str(self.cache_key_context["model"]),
            dimension=self.expected_dimension,
            isolation_scope=str(self.cache_key_context["isolation_scope"]),
        )
        try:
            vector, cache_result = self.cache.get_or_compute(
                key,
                lambda: self._validated(text),
                wait_seconds=lambda: max(
                    0.0,
                    self.deadline.remaining()
                    - self.policy.downstream_reserve_seconds,
                ),
            )
        except BaseException:
            self.diagnostics["cache"] = "miss_failed"
            current_request_count = getattr(
                self.provider, "embedding_request_count", None
            )
            self.diagnostics["external_calls"] = (
                current_request_count - initial_request_count
                if isinstance(initial_request_count, int)
                and isinstance(current_request_count, int)
                else len(self.diagnostics.get("attempts", []))
            )
            self.diagnostics["budget_remaining_ms"] = round(
                self.deadline.remaining() * 1000
            )
            raise
        self.diagnostics["cache"] = cache_result
        current_request_count = getattr(self.provider, "embedding_request_count", None)
        self.diagnostics["external_calls"] = (
            current_request_count - initial_request_count
            if isinstance(initial_request_count, int)
            and isinstance(current_request_count, int)
            else len(self.diagnostics.get("attempts", []))
        )
        self.diagnostics["budget_remaining_ms"] = round(
            self.deadline.remaining() * 1000
        )
        return vector

    def trace(self) -> Mapping[str, object]:
        return dict(self.diagnostics)


class BudgetedGenerationProvider:
    def __init__(
        self,
        provider: Any,
        deadline: QueryDeadline,
        timeout: float,
        response_reserve: float,
    ) -> None:
        self.provider = provider
        self.deadline = deadline
        self.timeout = timeout
        self.response_reserve = response_reserve
        self.diagnostics: dict[str, object] = {"external_calls": 0}

    def generate(self, question: str, context: str) -> str:
        assigned = self.deadline.operation_timeout(
            self.timeout, reserve=self.response_reserve
        )
        initial_request_count = getattr(
            self.provider, "generation_request_count", None
        )
        self.diagnostics = {
            "external_calls": 0,
            "timeout_ms": round(assigned * 1000),
            "budget_remaining_ms": round(self.deadline.remaining() * 1000),
        }
        try:
            return self.provider.generate(
                question, context, timeout_seconds=assigned
            )
        finally:
            current_request_count = getattr(
                self.provider, "generation_request_count", None
            )
            self.diagnostics["external_calls"] = (
                current_request_count - initial_request_count
                if isinstance(initial_request_count, int)
                and isinstance(current_request_count, int)
                else 1
            )

    def trace(self) -> Mapping[str, object]:
        return dict(self.diagnostics)


class BudgetedVectorStore:
    def __init__(
        self,
        store: Any,
        deadline: QueryDeadline,
        timeout: float,
        downstream_reserve: float,
    ) -> None:
        self.store = store
        self.deadline = deadline
        self.timeout = timeout
        self.downstream_reserve = downstream_reserve
        self.diagnostics: dict[str, object] = {"external_calls": 0}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def search(self, *args: Any, **kwargs: Any):
        assigned = self.deadline.operation_timeout(
            self.timeout, reserve=self.downstream_reserve
        )
        self.diagnostics = {
            "external_calls": 1,
            "timeout_ms": round(assigned * 1000),
            "budget_remaining_ms": round(self.deadline.remaining() * 1000),
        }
        return self.store.search(*args, **kwargs, timeout_seconds=assigned)

    def trace(self) -> Mapping[str, object]:
        return dict(self.diagnostics)
