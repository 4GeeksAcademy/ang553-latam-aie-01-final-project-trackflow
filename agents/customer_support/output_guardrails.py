"""Context-aware output validation for TrackFlow Support.

The validators intentionally use structural/relational checks and narrow
high-confidence security signatures.  They do not scan for isolated words such
as ``system``, ``prompt`` or ``password`` and do not depend on transport types.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from agents.customer_support.guardrail_contracts import (
    FailureCategory,
    GuardrailReasonCode,
    OutputValidationResult,
    OutputValidationStatus,
)
from services.api.agent_memory.contracts import MemoryScope, ProposalStatus
from services.api.agent_memory.service import MemoryPolicyViolation, canonical_key_for
from services.rag.chunking import DOCUMENT_PLANS
from services.rag.contracts import QueryResult
from services.rag.knowledge_coverage import (
    INSUFFICIENT_KNOWLEDGE,
    INSUFFICIENT_KNOWLEDGE_EN,
    is_canonical_covered_answer,
)


SAFE_OUTPUT_MESSAGE = (
    "I could not safely return that support result. Please restate the TrackFlow "
    "request or try again."
)

_COMPONENT_STATUSES = frozenset({"ok", "not_found", "timeout", "unavailable"})
_ROUTES = frozenset(
    {"clarification", "knowledge", "ticket", "inventory", "tracking", "mixed"}
)
_MEMORY_STATUSES = frozenset(
    {
        "not_candidate",
        "scope_unresolved",
        "insufficient_budget",
        "provider_unavailable",
        "not_proposed",
        "proposed",
        "invalid",
        "timeout",
        "error",
        "policy_rejected",
    }
)
_ALLOWED_SOURCES = frozenset(
    (plan.source_document, section)
    for plan in DOCUMENT_PLANS
    for section in plan.sections
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_BOUNDARY = re.compile(
    r"(?is)</?\s*(?:system|developer|assistant|tool)(?:\s[^>]*)?>|"
    r"(?:^|\n)\s*(?:system|developer|assistant|tool)\s*:"
)
_PRIVILEGED_DIRECTIVE = re.compile(
    r"(?is)\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?\b|"
    r"\b(?:you are now|act as)\b.{0,80}\b(?:unrestricted|without rules?)\b|"
    r"\b(?:call|invoke|execute)\s+(?:a\s+)?(?:tool|function)\b|"
    r"\bfollow\s+(?:these|the following)\s+instructions?\b|"
    r"\b(?:reveal|show|print|repeat|dump)\b.{0,80}"
    r"\b(?:system prompt|developer message|hidden instructions?)\b"
)
_SECRET_VALUE = re.compile(
    r"(?is)\bBearer\s+[A-Za-z0-9._~-]{12,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*['\"]?"
    r"[A-Za-z0-9_./+~-]{8,}|"
    r"\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"
)
_CONFIDENTIAL_COMMERCIAL_VALUE = re.compile(
    r"(?is)\b(?:negotiated|contracted|confidential|private|client-specific|"
    r"negociad[ao]s?|contractual(?:es)?|confidencial(?:es)?|privad[ao]s?)\b"
    r".{0,100}\b(?:carrier rates?|rates?|tariffs?|pricing|discounts?|"
    r"commercial terms?|tarifas?|precios?|descuentos?|condiciones comerciales?)\b"
    r".{0,80}(?:\b\d+(?:[.,]\d+)?\b|\b(?:USD|EUR)\b|[$€%])|"
    r"\b(?:carrier rates?|rates?|tariffs?|pricing|discounts?|commercial terms?|"
    r"tarifas?|precios?|descuentos?|condiciones comerciales?)\b.{0,100}"
    r"\b(?:negotiated|contracted|confidential|private|client-specific|"
    r"negociad[ao]s?|contractual(?:es)?|confidencial(?:es)?|privad[ao]s?)\b"
    r".{0,80}(?:\b\d+(?:[.,]\d+)?\b|\b(?:USD|EUR)\b|[$€%])"
)
_PHYSICAL_SECURITY_VALUE = re.compile(
    r"(?is)\b(?:internal|private|secure|exact|intern[ao]s?|privad[ao]s?|"
    r"segur[ao]s?|exact[ao]s?)\b.{0,80}"
    r"\b(?:warehouse|depot|facility|almac[eé]n|centro log[ií]stico)\b.{0,80}"
    r"\b(?:address|location|coordinates?|route|path|aisle|rack|shelf|bin|"
    r"loading dock|security gate|access(?: code)?|direcci[oó]n|ubicaci[oó]n|"
    r"coordenadas?|ruta|recorrido|pasillo|estanter[ií]a|muelle|puerta|acceso)\b|"
    r"\b(?:aisle|rack|shelf|bin|loading dock|security gate|access code|pasillo|"
    r"estanter[ií]a|muelle|puerta de seguridad|c[oó]digo de acceso)\s+"
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}\b"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_UPPER_DOMAIN_VALUE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe() -> OutputValidationResult:
    return OutputValidationResult(status=OutputValidationStatus.safe)


def _blocked(
    failure: FailureCategory, reason: GuardrailReasonCode
) -> OutputValidationResult:
    return OutputValidationResult(
        status=OutputValidationStatus.blocked,
        failure_category=failure,
        reason_code=reason,
    )


def _structural_failure() -> OutputValidationResult:
    return _blocked(
        FailureCategory.structural,
        GuardrailReasonCode.invalid_external_structure,
    )


def _security_failure() -> OutputValidationResult:
    return _blocked(FailureCategory.security, GuardrailReasonCode.unsafe_output)


def _text_is_safe(value: object, *, minimum: int = 0, maximum: int) -> bool:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        return False
    if minimum and not value.strip():
        return False
    return not (
        _CONTROL_CHARACTERS.search(value)
        or _ROLE_BOUNDARY.search(value)
        or _PRIVILEGED_DIRECTIVE.search(value)
        or _SECRET_VALUE.search(value)
        or _CONFIDENTIAL_COMMERCIAL_VALUE.search(value)
        or _PHYSICAL_SECURITY_VALUE.search(value)
        or "insufficient_context" in value.casefold()
    )


def _validate_ticket(ticket: object | None) -> OutputValidationResult:
    if ticket is None:
        return _safe()
    status = _value(_field(ticket, "status"))
    if status not in _COMPONENT_STATUSES:
        return _structural_failure()
    fields = (
        "incident_id",
        "incident_status",
        "category",
        "version",
        "updated_at",
    )
    if status != "ok":
        return _safe() if all(_field(ticket, name) is None for name in fields) else _structural_failure()
    incident_id = _field(ticket, "incident_id")
    incident_status = _field(ticket, "incident_status")
    category = _field(ticket, "category")
    version = _field(ticket, "version")
    updated_at = _field(ticket, "updated_at")
    if not isinstance(incident_id, str) or not re.fullmatch(r"TRF-\d{6}", incident_id):
        return _structural_failure()
    if not isinstance(incident_status, str) or not _UPPER_DOMAIN_VALUE.fullmatch(incident_status):
        return _structural_failure()
    if not isinstance(category, str) or not _UPPER_DOMAIN_VALUE.fullmatch(category):
        return _structural_failure()
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return _structural_failure()
    if updated_at is None:
        return _structural_failure()
    if not _text_is_safe(incident_status, minimum=1, maximum=64) or not _text_is_safe(
        category, minimum=1, maximum=64
    ):
        return _security_failure()
    return _safe()


def _validate_inventory(inventory: object | None) -> OutputValidationResult:
    if inventory is None:
        return _safe()
    status = _value(_field(inventory, "status"))
    if status not in _COMPONENT_STATUSES:
        return _structural_failure()
    fields = ("product_id", "name", "sku", "warehouse", "current_stock")
    if status != "ok":
        return _safe() if all(_field(inventory, name) is None for name in fields) else _structural_failure()
    product_id = _field(inventory, "product_id")
    current_stock = _field(inventory, "current_stock")
    name = _field(inventory, "name")
    sku = _field(inventory, "sku")
    warehouse = _field(inventory, "warehouse")
    if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
        return _structural_failure()
    if isinstance(current_stock, bool) or not isinstance(current_stock, int):
        return _structural_failure()
    if warehouse not in {"LA", "ZGZ"}:
        return _structural_failure()
    if not isinstance(sku, str) or not _IDENTIFIER.fullmatch(sku):
        return _structural_failure()
    if not _text_is_safe(name, minimum=1, maximum=200) or not _text_is_safe(
        sku, minimum=1, maximum=128
    ):
        return _security_failure()
    return _safe()


def _validate_tracking(tracking: object | None) -> OutputValidationResult:
    if tracking is None:
        return _safe()
    status = _value(_field(tracking, "status"))
    if status not in _COMPONENT_STATUSES:
        return _structural_failure()
    fields = ("tracking_number", "dispatch_state", "commercial_order_id")
    if status != "ok":
        return (
            _safe()
            if all(_field(tracking, name) is None for name in fields)
            else _structural_failure()
        )
    tracking_number = _field(tracking, "tracking_number")
    dispatch_state = _value(_field(tracking, "dispatch_state"))
    commercial_order_id = _field(tracking, "commercial_order_id")
    if not isinstance(tracking_number, str) or not _IDENTIFIER.fullmatch(
        tracking_number
    ):
        return _structural_failure()
    if dispatch_state != "recorded_dispatch" or not isinstance(
        commercial_order_id, UUID
    ):
        return _structural_failure()
    return _safe()


def _validate_sources(sources: object) -> OutputValidationResult:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        return _structural_failure()
    if len(sources) > 12:
        return _structural_failure()
    seen: set[tuple[str, str]] = set()
    for source in sources:
        document = _field(source, "source_document")
        section = _field(source, "section")
        kind = _field(source, "retrieval_kind")
        if (document, section) not in _ALLOWED_SOURCES or kind not in {
            "primary",
            "complementary",
        }:
            return _structural_failure()
        if (document, section) in seen:
            return _structural_failure()
        seen.add((document, section))
    return _safe()


def _validate_knowledge(
    knowledge: object | None, status_value: object | None, *, http: bool
) -> OutputValidationResult:
    status = _value(status_value)
    if status is None:
        return _safe() if knowledge is None else _structural_failure()
    if status not in _COMPONENT_STATUSES:
        return _structural_failure()
    if status in {"timeout", "unavailable"}:
        if knowledge is None:
            return _safe()
        if not http:
            return _structural_failure()
        empty_wrapper = (
            _field(knowledge, "answer") is None
            and _field(knowledge, "coverage") is None
            and not bool(_field(knowledge, "insufficient_context", False))
            and _field(knowledge, "sources", []) == []
        )
        return _safe() if empty_wrapper else _structural_failure()
    if knowledge is None:
        return _structural_failure()
    answer = _field(knowledge, "answer")
    sources = _field(knowledge, "sources", ())
    insufficient = bool(_field(knowledge, "insufficient_context", False))
    coverage = _field(knowledge, "coverage")
    if not _text_is_safe(answer, minimum=1, maximum=12000):
        return _security_failure()
    source_result = _validate_sources(sources)
    if source_result.status is OutputValidationStatus.blocked:
        return source_result
    if status == "ok" and insufficient:
        return _structural_failure()
    if status == "not_found" and (not insufficient or len(sources) != 0):
        return _structural_failure()
    if coverage == "insufficient":
        if status != "not_found" or answer not in {INSUFFICIENT_KNOWLEDGE, INSUFFICIENT_KNOWLEDGE_EN}:
            return _structural_failure()
    elif coverage is not None:
        if coverage not in ("complete", "partial") or status != "ok" or not any(
            _field(source, "retrieval_kind") == "primary" for source in sources
        ):
            return _structural_failure()
        if not is_canonical_covered_answer(
            answer,
            [(_field(source, "source_document"), _field(source, "section")) for source in sources],
            partial=coverage == "partial",
        ):
            return _security_failure()
    return _safe()


def validate_knowledge_result(result: QueryResult) -> OutputValidationResult:
    """Check standalone Knowledge output before publication."""
    status = "not_found" if result.insufficient_context else "ok"
    return _validate_knowledge(result, status, http=False)


def validate_memory_proposal(
    proposal: object | None, *, conversation_id: object | None = None
) -> OutputValidationResult:
    if proposal is None:
        return _safe()
    status = _value(_field(proposal, "status", ProposalStatus.pending))
    if status != ProposalStatus.pending.value:
        return _structural_failure()
    proposal_conversation = _field(proposal, "conversation_id")
    proposal_id = _field(proposal, "id")
    if not isinstance(proposal_id, UUID) or not isinstance(proposal_conversation, UUID):
        return _structural_failure()
    if conversation_id is not None and proposal_conversation != conversation_id:
        return _structural_failure()
    scope = _value(_field(proposal, "scope"))
    client_id = _field(proposal, "client_id")
    if scope == MemoryScope.global_.value:
        if client_id is not None:
            return _structural_failure()
    elif scope == MemoryScope.client.value:
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 36:
            return _structural_failure()
    else:
        return _structural_failure()
    category = _field(proposal, "category")
    content = _field(proposal, "content")
    if not isinstance(content, dict):
        return _structural_failure()
    try:
        expected = canonical_key_for(category, content)
    except (MemoryPolicyViolation, KeyError, TypeError, ValueError):
        return _structural_failure()
    canonical = _field(proposal, "canonical_key", expected)
    if canonical != expected:
        return _structural_failure()
    serialized = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if not _text_is_safe(serialized, minimum=2, maximum=2000):
        return _security_failure()
    return _safe()


def _validate_memory_metadata(
    metadata: object | None, proposal: object | None
) -> OutputValidationResult:
    if metadata is None:
        return _safe()
    status = _field(metadata, "status")
    category = _field(metadata, "candidate_category")
    called = _field(metadata, "evaluator_called")
    if status not in _MEMORY_STATUSES or not isinstance(called, bool):
        return _structural_failure()
    if status == "not_candidate" and (category is not None or called):
        return _structural_failure()
    if status == "proposed" and (category is None or not called or proposal is None):
        return _structural_failure()
    # Consent edits return a replacement pending proposal while the query's
    # evaluator metadata legitimately remains ``not_candidate``.
    if proposal is not None and status not in {"proposed", "not_candidate"}:
        return _structural_failure()
    return _safe()


def _validate_consent(consent: object | None) -> OutputValidationResult:
    if consent is None:
        return _safe()
    action = _value(_field(consent, "action"))
    disposition = _value(_field(consent, "disposition"))
    continued = _field(consent, "continue_support_question")
    if not isinstance(continued, bool):
        return _structural_failure()
    valid = {
        "approve": {"approved"},
        "reject": {"rejected"},
        "edit": {"replacement_pending"},
        "ambiguous": {"discarded"},
        "topic_change": {"discarded"},
    }
    if action not in valid or disposition not in valid[action]:
        return _structural_failure()
    if action == "topic_change" and not continued:
        return _structural_failure()
    if action in {"ambiguous", "edit"} and continued:
        return _structural_failure()
    return _safe()


def _validate_result(value: object, *, http: bool) -> OutputValidationResult:
    answer = _field(value, "answer")
    route = _field(value, "route")
    partial = _field(value, "partial")
    needs_clarification = _field(value, "needs_clarification")
    if not _text_is_safe(answer, minimum=1, maximum=16000):
        return _security_failure()
    if route not in _ROUTES or not isinstance(partial, bool) or not isinstance(
        needs_clarification, bool
    ):
        return _structural_failure()
    ticket = _field(value, "ticket")
    inventory = _field(value, "inventory")
    tracking = _field(value, "tracking")
    knowledge = _field(value, "knowledge")
    knowledge_status = (
        _field(knowledge, "status") if http and knowledge is not None else _field(value, "knowledge_status")
    )
    for validation in (
        _validate_ticket(ticket),
        _validate_inventory(inventory),
        _validate_tracking(tracking),
        _validate_knowledge(knowledge, knowledge_status, http=http),
    ):
        if validation.status is OutputValidationStatus.blocked:
            return validation
    components = sum(
        component is not None for component in (ticket, inventory, tracking)
    ) + int(knowledge is not None or knowledge_status is not None)
    if route == "clarification" and components:
        return _structural_failure()
    if needs_clarification and route != "clarification":
        return _structural_failure()
    if route == "ticket" and ticket is None:
        return _structural_failure()
    if route == "inventory" and inventory is None:
        return _structural_failure()
    if route == "tracking" and tracking is None:
        return _structural_failure()
    if route == "knowledge" and knowledge is None:
        return _structural_failure()
    if route == "mixed" and components < 2:
        return _structural_failure()
    proposal = _field(value, "memory_proposal")
    conversation_id = _field(value, "conversation_id") if http else None
    if http and conversation_id is not None and not isinstance(conversation_id, UUID):
        return _structural_failure()
    proposal_validation = validate_memory_proposal(
        proposal, conversation_id=conversation_id
    )
    if proposal_validation.status is OutputValidationStatus.blocked:
        return proposal_validation
    metadata_validation = _validate_memory_metadata(
        _field(value, "memory_evaluation"), proposal
    )
    if metadata_validation.status is OutputValidationStatus.blocked:
        return metadata_validation
    if http:
        read_status = _field(value, "memory_read_status")
        if read_status not in {None, "skipped", "empty", "used", "unavailable"}:
            return _structural_failure()
        consent_validation = _validate_consent(_field(value, "memory_consent"))
        if consent_validation.status is OutputValidationStatus.blocked:
            return consent_validation
    return _safe()


def validate_external_result(value: object) -> OutputValidationResult:
    """Validate the graph result before memory self-evaluation."""

    return _validate_result(value, http=False)


def validate_http_response(value: object) -> OutputValidationResult:
    """Validate the complete response immediately before HTTP serialization."""

    return _validate_result(value, http=True)
