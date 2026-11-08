"""Offline tests for TrackFlow RAG contracts and deterministic indexing."""

from __future__ import annotations

import json
import hashlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from pipelines.eval.rag.run_fidelity_review import build_review
from pipelines.eval.rag.run_independent_holdout import (
    DEFAULT_BINDING,
    SYSTEM_VERSION,
    calculate_metrics,
    load_evaluation_binding,
    validate_review_record,
)
from pipelines.eval.rag.validate_independent_holdout import validate_holdout
from pipelines.rag import SequentialEmbeddingProvider, recall_at_k
from services.rag.chunking import (
    DEFAULT_CORPUS_DIR,
    DOCUMENT_PLANS,
    deterministic_point_id,
    load_knowledge_chunks,
)
from services.rag.clients import FourGeeksHTTPClient, QdrantHTTPVectorStore
from services.rag.config import RAGSettings, load_settings
from services.rag.contracts import (
    COLLECTION_NAME,
    COMPANY,
    LANGUAGE,
    CollectionMismatchError,
    RAGConfigurationError,
    RAGProviderError,
    RetrievalResult,
    VectorPoint,
)
from services.rag.service import (
    INSUFFICIENT_CONTEXT_SENTINEL,
    INSUFFICIENT_INFORMATION_ANSWER,
    RAGService,
)


class FakeEmbeddingProvider:
    def __init__(self, vector=(0.1, 0.2, 0.3)):
        self.vector = vector
        self.calls: list[str] = []

    def embed(self, text: str):
        self.calls.append(text)
        return self.vector


class FakeGenerationProvider:
    def __init__(self, answer="Grounded answer"):
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, question: str, context: str) -> str:
        self.calls.append((question, context))
        return self.answer


class FakeVectorStore:
    def __init__(self):
        self.vector_size: int | None = None
        self.distance: str | None = None
        self.points: dict[str, VectorPoint] = {}
        self.search_results: list[tuple[str, float, Mapping[str, object]]] = []
        self.search_call: dict[str, object] | None = None

    def ensure_collection(self, collection_name: str, vector_size: int, distance: str):
        assert collection_name == COLLECTION_NAME
        if self.vector_size is not None and (
            self.vector_size != vector_size or self.distance != distance
        ):
            raise CollectionMismatchError("incompatible")
        self.vector_size = vector_size
        self.distance = distance

    def upsert(self, collection_name: str, points: Sequence[VectorPoint]):
        assert collection_name == COLLECTION_NAME
        self.points.update({point.point_id: point for point in points})

    def list_point_ids(
        self,
        collection_name: str,
        *,
        company: str,
        language: str,
        source_documents: Sequence[str],
    ):
        allowed = set(source_documents)
        return {
            point_id
            for point_id, point in self.points.items()
            if point.payload.get("company") == company
            and point.payload.get("language") == language
            and point.payload.get("source_document") in allowed
        }

    def delete_points(self, collection_name: str, point_ids: Sequence[str]):
        for point_id in point_ids:
            self.points.pop(point_id, None)

    def search(
        self,
        collection_name: str,
        vector: Sequence[float],
        *,
        k: int,
        min_score: float | None,
        company: str,
        language: str,
    ):
        self.search_call = {
            "collection": collection_name,
            "vector": tuple(vector),
            "k": k,
            "min_score": min_score,
            "company": company,
            "language": language,
        }
        return self.search_results[:k]


def _normalized(value: str) -> str:
    return " ".join(value.split())


def test_chunking_indexes_every_document_completely_with_contract_payloads():
    chunks = load_knowledge_chunks()
    by_source: dict[str, list] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source_document, []).append(chunk)
        assert chunk.payload() == {
            "company": "trackflow",
            "language": "en",
            "source_document": chunk.source_document,
            "section": chunk.section,
            "chunk_index": chunk.chunk_index,
            "text": chunk.text,
        }
    assert set(by_source) == {
        "sla-delivery",
        "returns-policy",
        "carrier-coverage",
        "storage-pricing",
    }
    assert all(len(items) >= 3 for items in by_source.values())

    for plan in DOCUMENT_PLANS:
        original = (DEFAULT_CORPUS_DIR / plan.filename).read_text(encoding="utf-8")
        body = "\n".join(original.replace("\r\n", "\n").splitlines()[1:]).strip()
        reconstructed = "\n".join(chunk.text for chunk in by_source[plan.source_document])
        assert _normalized(reconstructed) == _normalized(body)
        assert [chunk.chunk_index for chunk in by_source[plan.source_document]] == list(
            range(len(by_source[plan.source_document]))
        )


def test_chunk_ids_are_deterministic_and_change_with_text():
    first = load_knowledge_chunks()
    second = load_knowledge_chunks()
    assert [chunk.point_id for chunk in first] == [chunk.point_id for chunk in second]
    chunk = first[0]
    changed = (chunk.source_document, chunk.section, chunk.chunk_index, chunk.text + " changed")
    assert deterministic_point_id(changed) != chunk.point_id


def test_setup_is_idempotent_and_reconciles_stale_points():
    chunks = load_knowledge_chunks()
    store = FakeVectorStore()
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)
    first = service.setup(chunks)
    first_ids = set(store.points)
    second = service.setup(chunks)
    assert first.indexed == second.indexed == len(chunks)
    assert first.deleted_stale == second.deleted_stale == 0
    assert set(store.points) == first_ids

    stale_payload = dict(chunks[0].payload())
    store.points["stale-id"] = VectorPoint("stale-id", (0.1, 0.2, 0.3), stale_payload)
    reconciled = service.setup(chunks)
    assert reconciled.deleted_stale == 1
    assert "stale-id" not in store.points
    assert set(store.points) == first_ids


