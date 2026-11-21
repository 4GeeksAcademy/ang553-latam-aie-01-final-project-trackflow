"""Minimal, deterministic access to the four canonical TrackFlow documents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from data.pipelines.rfp_response.contracts import EvidenceReference
from services.rag.chunking import load_knowledge_chunks
from services.rag.contracts import KnowledgeChunk


@dataclass(frozen=True)
class OfficialSource:
    chunk: KnowledgeChunk
    evidence: EvidenceReference


class OfficialKnowledgeCatalog:
    def __init__(self) -> None:
        chunks = load_knowledge_chunks()
        self._sources = {
            (chunk.source_document, chunk.section): OfficialSource(
                chunk=chunk,
                evidence=EvidenceReference(
                    document_id=chunk.source_document,
                    section_id=chunk.section,
                    evidence_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                ),
            )
            for chunk in chunks
        }

    def get(self, document_id: str, section_id: str) -> OfficialSource:
        try:
            return self._sources[(document_id, section_id)]
        except KeyError as exc:
            raise ValueError(f"source section is not in the approved catalog: {document_id}/{section_id}") from exc

    def matches(self, reference: EvidenceReference) -> bool:
        if reference.document_id == "rfp-intake-v1":
            return False
        try:
            source = self.get(reference.document_id, reference.section_id)
        except ValueError:
            return False
        return source.evidence == reference

    def source_text(self, reference: EvidenceReference) -> str | None:
        if not self.matches(reference):
            return None
        return self.get(reference.document_id, reference.section_id).chunk.text

    def for_department(self, department: str) -> tuple[OfficialSource, ...]:
        routes = {
            "warehouse": (("storage-pricing", "standard-rates-and-grace-period"),
                          ("storage-pricing", "long-term-inventory")),
            "lastmile": (("carrier-coverage", "united-states-coverage"),
                         ("carrier-coverage", "spain-coverage"),
                         ("carrier-coverage", "selection-and-exceptions"),
                         ("sla-delivery", "delivery-commitments")),
            "reverse": (("returns-policy", "standard-return-process"),
                        ("returns-policy", "return-window-and-costs"),
                        ("returns-policy", "international-returns"),
                        ("returns-policy", "service-scope")),
        }
        try:
            return tuple(self.get(*key) for key in routes[department])
        except KeyError as exc:
            raise ValueError(f"unsupported department: {department}") from exc

    def for_global_terms(self) -> tuple[OfficialSource, ...]:
        return (
            self.get("sla-delivery", "monthly-sla-and-compensation"),
            self.get("sla-delivery", "high-demand-periods"),
            self.get("storage-pricing", "preferential-rates"),
        )
