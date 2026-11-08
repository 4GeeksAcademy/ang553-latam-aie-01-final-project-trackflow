"""TrackFlow retrieval-augmented generation service contracts."""

from services.rag.chunking import DEFAULT_CORPUS_DIR, load_knowledge_chunks
from services.rag.contracts import (
    CollectionMismatchError,
    KnowledgeChunk,
    QueryResult,
    RAGConfigurationError,
    RAGProviderError,
    RetrievalResult,
    SetupResult,
    SourceReference,
)
from services.rag.service import INSUFFICIENT_INFORMATION_ANSWER, RAGService

__all__ = [
    "CollectionMismatchError",
    "DEFAULT_CORPUS_DIR",
    "INSUFFICIENT_INFORMATION_ANSWER",
    "KnowledgeChunk",
    "QueryResult",
    "RAGConfigurationError",
    "RAGProviderError",
    "RAGService",
    "RetrievalResult",
    "SetupResult",
    "SourceReference",
    "load_knowledge_chunks",
]