def test_setup_checks_verified_vector_size_before_creating_collection():
    store = FakeVectorStore()
    service = RAGService(
        FakeEmbeddingProvider(),
        FakeGenerationProvider(),
        store,
        expected_vector_size=1024,
    )
    with pytest.raises(RAGProviderError, match="unexpected vector size"):
        service.setup(load_knowledge_chunks())
    assert store.vector_size is None
    assert store.points == {}

def test_setup_never_recreates_an_incompatible_collection():
    store = FakeVectorStore()
    store.vector_size = 99
    store.distance = "Cosine"
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)
    with pytest.raises(CollectionMismatchError):
        service.setup(load_knowledge_chunks())


def test_retrieve_preserves_primary_threshold_and_expands_same_document_top_k():
    chunks = load_knowledge_chunks()
    store = FakeVectorStore()
    store.search_results = [
        (chunks[0].point_id, 0.91, chunks[0].payload()),
        (chunks[1].point_id, 0.79, chunks[1].payload()),
    ]
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)
    results = service.retrieve("monthly delivery compensation", k=3, min_score=0.8)
    assert len(results) == 2
    assert results[0].score == 0.91
    assert results[0].retrieval_kind == "primary"
    assert results[1].score == 0.79
    assert results[1].retrieval_kind == "complementary"
    assert store.search_call == {
        "collection": COLLECTION_NAME,
        "vector": (0.1, 0.2, 0.3),
        "k": 3,
        "min_score": None,
        "company": COMPANY,
        "language": LANGUAGE,
    }


def test_inactive_inventory_uses_related_top_k_context_without_lowering_threshold():
    chunks = load_knowledge_chunks()
    standard = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("storage-pricing", "standard-rates-and-grace-period")
    )
    long_term = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("storage-pricing", "long-term-inventory")
    )
    unrelated = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "high-demand-periods")
    )
    store = FakeVectorStore()
    store.search_results = [
        (standard.point_id, 0.6174929, standard.payload()),
        (long_term.point_id, 0.55981266, long_term.payload()),
        (unrelated.point_id, 0.4315516, unrelated.payload()),
    ]
    generator = FakeGenerationProvider(
        "The fee applies after more than 180 days of no movement "
        "(no sales or outbound), starting on day 181, at 150% over "
        "the standard rate."
    )
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("inactive inventory fees")

    assert result.insufficient_context is False
    assert [(source.section, source.retrieval_kind) for source in result.sources] == [
        ("standard-rates-and-grace-period", "primary"),
        ("long-term-inventory", "complementary"),
    ]
    context = generator.calls[0][1]
    assert '"retrieval_kind":"primary"' in context
    assert '"retrieval_kind":"complementary"' in context
    assert "more than 180 days of no movement" in context
    assert "no sales or outbound" in context
    assert "150% over the standard rate" in context
    assert "day 181" in context
    assert "high-demand-periods" not in context


def test_complementary_candidate_cannot_establish_document_relevance():
    long_term = next(
        chunk
        for chunk in load_knowledge_chunks()
        if (chunk.source_document, chunk.section)
        == ("storage-pricing", "long-term-inventory")
    )
    store = FakeVectorStore()
    store.search_results = [(long_term.point_id, 0.55981266, long_term.payload())]
    generator = FakeGenerationProvider()
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("unrelated question")

    assert result.insufficient_context is True
    assert result.sources == ()
    assert generator.calls == []


def test_retrieval_excludes_tangential_same_document_candidate():
    chunks = load_knowledge_chunks()
    compensation = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "monthly-sla-and-compensation")
    )
    high_demand = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "high-demand-periods")
    )
    commitments = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "delivery-commitments")
    )
    store = FakeVectorStore()
    store.search_results = [
        (compensation.point_id, 0.6930667, compensation.payload()),
        (high_demand.point_id, 0.37799862, high_demand.payload()),
        (commitments.point_id, 0.33510184, commitments.payload()),
    ]
    service = RAGService(
        FakeEmbeddingProvider(),
        FakeGenerationProvider(),
        store,
        default_min_score=0.57,
    )

    results = service.retrieve(
        "A country's on-time delivery rate falls below 90% for two consecutive "
        "months. What compensation applies?",
        min_score=0.57,
    )

    assert [(result.chunk.section, result.retrieval_kind) for result in results] == [
        ("monthly-sla-and-compensation", "primary"),
    ]


def test_lexical_evidence_recovers_carrier_policy_without_lowering_threshold():
    chunks = load_knowledge_chunks()
    spain = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("carrier-coverage", "spain-coverage")
    )
    selection = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("carrier-coverage", "selection-and-exceptions")
    )
    united_states = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("carrier-coverage", "united-states-coverage")
    )
    store = FakeVectorStore()
    store.search_results = [
        (spain.point_id, 0.5341613, spain.payload()),
        (selection.point_id, 0.48770076, selection.payload()),
        (united_states.point_id, 0.3592352, united_states.payload()),
    ]
    generator = FakeGenerationProvider(
        "SEUR has the best rural coverage in Aragón. An account manager may "
        "choose another carrier only as an exception approved by Carlos Vega."
    )
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query(
        "Which carrier handles rural deliveries in Aragón, and can an account "
        "manager choose another carrier without approval?"
    )

    assert result.insufficient_context is False
    assert [(source.section, source.retrieval_kind) for source in result.sources] == [
        ("spain-coverage", "primary"),
        ("selection-and-exceptions", "complementary"),
    ]
    context = generator.calls[0][1]
    assert "SEUR" in context
    assert "Carlos" in context and "Vega" in context
    assert "united-states-coverage" not in context


