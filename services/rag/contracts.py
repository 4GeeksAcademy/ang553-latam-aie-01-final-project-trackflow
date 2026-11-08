"""Provider-neutral data contracts for the TrackFlow RAG service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, Sequence


COLLECTION_NAME = "trackflow_knowledge"
COMPANY = "trackflow"
LANGUAGE = "en"


class RAGError(RuntimeError):
    """Base class for controlled RAG failures."""


class RAGConfigurationError(RAGError):
    """Raised when required runtime configuration is absent or invalid."""


class RAGProviderError(RAGError):
    """Sanitized failure from an embedding, generation, or vector provider."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        retry_after_invalid: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_invalid = retry_after_invalid


class CollectionMismatchError(RAGError):
    """Raised when an existing collection has an incompatible vector schema."""


@dataclass(frozen=True)
class KnowledgeChunk:
    point_id: str
    source_document: str
    section: str
    chunk_index: int
    text: str

    def payload(self) -> dict[str, object]:
        return {
            "company": COMPANY,
            "language": LANGUAGE,
            "source_document": self.source_document,
            "section": self.section,
            "chunk_index": self.chunk_index,
            "text": self.text,
        }


@dataclass(frozen=True)
class VectorPoint:
    point_id: str
    vector: tuple[float, ...]
    payload: Mapping[str, object]


@dataclass(frozen=True)
class RetrievalResult:
    chunk: KnowledgeChunk
    score: float
    retrieval_kind: Literal["primary", "complementary"] = "primary"


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    chunk: KnowledgeChunk
    score: float
    retrieval_kind: Literal["primary", "complementary", "excluded"]
    evidence_basis: Literal["semantic", "lexical", "none"]


@dataclass(frozen=True)
class RetrievalTrace:
    selected: tuple[RetrievalResult, ...]
    candidates: tuple[RetrievalCandidateTrace, ...]


@dataclass(frozen=True)
class SourceReference:
    source_document: str
    section: str
    retrieval_kind: Literal["primary", "complementary"] = "primary"


@dataclass(frozen=True)
class QueryResult:
    answer: str
    sources: tuple[SourceReference, ...]
    insufficient_context: bool


@dataclass(frozen=True)
class SetupResult:
    indexed: int
    deleted_stale: int
    vector_size: int


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> Sequence[float]: ...


class GenerationProvider(Protocol):
    def generate(self, question: str, context: str) -> str: ...


class VectorStore(Protocol):
    def ensure_collection(
        self, collection_name: str, vector_size: int, distance: str
    ) -> None: ...

    def upsert(self, collection_name: str, points: Sequence[VectorPoint]) -> None: ...

    def list_point_ids(
        self,
        collection_name: str,
        *,
        company: str,
        language: str,
        source_documents: Sequence[str],
    ) -> set[str]: ...

    def delete_points(self, collection_name: str, point_ids: Sequence[str]) -> None: ...

    def search(
        self,
        collection_name: str,
        vector: Sequence[float],
        *,
        k: int,
        min_score: float | None,
        company: str,
        language: str,
    ) -> Sequence[tuple[str, float, Mapping[str, object]]]: ...
