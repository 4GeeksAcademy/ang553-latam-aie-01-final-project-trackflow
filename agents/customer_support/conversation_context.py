"""Resolve bounded knowledge references from completed chat data."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata

from agents.customer_support.guardrail_contracts import GuardrailAction, GuardrailInput
from agents.customer_support.input_guardrails import classify_support_input


# Only closed follow-up forms are eligible. Explicit qualifiers, coordinates,
# operational IDs and other clauses stay in the current question's normal path.
_TERM = r"(?!in\b|for\b|of\b|and\b|or\b|de\b|en\b|para\b|y\b|o\b)[a-z]{2,30}"
_REFERENCE = rf"(?P<reference>{_TERM}(?: {_TERM}){{0,2}})"
_SPANISH = re.compile(
    r"(?:y )?(?:(?:en que (?:casos|circunstancias|condiciones)|cuando|como|por que) "
    r"(?:(?:puede|podria|suele|se|variar|varia|cambiar|cambia|aplica|aplicarse|rige|extenderse) )*"
    r"|que (?:pasa|ocurre) con |cual es )?"
    rf"(?:ese|esa|este|esta|aquel|aquella) {_REFERENCE}"
)
_ENGLISH = re.compile(
    r"(?:and )?(?:what about |(?:when|why|how|under what (?:conditions|circumstances)) "
    r"(?:(?:can|could|does|would|might) )?)"
    rf"(?:that|this) {_REFERENCE}(?: (?:change|vary|apply))?"
)
_BARE_SPANISH = re.compile(r"(?:y )?(?:eso|esto|aquello|cuando|como|por que|que pasa con (?:eso|esto))")
_BARE_ENGLISH = re.compile(
    r"(?:and )?(?:what about (?:that|this|it)|(?:when|how|why)(?: (?:can|could|does|would) it(?: (?:change|vary|apply))?)?)"
)
_GENERIC_NOUNS = {"policy", "policies", "politica", "politicas", "process", "proceso"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(text.strip().strip("\u00bf?!.").split())


@dataclass(frozen=True)
class KnowledgeFollowup:
    clarification: str
    retrieval_question: str | None = None
    history_json: str | None = None


def _reference(question: str) -> tuple[str, tuple[str, ...]] | None:
    normalized = _normalize(question)
    for language, named, bare in (
        ("es", _SPANISH, _BARE_SPANISH),
        ("en", _ENGLISH, _BARE_ENGLISH),
    ):
        if bare.fullmatch(normalized):
            return language, ()
        match = named.fullmatch(normalized)
        if match:
            terms = re.sub(r" (?:change|vary|apply)$", "", match["reference"]).split()
            specific = tuple(term for term in terms if term not in _GENERIC_NOUNS)
            return language, specific or tuple(terms)
    return None


def _completed_pairs(history: str | None):
    """Parse only the bounded envelope emitted by the authorized chat store."""
    if not history or len(history) > 6000:
        return ()
    try:
        value = json.loads(history)
    except (ValueError, RecursionError):
        return ()
    if (
        not isinstance(value, dict)
        or value.get("trust") != "untrusted_data_only"
        or value.get("source") != "chat_history"
    ):
        return ()
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) > 8 or len(messages) % 2:
        return ()
    pairs = []
    for index in range(0, len(messages), 2):
        pair = messages[index:index + 2]
        if any(
            not isinstance(item, dict)
            or item.keys() != {"role", "status", "content"}
            or item.get("role") != role
            or item.get("status") != "completed"
            or not isinstance(item.get("content"), str)
            or not 0 < len(item["content"]) <= 1000
            for item, role in zip(pair, ("user", "assistant"))
        ):
            return ()
        pairs.append(pair)
    return pairs


def resolve_knowledge_followup(question: str, history: str | None) -> KnowledgeFollowup | None:
    """Bind one unambiguous noun reference without allowing history to route tools."""
    reference = _reference(question)
    if reference is None:
        return None
    language, terms = reference
    clarification = (
        "¿A qué te refieres? Indica el tema o la política que quieres consultar."
        if language == "es"
        else "What are you referring to? Please specify the topic or policy you want to ask about."
    )
    unresolved = KnowledgeFollowup(clarification)
    if not terms:
        return unresolved

    matches = {}
    for user, _assistant in reversed(_completed_pairs(history)):
        previous = user["content"]
        if _reference(previous) is not None:
            continue
        words = re.findall(r"[a-z]+", _normalize(previous))
        if not all(words.count(term) == 1 for term in terms):
            if not matches or all(term in words for term in terms):
                return unresolved
            continue
        # The store caps each field at 1000 characters. A possible truncation
        # cannot establish a referent or be forwarded as a complete question.
        if len(previous) == 1000:
            return unresolved
        try:
            user_input = classify_support_input(GuardrailInput(user_text=previous))
        except ValueError:
            return unresolved
        if user_input.action is not GuardrailAction.allow_support:
            return unresolved
        matches.setdefault(_normalize(previous), user)
    if len(matches) != 1:
        return unresolved
    user = next(iter(matches.values()))
    # Only the antecedent question is needed. Prior assistant prose can contain
    # user-reported Agent Memory or quoted instructions, so omit it entirely.
    return KnowledgeFollowup(
        clarification,
        user["content"],
        json.dumps(
            {"trust": "untrusted_data_only", "source": "chat_history", "messages": [user]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