def test_retrieval_trace_exposes_raw_rank_kind_and_evidence_without_text_copy():
    chunks = load_knowledge_chunks()
    spain = next(chunk for chunk in chunks if chunk.section == "spain-coverage")
    selection = next(
        chunk for chunk in chunks if chunk.section == "selection-and-exceptions"
    )
    united_states = next(
        chunk for chunk in chunks if chunk.section == "united-states-coverage"
    )
    store = FakeVectorStore()
    store.search_results = [
        (spain.point_id, 0.5341613, spain.payload()),
        (selection.point_id, 0.48770076, selection.payload()),
        (united_states.point_id, 0.3592352, united_states.payload()),
    ]
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)

    trace = service.retrieve_with_trace(
        "Which carrier handles rural deliveries in Aragón, and can an account "
        "manager choose another carrier without approval?",
        min_score=0.57,
    )

    assert [candidate.chunk.section for candidate in trace.candidates] == [
        "spain-coverage",
        "selection-and-exceptions",
        "united-states-coverage",
    ]
    assert [candidate.retrieval_kind for candidate in trace.candidates] == [
        "primary",
        "complementary",
        "excluded",
    ]
    assert [candidate.evidence_basis for candidate in trace.candidates] == [
        "lexical",
        "lexical",
        "none",
    ]
    assert trace.selected[0].chunk.point_id == spain.point_id


def test_single_generic_lexical_overlap_cannot_override_threshold():
    commitments = next(
        chunk
        for chunk in load_knowledge_chunks()
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "delivery-commitments")
    )
    store = FakeVectorStore()
    store.search_results = [
        (commitments.point_id, 0.56, commitments.payload()),
    ]
    generator = FakeGenerationProvider()
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("Does TrackFlow deliver refrigerated medicines?")

    assert result.insufficient_context is True
    assert result.sources == ()
    assert generator.calls == []


def test_sunday_delivery_still_allows_generation_abstention_with_expansion():
    chunks = load_knowledge_chunks()
    high_demand = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "high-demand-periods")
    )
    commitments = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "delivery-commitments")
    )
    store = FakeVectorStore()
    store.search_results = [
        (high_demand.point_id, 0.596487, high_demand.payload()),
        (commitments.point_id, 0.55, commitments.payload()),
    ]
    generator = FakeGenerationProvider(INSUFFICIENT_CONTEXT_SENTINEL)
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("Does TrackFlow guarantee Sunday delivery?")

    assert result.insufficient_context is True
    assert result.sources == ()
    assert len(generator.calls) == 1


def test_cross_document_candidates_do_not_contaminate_black_friday_context():
    chunks = load_knowledge_chunks()
    high_demand = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("sla-delivery", "high-demand-periods")
    )
    returns = next(
        chunk
        for chunk in chunks
        if chunk.source_document == "returns-policy"
    )
    store = FakeVectorStore()
    store.search_results = [
        (high_demand.point_id, 0.749, high_demand.payload()),
        (returns.point_id, 0.56, returns.payload()),
    ]
    generator = FakeGenerationProvider("No SLA guarantee during Black Friday.")
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("Black Friday commitments")

    assert [(source.section, source.retrieval_kind) for source in result.sources] == [
        ("high-demand-periods", "primary")
    ]
    assert "returns-policy" not in generator.calls[0][1]


def test_international_returns_keeps_only_material_policy_context():
    chunks = load_knowledge_chunks()
    international = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("returns-policy", "international-returns")
    )
    standard = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("returns-policy", "standard-return-process")
    )
    store = FakeVectorStore()
    store.search_results = [
        (international.point_id, 0.72, international.payload()),
        (standard.point_id, 0.51, standard.payload()),
    ]
    generator = FakeGenerationProvider(
        "Cross-country returns require manual handling by Sofía Ramos's team."
    )
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query(
        "How is a purchase handled when it is returned in another country?"
    )

    assert [(source.section, source.retrieval_kind) for source in result.sources] == [
        ("international-returns", "primary")
    ]
    assert "standard-return-process" not in generator.calls[0][1]


def test_approval_query_excludes_unneeded_same_document_coverage():
    chunks = load_knowledge_chunks()
    exception = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("carrier-coverage", "selection-and-exceptions")
    )
    coverage = next(
        chunk
        for chunk in chunks
        if (chunk.source_document, chunk.section)
        == ("carrier-coverage", "united-states-coverage")
    )
    storage = next(
        chunk
        for chunk in chunks
        if chunk.source_document == "storage-pricing"
    )
    store = FakeVectorStore()
    store.search_results = [
        (exception.point_id, 0.7, exception.payload()),
        (coverage.point_id, 0.54, coverage.payload()),
        (storage.point_id, 0.53, storage.payload()),
    ]
    generator = FakeGenerationProvider("Carlos Vega must approve the exception.")
    service = RAGService(
        FakeEmbeddingProvider(),
        generator,
        store,
        default_min_score=0.57,
    )

    result = service.query("Who approves a carrier exception?")

    assert [source.retrieval_kind for source in result.sources] == ["primary"]
    assert "united-states-coverage" not in generator.calls[0][1]
    assert "storage-pricing" not in generator.calls[0][1]


