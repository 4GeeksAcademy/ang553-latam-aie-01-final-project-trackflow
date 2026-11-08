"""Deterministic resilience tests; no gateway or Qdrant process is used."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from agents.customer_support import CustomerSupportAgent
from services.rag.clients import FourGeeksHTTPClient, _bounded_httpx_timeout
from services.rag.config import RAGSettings, load_settings
from services.rag.contracts import (
    KnowledgeChunk,
    RAGConfigurationError,
    RAGProviderError,
)
from services.rag.resilience import (
    BudgetedGenerationProvider,
    BudgetedVectorStore,
    EmbeddingRetryPolicy,
    QueryDeadline,
    QueryEmbeddingCache,
    ResilientEmbeddingProvider,
    embedding_cache_key,
)
from services.rag.service import RAGService


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class Provider:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls: list[float] = []

    def embed(self, _text: str, *, timeout_seconds: float):
        self.calls.append(timeout_seconds)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def policy(*, reserve: float = 10.0) -> EmbeddingRetryPolicy:
    return EmbeddingRetryPolicy(
        attempt_timeout_seconds=25,
        downstream_reserve_seconds=reserve,
        backoff_seconds=0.5,
        max_backoff_seconds=2,
        jitter_seconds=0.25,
        retry_after_cap_seconds=5,
        minimum_retry_timeout_seconds=22,
    )


def resilient(provider, clock, *, cache=None, model="embed-v1", budget=80, reserve=10):
    return ResilientEmbeddingProvider(
        provider,
        deadline=QueryDeadline(budget, clock=clock),
        cache=cache or QueryEmbeddingCache(8, 60, clock=clock),
        cache_key_context={"model": model, "isolation_scope": "trackflow:en"},
        expected_dimension=2,
        policy=policy(reserve=reserve),
        sleep=clock.sleep,
        random_source=lambda: 0.4,
    )


def error(category: str, status: int | None = None, retry_after: float | None = None):
    return RAGProviderError(
        "sanitized",
        category=category,
        status_code=status,
        retry_after_seconds=retry_after,
    )


def test_first_timeout_retries_once_and_caches_only_success():
    clock = Clock()
    provider = Provider([error("timeout"), (0.1, 0.2)])
    wrapped = resilient(provider, clock)

    assert tuple(wrapped.embed("exact question")) == (0.1, 0.2)
    assert len(provider.calls) == 2
    assert clock.value == pytest.approx(0.6)  # 0.5 exponential + 0.1 jitter
    assert wrapped.trace()["cache"] == "miss"
    assert wrapped.trace()["external_calls"] == 2
    assert tuple(wrapped.embed("exact question")) == (0.1, 0.2)
    assert len(provider.calls) == 2
    assert wrapped.trace()["cache"] == "hit"


def test_two_retryable_failures_become_retry_exhausted():
    clock = Clock()
    wrapped = resilient(
        Provider([error("network_error"), error("http_error", 503)]),
        clock,
    )

    with pytest.raises(RAGProviderError) as captured:
        wrapped.embed("question")
    assert captured.value.category == "retry_exhausted"
    assert wrapped.trace()["external_calls"] == 2
    assert wrapped.trace()["cache"] == "miss_failed"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_selected_5xx_statuses_retry(status):
    provider = Provider([error("http_error", status), (0.1, 0.2)])
    wrapped = resilient(provider, Clock())
    assert tuple(wrapped.embed("question")) == (0.1, 0.2)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_statuses_do_not_retry(status):
    provider = Provider([error("http_error", status)])
    wrapped = resilient(provider, Clock())
    with pytest.raises(RAGProviderError) as captured:
        wrapped.embed("question")
    assert captured.value.status_code == status
    assert len(provider.calls) == 1


@pytest.mark.parametrize("retry_after,expected", [(None, 0.6), (3.0, 3.0)])
def test_429_respects_bounded_retry_after(retry_after, expected):
    clock = Clock()
    wrapped = resilient(
        Provider([error("http_error", 429, retry_after), (0.1, 0.2)]), clock
    )
    assert tuple(wrapped.embed("question")) == (0.1, 0.2)
    assert clock.value == pytest.approx(expected)


def test_429_retry_after_above_cap_is_rejected():
    clock = Clock()
    provider = Provider([error("http_error", 429, 99.0)])
    wrapped = resilient(provider, clock)
    with pytest.raises(RAGProviderError) as captured:
        wrapped.embed("question")
    assert captured.value.category == "retry_exhausted"
    assert len(provider.calls) == 1
    assert wrapped.trace()["attempts"][0]["retry"] == "rejected_retry_after"


def test_retry_is_rejected_when_minimum_attempt_cannot_fit():
    clock = Clock()

    class SlowFailureProvider(Provider):
        def embed(self, text: str, *, timeout_seconds: float):
            clock.sleep(20)
            return super().embed(text, timeout_seconds=timeout_seconds)

    provider = SlowFailureProvider([error("timeout")])
    wrapped = resilient(provider, clock, budget=45, reserve=10)
    with pytest.raises(RAGProviderError) as captured:
        wrapped.embed("question")
    assert captured.value.category == "deadline_exhausted"
    assert len(provider.calls) == 1
    assert wrapped.trace()["attempts"][0]["retry"] == "rejected_by_deadline"


def test_first_attempt_is_not_started_without_a_useful_timeout():
    clock = Clock()
    provider = Provider([(0.1, 0.2)])
    wrapped = resilient(provider, clock, budget=27, reserve=10)

    with pytest.raises(RAGProviderError) as captured:
        wrapped.embed("question")

    assert captured.value.category == "deadline_exhausted"
    assert provider.calls == []


def test_cache_ttl_lru_and_model_identity_are_exact():
    clock = Clock()
    cache = QueryEmbeddingCache(2, 10, clock=clock)
    calls: list[str] = []

    def compute(value):
        calls.append(value)
        return (float(len(calls)),)

    assert cache.get_or_compute("a", lambda: compute("a"))[1] == "miss"
    assert cache.get_or_compute("a", lambda: compute("a"))[1] == "hit"
    cache.get_or_compute("b", lambda: compute("b"))
    cache.get_or_compute("c", lambda: compute("c"))
    assert cache.get_or_compute("a", lambda: compute("a"))[1] == "miss"
    clock.value += 10
    assert len(cache) == 0
    assert embedding_cache_key("Q", model="a", dimension=2, isolation_scope="x") != (
        embedding_cache_key("Q", model="b", dimension=2, isolation_scope="x")
    )
    assert embedding_cache_key("Q", model="a", dimension=2, isolation_scope="x") != (
        embedding_cache_key("q", model="a", dimension=2, isolation_scope="x")
    )


def test_invalid_vector_is_never_cached():
    clock = Clock()
    cache = QueryEmbeddingCache(2, 10, clock=clock)
    provider = Provider([(float("nan"), 0.2), (0.1, 0.2)])
    wrapped = resilient(provider, clock, cache=cache)
    with pytest.raises(RAGProviderError, match="invalid vector"):
        wrapped.embed("question")
    assert len(cache) == 0
    assert tuple(wrapped.embed("question")) == (0.1, 0.2)
    assert len(provider.calls) == 2


def test_cache_single_flight_for_concurrent_equal_queries():
    cache = QueryEmbeddingCache(2, 60)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[tuple[tuple[float, ...], str]] = []

    def compute():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return (0.1, 0.2)

    threads = [
        threading.Thread(
            target=lambda: results.append(cache.get_or_compute("k", compute))
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)
    assert calls == 1
    assert sorted(item[1] for item in results) == ["coalesced", "miss"]


def test_single_flight_waiter_can_refuse_unbounded_wait():
    cache = QueryEmbeddingCache(2, 60)
    entered = threading.Event()
    release = threading.Event()

    def compute():
        entered.set()
        assert release.wait(2)
        return (0.1,)

    producer = threading.Thread(target=lambda: cache.get_or_compute("k", compute))
    producer.start()
    assert entered.wait(2)
    with pytest.raises(RAGProviderError) as captured:
        cache.get_or_compute("k", compute, wait_seconds=lambda: 0)
    assert captured.value.category == "deadline_exhausted"
    release.set()
    producer.join(2)


def test_generation_and_qdrant_receive_deadline_bounded_timeouts():
    clock = Clock()
    deadline = QueryDeadline(40, clock=clock)

    class Generation:
        def generate(self, _question, _context, *, timeout_seconds):
            assert timeout_seconds == 20
            return "answer"

    class Store:
        def search(self, *_args, timeout_seconds, **_kwargs):
            assert timeout_seconds == 5
            return []

    generation = BudgetedGenerationProvider(Generation(), deadline, 20, 3)
    store = BudgetedVectorStore(Store(), deadline, 5, 23)
    assert store.search(
        "collection", (0.1,), k=3, min_score=None, company="x", language="en"
    ) == []
    assert generation.generate("q", "c") == "answer"


def test_deadline_prevents_qdrant_and_generation_from_starting():
    clock = Clock()
    deadline = QueryDeadline(2, clock=clock)

    class NeverCalled:
        def search(self, *_args, **_kwargs):
            raise AssertionError("Qdrant must not start")

        def generate(self, *_args, **_kwargs):
            raise AssertionError("generation must not start")

    store = BudgetedVectorStore(NeverCalled(), deadline, 5, 3)
    generation = BudgetedGenerationProvider(NeverCalled(), deadline, 20, 3)
    with pytest.raises(RAGProviderError, match="deadline"):
        store.search("collection", (0.1,))
    with pytest.raises(RAGProviderError, match="deadline"):
        generation.generate("q", "c")


def test_gateway_pacing_is_subtracted_from_operation_timeout(monkeypatch):
    moments = iter((1.0, 2.0))
    sleeps: list[float] = []
    received_timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_timeouts.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    settings = RAGSettings(
        base_url="https://gateway.invalid/v1",
        api_key="test-only",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant.invalid",
        min_request_interval_seconds=2,
    )
    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client._last_request_finished = 0.0
    monkeypatch.setattr("services.rag.clients.time.monotonic", lambda: next(moments))
    monkeypatch.setattr("services.rag.clients.time.sleep", sleeps.append)

    assert tuple(client.embed("question", timeout_seconds=5)) == (0.1, 0.2)
    assert sleeps == [1.0]
    assert len(received_timeouts) == 1
    assert sum(received_timeouts[0].values()) == pytest.approx(4.0)
    assert all(value > 0 for value in received_timeouts[0].values())


def test_gateway_phase_budget_preserves_observed_embedding_read_margin():
    timeout = _bounded_httpx_timeout(25)
    phases = timeout.as_dict()

    assert sum(phases.values()) == pytest.approx(25)
    assert phases["read"] > 16.68


def test_pacing_rejection_does_not_count_an_external_request(monkeypatch):
    settings = RAGSettings(
        base_url="https://gateway.invalid/v1",
        api_key="test-only",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant.invalid",
        min_request_interval_seconds=2,
    )
    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("socket must not be opened")
            )
        ),
    )
    client._last_request_finished = 0.0
    monkeypatch.setattr("services.rag.clients.time.monotonic", lambda: 0.0)

    with pytest.raises(RAGProviderError) as captured:
        client.embed("question", timeout_seconds=1)

    assert captured.value.category == "deadline_exhausted"
    assert client.embedding_request_count == 0


def test_gateway_parses_retry_after_without_exposing_provider_body():
    settings = RAGSettings(
        base_url="https://gateway.invalid/v1",
        api_key="test-only",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant.invalid",
    )
    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    429,
                    headers={"Retry-After": "3"},
                    text="private provider body",
                )
            )
        ),
    )

    with pytest.raises(RAGProviderError) as captured:
        client.embed("private question", timeout_seconds=5)

    assert captured.value.status_code == 429
    assert captured.value.retry_after_seconds == 3
    assert "private provider body" not in str(captured.value)


def test_resilience_configuration_rejects_impossible_attempt_window():
    values = {
        "FOUR_GEEKS_API_KEY": "test-only",
        "QDRANT_URL": "http://qdrant.invalid",
        "RAG_EMBEDDING_ATTEMPT_TIMEOUT_SECONDS": "10",
        "RAG_EMBEDDING_MIN_RETRY_TIMEOUT_SECONDS": "18",
    }

    with pytest.raises(RAGConfigurationError, match="must not exceed"):
        load_settings(values)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_resilience_configuration_rejects_non_finite_values(value):
    values = {
        "FOUR_GEEKS_API_KEY": "test-only",
        "QDRANT_URL": "http://qdrant.invalid",
        "RAG_TOTAL_BUDGET_SECONDS": value,
    }

    with pytest.raises(RAGConfigurationError):
        load_settings(values)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RAG_REQUEST_TIMEOUT_SECONDS", "inf"),
        ("RAG_REQUEST_INTERVAL_SECONDS", "nan"),
    ],
)
def test_legacy_gateway_timing_rejects_non_finite_values(name, value):
    values = {
        "FOUR_GEEKS_API_KEY": "test-only",
        "QDRANT_URL": "http://qdrant.invalid",
        name: value,
    }

    with pytest.raises(RAGConfigurationError):
        load_settings(values)


def test_resilience_configuration_rejects_impossible_total_budget():
    values = {
        "FOUR_GEEKS_API_KEY": "test-only",
        "QDRANT_URL": "http://qdrant.invalid",
        "RAG_TOTAL_BUDGET_SECONDS": "49",
    }

    with pytest.raises(RAGConfigurationError, match="exceed the total"):
        load_settings(values)


def test_fresh_cache_instance_models_process_restart():
    first = QueryEmbeddingCache(2, 60)
    first.get_or_compute("key", lambda: (0.1,))
    assert len(first) == 1
    assert len(QueryEmbeddingCache(2, 60)) == 0


def test_langgraph_retry_stays_inside_retrieval_and_trace_is_content_free():
    clock = Clock()
    deadline = QueryDeadline(80, clock=clock)
    cache = QueryEmbeddingCache(8, 60, clock=clock)
    embedding_backend = Provider([error("timeout"), (1.0, 0.0)])
    embedding = ResilientEmbeddingProvider(
        embedding_backend,
        deadline=deadline,
        cache=cache,
        cache_key_context={"model": "embed-v1", "isolation_scope": "trackflow:en"},
        expected_dimension=2,
        policy=policy(reserve=28),
        sleep=clock.sleep,
        random_source=lambda: 0,
    )
    chunk = KnowledgeChunk(
        point_id="point-1",
        source_document="sla-delivery",
        section="high-demand-periods",
        chunk_index=2,
        text="Local deterministic context.",
    )

    class Store:
        calls = 0

        def search(self, *_args, timeout_seconds, **_kwargs):
            self.calls += 1
            return [(chunk.point_id, 0.9, chunk.payload())]

    class Generation:
        calls = 0

        def generate(self, _question, _context, *, timeout_seconds):
            self.calls += 1
            return "Grounded deterministic answer."

    store_backend = Store()
    generation_backend = Generation()
    service = RAGService(
        embedding,
        BudgetedGenerationProvider(generation_backend, deadline, 20, 3),
        BudgetedVectorStore(store_backend, deadline, 5, 23),
        default_min_score=0.57,
        expected_vector_size=2,
    )
    agent = CustomerSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440042"

    execution = agent.execute("private exact question", service, run_id=run_id)

    assert execution.run_id == run_id
    assert len(embedding_backend.calls) == 2
    assert store_backend.calls == 1
    assert generation_backend.calls == 1
    trace = agent.tracer.get(run_id)
    assert trace is not None
    assert trace["outcome"] == "answered"
    assert trace["nodes"][0]["resilience"]["embedding"]["cache"] == "miss"
    serialized = json.dumps(trace)
    assert "private exact question" not in serialized
    assert "Local deterministic context" not in serialized
    assert "Grounded deterministic answer" not in serialized
    assert "[1.0, 0.0]" not in serialized


def test_langgraph_persistent_embedding_failure_records_retry_exhaustion():
    clock = Clock()
    embedding_backend = Provider(
        [error("network_error"), error("http_error", 503)]
    )
    embedding = ResilientEmbeddingProvider(
        embedding_backend,
        deadline=QueryDeadline(80, clock=clock),
        cache=QueryEmbeddingCache(8, 60, clock=clock),
        cache_key_context={"model": "embed-v1", "isolation_scope": "trackflow:en"},
        expected_dimension=2,
        policy=policy(reserve=28),
        sleep=clock.sleep,
        random_source=lambda: 0,
    )

    class NeverStore:
        def search(self, *_args, **_kwargs):
            raise AssertionError("Qdrant must not run after embedding exhaustion")

    class NeverGeneration:
        def generate(self, *_args, **_kwargs):
            raise AssertionError("generation must not run after embedding exhaustion")

    service = RAGService(
        embedding,
        NeverGeneration(),
        NeverStore(),
        default_min_score=0.57,
        expected_vector_size=2,
    )
    agent = CustomerSupportAgent()
    run_id = "550e8400-e29b-41d4-a716-446655440043"

    with pytest.raises(RAGProviderError) as captured:
        agent.execute("private persistent failure question", service, run_id=run_id)

    assert captured.value.category == "retry_exhausted"
    assert len(embedding_backend.calls) == 2
    trace = agent.tracer.get(run_id)
    assert trace is not None
    assert trace["status"] == "failed"
    assert trace["outcome"] == "retry_exhausted"
    assert trace["nodes"][0]["resilience"]["embedding"]["external_calls"] == 2
    serialized = json.dumps(trace)
    assert "private persistent failure question" not in serialized
    assert "sanitized" not in serialized
