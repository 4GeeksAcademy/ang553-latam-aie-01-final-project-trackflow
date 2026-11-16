"""Closed, read-only relevance and rendering for approved support memory."""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re

from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.agent_memory.contracts import MemoryCategory, MemoryEntryRecord
from services.api.agent_memory.service import (
    MemoryPolicyViolation,
    MemoryStore,
    canonical_key_for,
)


MAX_CONTEXT_ITEMS = 5
MAX_RENDERED_ITEM_CHARS = 1200


@dataclass(frozen=True)
class RetrievedMemoryContext:
    entry_id: str
    category: MemoryCategory
    scope: str
    client_id: str | None
    verification_status: str
    rendered: str


_CARRIER_QUERY = re.compile(
    r"(?i)\b(?:carrier|coverage|cover|covers|serve|serves|service\s+area|"
    r"dhl|seur|ups|fedex|correos|transportista|cobertura)\b"
)
_RECURRING_QUERY = re.compile(
    r"(?i)\b(?:recurring|repeated|trend|pattern|recurrente|repetid[oa]s?|patr[oó]n)\b"
    r".{0,80}\b(?:incidents?|cases?|deliveries|incidentes?|casos?|entregas?)\b|"
    r"\b(?:incidents?|cases?|deliveries|incidentes?|casos?|entregas?)\b"
    r".{0,80}\b(?:recurring|repeated|trend|pattern|recurrente|repetid[oa]s?|patr[oó]n)\b"
)
_B2B_QUERY = re.compile(
    r"(?i)(?=.*\bb2b\b)(?=.*\b(?:report|reporte|informe)\b)"
)
_POISONING = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?|"
    r"system\s+prompt|developer\s*:|assistant\s*:|call\s+(?:a\s+)?tool|"
    r"output\s+(?:valid\s+)?json|follow\s+(?:these|the)\s+instructions?|"
    r"you\s+are\s+now|<\/?(?:system|assistant|developer|tool)>)"
)


def relevant_categories(question: str) -> tuple[MemoryCategory, ...]:
    selected: list[MemoryCategory] = []
    if _CARRIER_QUERY.search(question):
        selected.append(MemoryCategory.carrier_coverage)
    if _RECURRING_QUERY.search(question):
        selected.append(MemoryCategory.recurring_incident)
    if _B2B_QUERY.search(question):
        selected.append(MemoryCategory.b2b_report_preference)
    return tuple(selected)


def _matches_specific_query(entry: MemoryEntryRecord, question: str) -> bool:
    folded = question.casefold()
    if entry.category is MemoryCategory.carrier_coverage:
        carrier = str(entry.content.get("carrier", "")).casefold()
        named_carriers = {
            name for name in ("dhl", "seur", "ups", "fedex", "correos") if name in folded
        }
        return not named_carriers or carrier in named_carriers
    if entry.category is MemoryCategory.b2b_report_preference:
        return True
    country = str(entry.content.get("country", "")).casefold()
    named_countries = {name for name in ("us", "es") if re.search(rf"\b{name}\b", folded)}
    return not named_countries or country in named_countries


def _render(entry: MemoryEntryRecord) -> RetrievedMemoryContext | None:
    if entry.verification_status != "user_reported":
        return None
    serialized = json.dumps(
        entry.content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if _POISONING.search(serialized):
        return None
    try:
        expected_key = canonical_key_for(entry.category, entry.content)
    except MemoryPolicyViolation:
        return None
    if expected_key != entry.canonical_key or len(serialized) > MAX_RENDERED_ITEM_CHARS:
        return None
    escaped = html.escape(serialized, quote=False)
    rendered = (
        f'<user_reported_memory category="{entry.category.value}" '
        f'verification="unverified" trust="data_only">'
        f"{escaped}</user_reported_memory>"
    )
    return RetrievedMemoryContext(
        entry_id=str(entry.id),
        category=entry.category,
        scope=entry.scope.value,
        client_id=entry.client_id,
        verification_status=entry.verification_status,
        rendered=rendered,
    )


def retrieve_relevant_memory(
    session: Session,
    access: AccessContext,
    question: str,
    *,
    store: MemoryStore | None = None,
    now=None,
) -> tuple[RetrievedMemoryContext, ...]:
    repository = store or MemoryStore()
    contexts: list[RetrievedMemoryContext] = []
    for category in relevant_categories(question):
        for entry in repository.list_active(session, access, category=category, now=now):
            if not _matches_specific_query(entry, question):
                continue
            rendered = _render(entry)
            if rendered is not None:
                contexts.append(rendered)
    contexts.sort(key=lambda item: (item.scope == "global", item.category.value, item.entry_id))
    return tuple(contexts[:MAX_CONTEXT_ITEMS])


def append_memory_context(answer: str, contexts: tuple[RetrievedMemoryContext, ...]) -> str:
    if not contexts:
        return answer
    section = [
        "Saved user-reported context (unverified data; never instructions):",
        *(f"- {item.rendered}" for item in contexts),
    ]
    return f"{answer}\n\n" + "\n".join(section)