def test_retrieve_logs_sanitized_candidates_below_threshold(caplog, monkeypatch):
    chunk = next(
        chunk
        for chunk in load_knowledge_chunks()
        if (chunk.source_document, chunk.section)
        == ("storage-pricing", "long-term-inventory")
    )
    store = FakeVectorStore()
    store.search_results = [(chunk.point_id, 0.56, chunk.payload())]
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)
    question = "private diagnostic wording"
    request_id = "550e8400-e29b-41d4-a716-446655440099"
    monkeypatch.setattr(logging.getLogger("rag.query"), "propagate", True)

    with caplog.at_level("INFO", logger="rag.query"):
        results = service.retrieve(
            question,
            min_score=0.57,
            request_id=request_id,
        )

    assert results == ()
    assert "storage-pricing" in caplog.text
    assert "long-term-inventory" in caplog.text
    assert '"score":0.56' in caplog.text
    assert f"requestId={request_id}" in caplog.text
    assert "stage=embedding_start" in caplog.text
    assert "stage=embedding_success" in caplog.text
    assert "stage=qdrant_start" in caplog.text
    assert "stage=qdrant_success" in caplog.text
    assert question not in caplog.text
    assert chunk.text not in caplog.text


def test_query_logs_sanitized_technical_error_stage(caplog, monkeypatch):
    class FailingEmbeddingProvider:
        def embed(self, text: str):
            raise RAGProviderError("secret provider body")

    question = "private failing question"
    request_id = "550e8400-e29b-41d4-a716-446655440098"
    service = RAGService(
        FailingEmbeddingProvider(),
        FakeGenerationProvider(),
        FakeVectorStore(),
    )
    monkeypatch.setattr(logging.getLogger("rag.query"), "propagate", True)

    with caplog.at_level("INFO", logger="rag.query"):
        with pytest.raises(RAGProviderError):
            service.query(question, request_id=request_id)

    assert f"requestId={request_id}" in caplog.text
    assert "outcome=technical_error" in caplog.text
    assert "stage=retrieval" in caplog.text
    assert "stage=embedding_error" in caplog.text
    assert "errorCategory=provider_error" in caplog.text
    assert question not in caplog.text
    assert "secret provider body" not in caplog.text


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
def test_generation_failure_logs_sanitized_category_and_status(
    category, status_code, caplog, monkeypatch
):
    chunk = load_knowledge_chunks()[0]
    store = FakeVectorStore()
    store.search_results = [(chunk.point_id, 0.9, chunk.payload())]
    question = "private generation question"
    request_id = "550e8400-e29b-41d4-a716-446655440095"

    class FailingGenerationProvider:
        def generate(self, question: str, context: str) -> str:
            raise RAGProviderError(
                "secret provider response body",
                category=category,
                status_code=status_code,
            )

    service = RAGService(
        FakeEmbeddingProvider(),
        FailingGenerationProvider(),
        store,
        default_min_score=0.57,
    )
    monkeypatch.setattr(logging.getLogger("rag.query"), "propagate", True)

    with caplog.at_level("INFO", logger="rag.query"):
        with pytest.raises(RAGProviderError):
            service.query(question, request_id=request_id)

    assert f"requestId={request_id}" in caplog.text
    assert "stage=generation_start" in caplog.text
    assert "stage=generation_error" in caplog.text
    assert f"errorCategory={category}" in caplog.text
    assert f"httpStatus={status_code}" in caplog.text
    assert "outcome=technical_error" in caplog.text
    assert "stage=generation" in caplog.text
    assert question not in caplog.text
    assert chunk.text not in caplog.text
    assert "secret provider response body" not in caplog.text


def test_successful_generation_logs_sanitized_substage(caplog, monkeypatch):
    chunk = load_knowledge_chunks()[0]
    store = FakeVectorStore()
    store.search_results = [(chunk.point_id, 0.9, chunk.payload())]
    question = "private successful question"
    service = RAGService(
        FakeEmbeddingProvider(),
        FakeGenerationProvider("safe answer"),
        store,
        default_min_score=0.57,
    )
    monkeypatch.setattr(logging.getLogger("rag.query"), "propagate", True)

    with caplog.at_level("INFO", logger="rag.query"):
        result = service.query(
            question,
            request_id="550e8400-e29b-41d4-a716-446655440094",
        )

    assert result.answer == "safe answer"
    assert "stage=generation_start" in caplog.text
    assert "stage=generation_success" in caplog.text
    assert "stage=generation_error" not in caplog.text
    assert question not in caplog.text
    assert chunk.text not in caplog.text


def test_query_abstains_without_calling_generation():
    generator = FakeGenerationProvider()
    service = RAGService(FakeEmbeddingProvider(), generator, FakeVectorStore())
    result = service.query("Does TrackFlow insure hazardous materials?")
    assert result.answer == INSUFFICIENT_INFORMATION_ANSWER
    assert result.insufficient_context is True
    assert result.sources == ()
    assert generator.calls == []


