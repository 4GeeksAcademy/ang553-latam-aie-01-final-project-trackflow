"""Deterministic grounding checks shared by generation and preview safety."""

from __future__ import annotations

import re
from collections.abc import Sequence


_WORDS = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_MATERIAL_ATOMS = re.compile(
    r"(?i)(?<!\w)(?:\d+(?:[.,]\d+)?%?|usd|eur|dollars?|euros?|"
    r"ups|fedex|dhl|mrw|seur|hours?|horas?|days?|d[ií]as?|months?|meses?)(?!\w)")
_NEGATIONS = re.compile(r"(?i)(?<!\w)(?:not|no|never|without|sin|nunca)(?!\w)")
_COMMITMENT_ATOMS = re.compile(
    r"(?i)(?<!\w)(?:guarantee(?:d|s)?|promise(?:d|s)?|commit(?:ment|s|ted)?|"
    r"ensure(?:d|s)?|unlimited|always|free|garantiz\w*|promet\w*|compromet\w*|"
    r"ilimitad\w*|siempre|gratis)(?!\w)")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "client", "for", "from",
    "in", "is", "it", "of", "on", "or", "our", "the", "their", "this", "to",
    "trackflow", "we", "with", "y", "o", "el", "la", "los", "las", "de", "del",
    "en", "por", "para", "es", "son", "un", "una", "su", "sus", "que", "con",
}


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _WORDS.findall(value)
            if len(token) > 2 and token.casefold() not in _STOPWORDS}


def grounding_violation(text: str, supporting_statements: Sequence[str]) -> str | None:
    """Return a stable reason when one response segment is not conservatively grounded."""
    normalized = text.strip()
    if "\n" in normalized or len(re.findall(r"[.!?](?:\s|$)", normalized)) > 1:
        return "segment_not_single_sentence"
    support = " ".join(supporting_statements)
    response_tokens = _tokens(normalized)
    support_tokens = _tokens(support)
    if not response_tokens:
        return "segment_has_no_verifiable_content"
    if len(response_tokens & support_tokens) / len(response_tokens) < 0.70:
        return "segment_lexically_ungrounded"
    response_material = {item.casefold() for item in _MATERIAL_ATOMS.findall(normalized)}
    support_material = {item.casefold() for item in _MATERIAL_ATOMS.findall(support)}
    if not response_material <= support_material:
        return "segment_introduces_material_value"
    response_commitments = {
        item.casefold() for item in _COMMITMENT_ATOMS.findall(normalized)}
    support_commitments = {
        item.casefold() for item in _COMMITMENT_ATOMS.findall(support)}
    if not response_commitments <= support_commitments:
        return "segment_introduces_business_commitment"
    if bool(_NEGATIONS.search(normalized)) != bool(_NEGATIONS.search(support)):
        return "segment_changes_claim_polarity"
    return None
