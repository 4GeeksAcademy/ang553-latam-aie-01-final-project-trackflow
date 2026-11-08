"""Deterministic, semantic chunking for TrackFlow's canonical knowledge base."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from services.rag.contracts import KnowledgeChunk, RAGConfigurationError


DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[2] / "docs" / "company-knowledge-base"
POINT_NAMESPACE = uuid.UUID("2c347582-9ac0-5cd7-8d6c-29dbb4515c9b")


@dataclass(frozen=True)
class DocumentPlan:
    filename: str
    source_document: str
    sections: tuple[str, ...]
    boundaries: tuple[str, ...]


DOCUMENT_PLANS = (
    DocumentPlan(
        "trackflow-sla-delivery.en.md",
        "sla-delivery",
        (
            "delivery-commitments",
            "monthly-sla-and-compensation",
            "high-demand-periods",
        ),
        (
            "The contractually committed SLA is 90%",
            "TrackFlow does not guarantee delivery SLA",
        ),
    ),
    DocumentPlan(
        "trackflow-returns-policy.en.md",
        "returns-policy",
        (
            "standard-return-process",
            "return-window-and-costs",
            "international-returns",
            "service-scope",
        ),
        (
            "Standard return window:",
            "International returns",
            "TrackFlow does not recondition or resell",
        ),
    ),
    DocumentPlan(
        "trackflow-carrier-coverage.en.md",
        "carrier-coverage",
        (
            "united-states-coverage",
            "spain-coverage",
            "selection-and-exceptions",
        ),
        (
            "Spain (Zaragoza):",
            "Carrier selection for each shipment",
        ),
    ),
    DocumentPlan(
        "trackflow-storage-pricing.en.md",
        "storage-pricing",
        (
            "standard-rates-and-grace-period",
            "long-term-inventory",
            "aging-reports",
            "preferential-rates",
        ),
        (
            '- Inventory with more than 180 days',
            "The client can request an inventory aging report",
            "Clients with more than 50 cubic meters",
        ),
    ),
)


def _body_without_heading(text: str, filename: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise RAGConfigurationError(f"{filename} must start with one H1 heading")
    return "\n".join(lines[1:]).strip()


def _semantic_sections(body: str, plan: DocumentPlan) -> list[str]:
    offsets = [0]
    previous = 0
    for boundary in plan.boundaries:
        offset = body.find(boundary, previous)
        if offset < 0 or body.find(boundary, offset + 1) >= 0:
            raise RAGConfigurationError(
                f"{plan.filename} does not match its approved semantic boundaries"
            )
        offsets.append(offset)
        previous = offset + len(boundary)
    offsets.append(len(body))
    chunks = [body[offsets[i] : offsets[i + 1]].strip() for i in range(len(offsets) - 1)]
    if len(chunks) != len(plan.sections) or any(not chunk for chunk in chunks):
        raise RAGConfigurationError(f"{plan.filename} produced invalid semantic chunks")
    return chunks


def deterministic_point_id(chunk: KnowledgeChunk | tuple[str, str, int, str]) -> str:
    if isinstance(chunk, KnowledgeChunk):
        values = (chunk.source_document, chunk.section, chunk.chunk_index, chunk.text)
    else:
        values = chunk
    source_document, section, chunk_index, text = values
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    name = f"{source_document}\0{section}\0{chunk_index}\0{digest}"
    return str(uuid.uuid5(POINT_NAMESPACE, name))


def load_knowledge_chunks(corpus_dir: Path = DEFAULT_CORPUS_DIR) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for plan in DOCUMENT_PLANS:
        path = corpus_dir / plan.filename
        if not path.is_file():
            raise RAGConfigurationError(f"Required knowledge document is missing: {plan.filename}")
        body = _body_without_heading(path.read_text(encoding="utf-8"), plan.filename)
        texts = _semantic_sections(body, plan)
        for index, (section, text) in enumerate(zip(plan.sections, texts, strict=True)):
            values = (plan.source_document, section, index, text)
            chunks.append(
                KnowledgeChunk(
                    point_id=deterministic_point_id(values),
                    source_document=plan.source_document,
                    section=section,
                    chunk_index=index,
                    text=text,
                )
            )
    return chunks