def test_generation_sentinel_becomes_controlled_abstention_without_sources():
    chunk = load_knowledge_chunks()[0]
    store = FakeVectorStore()
    store.search_results = [(chunk.point_id, 0.9, chunk.payload())]
    generator = FakeGenerationProvider(INSUFFICIENT_CONTEXT_SENTINEL)
    service = RAGService(FakeEmbeddingProvider(), generator, store)

    result = service.query("Question not answered by the retrieved chunk")

    assert result.answer == INSUFFICIENT_INFORMATION_ANSWER
    assert result.insufficient_context is True
    assert result.sources == ()


def test_retrieve_and_generation_are_separate_and_raw_chunks_are_not_the_answer():
    chunk = load_knowledge_chunks()[0]
    store = FakeVectorStore()
    store.search_results = [(chunk.point_id, 0.92, chunk.payload())]
    generator = FakeGenerationProvider("A concise generated answer")
    service = RAGService(FakeEmbeddingProvider(), generator, store)
    context = service.retrieve("question")
    assert generator.calls == []
    result = service.query("question")
    assert result.answer == "A concise generated answer"
    assert result.answer != chunk.text
    assert result.sources[0].source_document == chunk.source_document
    assert "untrusted reference data" in generator.calls[0][1]


def test_long_term_storage_generation_context_preserves_inactivity_condition():
    chunk = next(
        chunk
        for chunk in load_knowledge_chunks()
        if (chunk.source_document, chunk.section)
        == ("storage-pricing", "long-term-inventory")
    )
    generator = FakeGenerationProvider("Complete answer")
    service = RAGService(FakeEmbeddingProvider(), generator, FakeVectorStore())

    service.generate_answer(
        "When does the long-term inventory fee apply?",
        (RetrievalResult(chunk, 1.0),),
    )

    context = generator.calls[0][1]
    assert "more than 180 days of no movement" in context
    assert "no sales or outbound" in context
    assert "150% over the standard rate" in context
    assert "day 181" in context


def test_http_gateway_contracts_use_distinct_models_and_sanitize_failures():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert body["model"] == "embedding-model"
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1, 0.2]}],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        assert body["model"] == "generation-model"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "safe answer"}}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
            },
        )

    settings = RAGSettings(
        base_url="https://gateway.example/v1",
        api_key="super-secret-key",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant:6333",
    )
    client = FourGeeksHTTPClient(
        settings, httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.embed("hello") == [0.1, 0.2]
    assert client.embedding_request_count == 1
    assert client.last_embedding_status_code == 200
    assert client.embedding_usage == {"prompt_tokens": 3, "total_tokens": 3}
    assert client.generate("question", "context") == "safe answer"
    assert client.generation_request_count == 1
    assert client.last_generation_status_code == 200
    assert client.generation_usage == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
    }
    generation_body = json.loads(requests[1].content)
    system_prompt = generation_body["messages"][0]["content"]
    assert "every material qualifier" in system_prompt
    assert "eligibility and trigger conditions" in system_prompt
    assert "inactivity or movement requirements" in system_prompt
    assert "Answer the specific question directly" in system_prompt
    assert "reference marked complementary" in system_prompt
    assert "do not add unrelated policies" in system_prompt
    assert "Do not infer a fee base" in system_prompt
    assert "explicitly say they are exhaustive" in system_prompt
    assert "unambiguous Yes or No" in system_prompt
    assert [request.url.path for request in requests] == [
        "/v1/embeddings",
        "/v1/chat/completions",
    ]
    assert all(request.headers["authorization"] == "Bearer super-secret-key" for request in requests)

    failing = FourGeeksHTTPClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, text="super-secret-key upstream detail")
            )
        ),
    )
    with pytest.raises(RAGProviderError) as captured:
        failing.embed("hello")
    assert "super-secret-key" not in str(captured.value)
    assert "upstream detail" not in str(captured.value)
    assert captured.value.category == "http_error"
    assert captured.value.status_code == 401

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    timing_out = FourGeeksHTTPClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(timeout_handler)),
    )
    with pytest.raises(RAGProviderError) as timeout_error:
        timing_out.embed("hello")
    assert timeout_error.value.category == "timeout"
    assert timeout_error.value.status_code is None
    assert "secret timeout detail" not in str(timeout_error.value)


@pytest.mark.parametrize("status_code", [429, 500])
def test_generation_http_errors_preserve_only_sanitized_status(status_code):
    settings = RAGSettings(
        base_url="https://gateway.example/v1",
        api_key="super-secret-key",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant:6333",
        min_request_interval_seconds=0,
    )
    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status_code,
                    text="super-secret-key private upstream body",
                )
            )
        ),
    )

    with pytest.raises(RAGProviderError) as captured:
        client.generate("private question", "private context")

    assert captured.value.category == "http_error"
    assert captured.value.status_code == status_code
    assert "super-secret-key" not in str(captured.value)
    assert "private upstream body" not in str(captured.value)


@pytest.mark.parametrize(
    ("failure", "expected_category"),
    [
        (httpx.ReadTimeout("private timeout detail"), "timeout"),
        (httpx.ConnectError("private network detail"), "network_error"),
    ],
)
def test_generation_transport_errors_are_classified(failure, expected_category):
    settings = RAGSettings(
        base_url="https://gateway.example/v1",
        api_key="super-secret-key",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant:6333",
        min_request_interval_seconds=0,
    )

    def fail(request: httpx.Request) -> httpx.Response:
        failure.request = request
        raise failure

    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(transport=httpx.MockTransport(fail)),
    )

    with pytest.raises(RAGProviderError) as captured:
        client.generate("private question", "private context")

    assert captured.value.category == expected_category
    assert captured.value.status_code is None
    assert "private" not in str(captured.value)


