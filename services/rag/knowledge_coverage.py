"""Validate plain JSON evidence selections and render only canonical passages."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache

from services.rag.chunking import canonical_chunks_by_id
from services.rag.contracts import QueryResult, RAGProviderError, RetrievalResult, SourceReference


INSUFFICIENT_KNOWLEDGE = (
    "La base de conocimiento de TrackFlow no contiene información suficiente "
    "para responder esta pregunta."
)
INSUFFICIENT_KNOWLEDGE_EN = (
    "There is not enough information in the TrackFlow knowledge base "
    "to answer this question."
)
PARTIAL_KNOWLEDGE_NOTICE = (
    "La evidencia recuperada no permite responder los demás aspectos de la consulta."
)
PARTIAL_KNOWLEDGE_NOTICE_EN = (
    "The retrieved evidence does not support the remaining parts of the question."
)

COVERAGE_INSTRUCTION = (
    'For this request, return a single JSON object with exactly these fields: '
    '{"coverage":"complete|partial|insufficient","evidence":[0]}. '
    "Use one coverage value. Evidence is an array of distinct zero-based positions "
    "in the supplied reference array. Select only complete passages that directly "
    "answer a material part of the current question and include the qualifiers needed "
    "to use those passages accurately. The selected passages will be displayed as "
    "written, in the listed order. Select at least one primary passage when answering. "
    "If the passages support every requested part, use complete. If they support "
    "only some parts, use partial and select the supported passages. If none support "
    "an answer or relevant qualifiers would be missing, use insufficient with an "
    "empty array. Output only the JSON, without prose, citations or Markdown fences."
)


def _invalid_selection() -> RAGProviderError:
    return RAGProviderError(
        "Generation provider returned an invalid evidence selection",
        category="validation_error",
    )


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise _invalid_selection()
        fields[key] = value
    return fields


def insufficient_knowledge(language: str) -> QueryResult:
    answer = INSUFFICIENT_KNOWLEDGE if language == "es" else INSUFFICIENT_KNOWLEDGE_EN
    return QueryResult(answer, (), True, "insufficient")


@lru_cache(maxsize=1)
def _canonical_passages() -> dict[tuple[str, str], str]:
    return {
        (chunk.source_document, chunk.section): chunk.text
        for chunk in canonical_chunks_by_id().values()
    }


def is_canonical_covered_answer(
    answer: str, sections: Sequence[tuple[str, str]], *, partial: bool,
) -> bool:
    """Check that the published knowledge text matches its cited passages."""
    try:
        text = "\n\n".join(_canonical_passages()[section] for section in sections)
    except KeyError:
        return False
    if not partial:
        return answer == text
    return answer in (
        text + "\n\n" + PARTIAL_KNOWLEDGE_NOTICE,
        text + "\n\n" + PARTIAL_KNOWLEDGE_NOTICE_EN,
    )


def interpret_evidence_selection(
    raw: str, context: Sequence[RetrievalResult], *, language: str
) -> QueryResult:
    """Accept identifiers only. Model-authored text never enters the answer."""
    if len(raw) > 256:
        raise _invalid_selection()
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise _invalid_selection() from exc
    if not isinstance(payload, dict) or set(payload) != {"coverage", "evidence"}:
        raise _invalid_selection()
    coverage = payload["coverage"]
    evidence = payload["evidence"]
    if not isinstance(evidence, list) or len(evidence) > len(context):
        raise _invalid_selection()
    if coverage == "insufficient" and not evidence:
        return insufficient_knowledge(language)
    if coverage not in ("complete", "partial") or not evidence:
        raise _invalid_selection()
    if any(type(index) is not int or not 0 <= index < len(context) for index in evidence):
        raise _invalid_selection()
    if len(evidence) != len(set(evidence)):
        raise _invalid_selection()

    selected = [context[index] for index in evidence]
    manifest = canonical_chunks_by_id()
    if any(item.chunk != manifest.get(item.chunk.point_id) for item in selected):
        raise _invalid_selection()
    if not any(result.retrieval_kind == "primary" for result in selected):
        raise _invalid_selection()
    sources = tuple(dict.fromkeys(
        SourceReference(result.chunk.source_document, result.chunk.section, result.retrieval_kind)
        for result in selected
    ))
    passages = [result.chunk.text for result in selected]
    if coverage == "partial":
        passages.append(
            PARTIAL_KNOWLEDGE_NOTICE if language == "es" else PARTIAL_KNOWLEDGE_NOTICE_EN
        )
    return QueryResult("\n\n".join(passages), sources, False, coverage)