def test_generation_invalid_response_is_a_validation_error():
    settings = RAGSettings(
        base_url="https://gateway.example/v1",
        api_key="super-secret-key",
        embedding_model="embedding-model",
        generation_model="generation-model",
        qdrant_url="http://qdrant:6333",
        min_request_interval_seconds=0,
    )
    client = FourGeeksHTTPClient(
        settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": None}}]},
                )
            )
        ),
    )

    with pytest.raises(RAGProviderError) as captured:
        client.generate("private question", "private context")

    assert captured.value.category == "validation_error"
    assert captured.value.status_code is None


def test_empty_generation_answer_is_a_validation_error():
    service = RAGService(
        FakeEmbeddingProvider(),
        FakeGenerationProvider("   "),
        FakeVectorStore(),
    )
    chunk = load_knowledge_chunks()[0]

    with pytest.raises(RAGProviderError) as captured:
        service.generate_answer("question", (RetrievalResult(chunk, 0.9),))

    assert captured.value.category == "validation_error"


def test_qdrant_failure_is_classified_and_logged_without_details(
    caplog, monkeypatch
):
    request_id = "550e8400-e29b-41d4-a716-446655440096"

    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret qdrant address", request=request)

    store = QdrantHTTPVectorStore(
        "http://qdrant:6333",
        httpx.Client(transport=httpx.MockTransport(network_failure)),
    )
    service = RAGService(FakeEmbeddingProvider(), FakeGenerationProvider(), store)
    monkeypatch.setattr(logging.getLogger("rag.query"), "propagate", True)

    with caplog.at_level("INFO", logger="rag.query"):
        with pytest.raises(RAGProviderError) as captured:
            service.retrieve("private qdrant query", request_id=request_id)

    assert captured.value.category == "network_error"
    assert "stage=embedding_success" in caplog.text
    assert "stage=qdrant_start" in caplog.text
    assert "stage=qdrant_error" in caplog.text
    assert "errorCategory=network_error" in caplog.text
    assert "private qdrant query" not in caplog.text
    assert "secret qdrant address" not in caplog.text


def test_sequential_embedding_provider_waits_between_requests_and_stops_on_failure():
    calls: list[str] = []
    sleeps: list[float] = []

    class Provider:
        def embed(self, text: str):
            calls.append(text)
            if text == "second":
                raise RAGProviderError("sanitized failure")
            return [0.1, 0.2]

    provider = SequentialEmbeddingProvider(
        Provider(),  # type: ignore[arg-type]
        delay_seconds=2.0,
        sleep=sleeps.append,
    )
    assert provider.embed("first") == [0.1, 0.2]
    with pytest.raises(RAGProviderError):
        provider.embed("second")
    assert calls == ["first", "second"]
    assert sleeps == [2.0]


def test_configuration_requires_distinct_embedding_and_generation_models():
    values = {
        "FOUR_GEEKS_API_KEY": "secret",
        "QDRANT_URL": "http://qdrant:6333",
        "RAG_EMBEDDING_MODEL": "same-model",
        "RAG_GENERATION_MODEL": "same-model",
    }
    with pytest.raises(RAGConfigurationError, match="must be different"):
        load_settings(values)


def test_configuration_leaves_threshold_unset_until_calibration_and_validates_it():
    values = {
        "FOUR_GEEKS_API_KEY": "secret",
        "QDRANT_URL": "http://qdrant:6333",
    }
    assert load_settings(values).min_score is None
    assert load_settings(values).expected_vector_size is None
    assert load_settings({**values, "RAG_MIN_SCORE": "0.75"}).min_score == 0.75
    assert load_settings({**values, "RAG_VECTOR_SIZE": "1024"}).expected_vector_size == 1024
    with pytest.raises(RAGConfigurationError, match="between 0 and 1"):
        load_settings({**values, "RAG_MIN_SCORE": "1.1"})


def test_qdrant_adapter_refuses_incompatible_collection_without_deleting_it():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "result": {
                    "config": {"params": {"vectors": {"size": 999, "distance": "Cosine"}}}
                }
            },
        )

    store = QdrantHTTPVectorStore(
        "http://qdrant:6333", httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(CollectionMismatchError):
        store.ensure_collection(COLLECTION_NAME, 1024, "Cosine")
    assert calls == [("GET", f"/collections/{COLLECTION_NAME}")]


def test_evaluation_sets_are_separate_and_recall_is_precomputed_only():
    path = Path("pipelines/eval/rag/test-queries.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["metric"] == {"name": "Recall@3", "target": 0.8, "status": "not_measured"}
    assert len(data["evaluation"]) == 10
    assert len(data["calibration"]) >= 1
    assert len(data["negative"]) >= 1
    assert {case["id"] for case in data["evaluation"]}.isdisjoint(
        {case["id"] for case in data["calibration"]}
    )
    expected_sections = {
        item["source_document"] for case in data["evaluation"] for item in case["expected"]
    }
    assert expected_sections == {
        "sla-delivery",
        "returns-policy",
        "carrier-coverage",
        "storage-pricing",
    }
    retrieved = {case["id"]: case["expected"] for case in data["evaluation"][:8]}
    assert recall_at_k(data["evaluation"], retrieved) == pytest.approx(0.8)


def test_real_retrieval_artifacts_match_the_frozen_input_and_measured_result():
    root = Path("pipelines/eval/rag")
    source_hash = hashlib.sha256((root / "test-queries.json").read_bytes()).hexdigest()
    calibration = json.loads((root / "calibration-results.json").read_text("utf-8"))
    evaluation = json.loads((root / "evaluation-results.json").read_text("utf-8"))
    negative = json.loads((root / "negative-results.json").read_text("utf-8"))

    assert {calibration["source_sha256"], evaluation["source_sha256"], negative["source_sha256"]} == {source_hash}
    assert (calibration["passed"], calibration["total"], calibration["threshold"]) == (3, 3, None)
    assert (evaluation["passed"], evaluation["total"], evaluation["threshold"]) == (9, 10, 0.57)
    assert evaluation["rate"] == pytest.approx(0.9)
    assert (negative["passed"], negative["total"], negative["threshold"]) == (2, 3, 0.57)
    assert sum(item["pass"] for item in evaluation["cases"]) == 9


def test_current_evaluation_plan_does_not_relabel_historical_evidence():
    root = Path("pipelines/eval/rag")
    plan = json.loads(
        (root / "current-system-evaluation-plan.json").read_text("utf-8")
    )

    assert plan["system_version"] == "rag-hybrid-v3"
    assert plan["status"] == "sealed_v2.1_pending_review_and_execution_authorization"
    assert plan["primary_retrieval"] == {"min_score": 0.57, "k": 3}
    assert plan["complementary_retrieval"]["maximum_per_established_document"] == 1
    assert plan["lexical_verification"] == {
        "semantic_candidate_limit": 3,
        "minimum_distinct_content_overlaps": 2,
        "requires_corpus_discriminative_overlap": True,
        "maximum_chunk_document_frequency": 2,
        "best_verified_candidate_can_establish_document": True,
    }
    assert plan["historical_baseline"]["recall_at_3"] == 0.9
    assert plan["historical_baseline"]["recomputed_for_current_version"] is False
    assert "current-version Recall@3 on fresh query embeddings" in plan[
        "metrics_not_available_without_external_calls"
    ]
    _, validated, _, _, _ = load_evaluation_binding(DEFAULT_BINDING)
    assert plan["prospective_holdout"]["input_sha256"] == validated["sha256"]
    assert plan["prospective_holdout"]["rubric_sha256"] == validated[
        "rubric_sha256"
    ]
    assert plan["prospective_holdout"]["executed"] is False
    assert plan["prospective_holdout"]["results_artifact"] is None
    assert plan["prospective_holdout"]["target_system_version"] == SYSTEM_VERSION
    assert plan["prospective_holdout"]["binding"] == DEFAULT_BINDING.name


def test_independent_review_record_is_required_and_hash_bound(tmp_path):
    validation = validate_holdout()
    record = {
        "target_system_version": SYSTEM_VERSION,
        "evaluation_version": validation["evaluation_version"],
        "holdout_sha256": validation["sha256"],
        "rubric_sha256": validation["rubric_sha256"],
        "approved": True,
        "reviewer": "External evaluator",
        "reviewer_independent_from_implementation": True,
        "reviewed_at": "2026-11-07T20:00:00Z",
        "approval_source": "signed external review record",
        "review_type": "strictly_independent",
        "independence_basis": "Reviewer did not participate in implementation.",
        "limitations": ["Single-reviewer assessment."],
        "checks": {
            "references_match_corpus": True,
            "ambiguity_reviewed": True,
            "duplicates_reviewed": True,
            "prior_use_reviewed": True,
            "rubric_is_material_and_complete": True,
        },
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    assert validate_review_record(path, validation)["approved"] is True
    record["reviewer_independent_from_implementation"] = False
    record["review_type"] = "prospective_with_independence_limitations"
    record["independence_limitation_acknowledged"] = True
    record.pop("independence_basis")
    path.write_text(json.dumps(record), encoding="utf-8")
    assert validate_review_record(path, validation)[
        "reviewer_independent_from_implementation"
    ] is False


def test_review_gate_rejects_unattributed_incomplete_or_false_independence(tmp_path):
    _, validation, _, _, _ = load_evaluation_binding(DEFAULT_BINDING)
    template_path = Path(
        "pipelines/eval/rag/independent-holdout-v2.1-review-template.json"
    )
    historical_path = Path(
        "pipelines/eval/rag/independent-holdout-v2.1-review.json"
    )

    with pytest.raises(ValueError, match="not approved"):
        validate_review_record(template_path, validation)
    with pytest.raises(ValueError, match="approval source"):
        validate_review_record(historical_path, validation)

    record = json.loads(template_path.read_text(encoding="utf-8"))
    record.update(
        {
            "approved": True,
            "reviewer": "Attributable project reviewer",
            "reviewed_at": "2026-11-07T22:30:00Z",
            "approval_source": "explicit user decision",
        }
    )
    path = tmp_path / "limited-review.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert validate_review_record(path, validation)["approved"] is True

    record["reviewer"] = None
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="attributable reviewer"):
        validate_review_record(path, validation)

    record["reviewer"] = "Attributable project reviewer"
    record["reviewer_independent_from_implementation"] = True
    record["review_type"] = "strictly_independent"
    record.pop("independence_limitation_acknowledged")
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="declared basis"):
        validate_review_record(path, validation)


def test_holdout_metrics_separate_raw_primary_selected_and_negative_behavior():
    cases = [
        {
            "status": "completed",
            "split": "positive",
            "raw_top3_hit": True,
            "raw_top3_complete_coverage": False,
            "raw_top3_reference_coverage": 1 / 3,
            "top3_precision": 1 / 3,
            "semantic_primary_hit": False,
            "lexical_primary_hit": True,
            "primary_hit": False,
            "complementary_hit": True,
            "selected_hit": True,
            "selected_complete_coverage": True,
            "selected_reference_coverage": 1.0,
        },
        {
            "status": "completed",
            "split": "positive",
            "raw_top3_hit": False,
            "raw_top3_complete_coverage": False,
            "raw_top3_reference_coverage": 0.0,
            "top3_precision": 0.0,
            "semantic_primary_hit": False,
            "lexical_primary_hit": False,
            "primary_hit": False,
            "complementary_hit": False,
            "selected_hit": False,
            "selected_complete_coverage": False,
            "selected_reference_coverage": 0.0,
        },
        {
            "status": "completed",
            "split": "negative",
            "final_abstention": True,
        },
        {
            "status": "completed",
            "split": "negative",
            "final_abstention": False,
        },
    ]

    metrics = calculate_metrics(cases)

    assert metrics["recall_at_3"] == pytest.approx(0.5)
    assert metrics["complete_raw_top3_reference_coverage_rate"] == 0.0
    assert metrics["mean_raw_top3_reference_coverage"] == pytest.approx(1 / 6)
    assert metrics["mean_top3_precision"] == pytest.approx(1 / 6)
    assert metrics["semantic_primary_expected_recovery"] == pytest.approx(0.0)
    assert metrics["lexical_primary_expected_recovery"] == pytest.approx(0.5)
    assert metrics["primary_expected_recovery"] == pytest.approx(0.0)
    assert metrics["complementary_expected_recovery"] == pytest.approx(0.5)
    assert metrics["combined_selected_recovery"] == pytest.approx(0.5)
    assert metrics["complete_selected_reference_coverage_rate"] == pytest.approx(0.5)
    assert metrics["mean_selected_reference_coverage"] == pytest.approx(0.5)
    assert metrics["negative_abstention_rate"] == pytest.approx(0.5)
    assert metrics["negative_false_positives"] == 1
    assert metrics["commercial_fidelity"]["automatic_score"] is None


def test_v21_binding_is_hash_bound_and_preserves_three_reference_case():
    binding, validation, holdout_path, rubric_path, output_path = (
        load_evaluation_binding(DEFAULT_BINDING)
    )
    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    case = next(item for item in holdout["positive"] if item["id"] == "ihv2-p12")

    assert validation["status"] == "valid"
    assert validation["positive"] == 12
    assert validation["negative"] == 4
    assert len(case["expected"]) == 3
    assert binding["external_request_budget"] == {
        "embedding_requests_maximum": 16,
        "generation_requests_maximum": 16,
        "minimum_interval_seconds": 2.0,
        "automatic_retries": 0,
    }
    assert rubric_path.name == "independent-holdout-v2.1-rubric.json"
    assert output_path.name == "independent-holdout-v2.1-results.json"


def test_generation_rubric_is_frozen_before_real_model_execution():
    data = json.loads(
        Path("pipelines/eval/rag/generation-cases.json").read_text(encoding="utf-8")
    )
    ids = [case["id"] for case in data["cases"]]
    assert len(ids) == len(set(ids)) == 11
    assert {
        "gen-high-demand",
        "gen-return-window-costs",
        "gen-international-returns",
        "gen-carrier-exception",
        "gen-storage-long-term",
        "gen-storage-preferential",
        "gen-negative-sunday",
    } <= set(ids)


def test_real_generation_and_manual_review_artifacts_are_consistent():
    root = Path("pipelines/eval/rag")
    result_path = root / "generation-results.json"
    results = json.loads(result_path.read_text(encoding="utf-8"))
    review = json.loads((root / "generation-review.json").read_text(encoding="utf-8"))

    assert results["status"] == "succeeded"
    assert results["completed"] == results["generation_requests"] == 11
    assert results["automatic_passed"] == 7
    assert review["generation_results_sha256"] == hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    assert review["manual_passed"] == review["total"] == 11
    assert all(case["pass"] for case in review["cases"])


def test_post_hoc_fidelity_review_accepts_equivalence_but_rejects_omission():
    review = build_review()
    cases = {case["id"]: case for case in review["cases"]}

    assert review["reviewed"] == 4
    assert review["passed"] == 3
    assert cases["gen-carrier-international-la"]["pass"] is True
    assert cases["gen-carrier-exception"]["pass"] is True
    assert cases["gen-storage-preferential"]["pass"] is True
    assert cases["gen-storage-long-term"]["pass"] is False
    assert cases["gen-storage-long-term"]["missing_requirements"] == [
        "no-movement eligibility"
    ]


def test_real_api_e2e_artifact_proves_auth_retrieval_and_generation():
    artifact = json.loads(
        Path("pipelines/eval/rag/api-e2e-result.json").read_text(encoding="utf-8")
    )
    assert artifact["manager_status"] == 200
    assert artifact["user_status"] == 403
    assert artifact["missing_token_status"] == 401
    assert artifact["invalid_token_status"] == 401
    assert artifact["valid_response"] is True
    assert artifact["embedding_requests"] == artifact["generation_requests"] == 1
    assert "does not guarantee" in artifact["answer"]
    assert artifact["sources"] == [
        {
            "source_document": "sla-delivery",
            "section": "high-demand-periods",
        }
    ]
