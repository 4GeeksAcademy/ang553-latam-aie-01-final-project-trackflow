"""Deterministic first-line input guardrails for TrackFlow Support.

The classifier intentionally handles only high-confidence cases.  Unknown
requests remain eligible for the existing Support router instead of being
treated as attacks.  Authenticated identity and tenant authorization are
enforced elsewhere and are never inferred from user text.
"""

from __future__ import annotations

import re

from agents.customer_support.guardrail_contracts import (
    GuardrailAction,
    GuardrailDecision,
    GuardrailInput,
    GuardrailReasonCode,
    InputCategory,
    JurisdictionProvenance,
)


_RESPONSES = {
    "en": {
        "instruction": (
            "I can’t follow requests to override safeguards or reveal protected "
            "instructions, prompts, credentials, or secrets. I can help with "
            "authorized TrackFlow logistics support."
        ),
        "commercial": (
            "I can’t provide confidential negotiated rates or client-specific "
            "commercial terms. I can help with published standard rates or explain "
            "the approval process for preferential rates."
        ),
        "physical": (
            "I can’t provide restricted facility layouts or access details, including "
            "non-public routing information. I can help with general warehouse codes "
            "and logistics coverage."
        ),
        "personal": (
            "I can’t complete personal tasks such as essays or homework. I can help "
            "with TrackFlow tracking, returns, incidents, and logistics support."
        ),
        "smalltalk": (
            "Hello! I’m the TrackFlow CX support assistant. I can help with tracking, "
            "returns, incidents, or authorized internal inventory."
        ),
        "general_logistics": (
            "Reverse logistics covers returns and product recovery, while last-mile "
            "logistics covers the final delivery stage. I can help with how TrackFlow "
            "handles tracking, returns, incidents, and documented logistics policies."
        ),
        "casual": (
            "Tokyo uses Japan Standard Time (JST), but I don’t provide live local-time "
            "data. I can help with TrackFlow tracking, returns, incidents, and logistics support."
        ),
        "identifier": "Please provide the relevant tracking or ticket identifier so I can help.",
        "jurisdiction": (
            "I cannot select a national policy until the order jurisdiction is "
            "verified from an authorized TrackFlow source."
        ),
    },
    "es": {
        "instruction": (
            "No puedo seguir solicitudes para anular las medidas de seguridad ni "
            "revelar instrucciones, prompts, credenciales o secretos protegidos. "
            "Puedo ayudarte con soporte logístico autorizado de TrackFlow."
        ),
        "commercial": (
            "No puedo proporcionar tarifas negociadas confidenciales ni condiciones "
            "comerciales específicas de un cliente. Puedo ayudarte con tarifas "
            "estándar publicadas o con el proceso de aprobación de tarifas preferenciales."
        ),
        "physical": (
            "No puedo proporcionar planos restringidos de instalaciones ni detalles "
            "de acceso, incluida información de circulación no pública. Puedo ayudarte "
            "con códigos generales de almacén y cobertura logística."
        ),
        "personal": (
            "No puedo realizar tareas personales como ensayos o deberes. Puedo "
            "ayudarte con tracking, devoluciones, incidencias y soporte logístico de TrackFlow."
        ),
        "smalltalk": (
            "¡Hola! Soy el asistente de soporte de TrackFlow del área de CX. Puedo "
            "ayudarte con tracking, devoluciones, incidencias o inventario interno autorizado."
        ),
        "general_logistics": (
            "La logística inversa gestiona devoluciones y recuperación de productos, "
            "mientras que la última milla cubre la etapa final de entrega. Puedo ayudarte "
            "con tracking, devoluciones, incidencias y políticas documentadas de TrackFlow."
        ),
        "casual": (
            "Tokio utiliza la hora estándar de Japón (JST), pero no proporciono la hora "
            "local en vivo. Puedo ayudarte con tracking, devoluciones, incidencias y soporte "
            "logístico de TrackFlow."
        ),
        "identifier": "Indica el identificador de tracking o de incidencia para poder ayudarte.",
        "jurisdiction": (
            "No puedo seleccionar una política nacional hasta verificar la jurisdicción "
            "del pedido mediante una fuente autorizada de TrackFlow."
        ),
    },
}

# Backward-compatible English constants for callers that import them directly.
SECURITY_REJECTION = _RESPONSES["en"]["instruction"]
PERSONAL_REJECTION = _RESPONSES["en"]["personal"]
SMALLTALK_REDIRECTION = _RESPONSES["en"]["smalltalk"]
IDENTIFIER_CLARIFICATION = _RESPONSES["en"]["identifier"]
JURISDICTION_CLARIFICATION = _RESPONSES["en"]["jurisdiction"]


_INSTRUCTION_OVERRIDE = re.compile(
    r"(?is)(?:\bignore|\bdisregard|\bforget|\boverride|\bbypass|\bolv[ií]date|"
    r"\bignora|\bdesobedece|\bsalta(?:te)?)\b.{0,80}"
    r"(?:instructions?|rules?|guardrails?|policy|policies|instrucciones?|reglas?|"
    r"pol[ií]ticas?|restricciones?)|"
    r"(?:act|behave|pretend|finge|act[uú]a|comp[oó]rtate)\b.{0,80}"
    r"(?:no rules?|without rules?|unrestricted|sin reglas?|sin restricciones?)"
)
_PROMPT_EXFILTRATION = re.compile(
    r"(?is)(?:reveal|show|print|repeat|display|dump|expose|muestra|revela|imprime|"
    r"repite|ens[eé][nñ]ame|exp[oó]n)\b.{0,80}"
    r"(?:system|developer|internal|hidden|sistema|desarrollador|intern[oa]|ocult[oa])?"
    r"\s*(?:prompt|message|instructions?|mensaje|instrucciones?)|"
    r"(?:what|cu[aá]l(?:es)?|dime)\b.{0,40}"
    r"(?:system prompt|developer message|prompt del sistema|mensaje del desarrollador)"
)
_SECRET_EXFILTRATION = re.compile(
    r"(?is)(?:reveal|show|print|dump|give me|muestra|revela|imprime|dame)\b.{0,80}"
    r"(?:api[ _-]?keys?|jwt|access[ _-]?tokens?|passwords?|credentials?|secrets?|"
    r"claves? api|contrase[nñ]as?|credenciales?|secretos?|tokens?)"
)
_CONFIDENTIAL_COMMERCIAL_REQUEST = re.compile(
    r"(?is)\b(?:reveal|show|print|list|give me|tell me|disclose|muestra|revela|"
    r"imprime|lista|dame|dime|divulga)\b.{0,100}"
    r"\b(?:negotiated|contracted|confidential|private|client-specific|custom|"
    r"negociad[ao]s?|contractual(?:es)?|confidencial(?:es)?|privad[ao]s?|"
    r"espec[ií]fic[ao]s? del cliente|personalizad[ao]s?)\b.{0,80}"
    r"\b(?:carrier rates?|rates?|tariffs?|pricing|discounts?|commercial terms?|"
    r"tarifas?|precios?|descuentos?|condiciones comerciales?)\b|"
    r"\b(?:negotiated|contracted|confidential|private|client-specific|"
    r"negociad[ao]s?|confidencial(?:es)?|privad[ao]s?)\s+"
    r"(?:carrier rates?|rates?|tariffs?|pricing|commercial terms?|tarifas?|"
    r"precios?|condiciones comerciales?)\b|"
    r"\b(?:reveal|show|print|list|give me|tell me|disclose|muestra|revela|"
    r"imprime|lista|dame|dime|divulga)\b.{0,100}"
    r"\b(?:carrier rates?|rates?|tariffs?|pricing|discounts?|commercial terms?|"
    r"tarifas?|precios?|descuentos?|condiciones comerciales?)\b.{0,80}"
    r"\b(?:negotiated|contracted|confidential|private|client-specific|custom|"
    r"negociad[ao]s?|contractual(?:es)?|confidencial(?:es)?|privad[ao]s?|"
    r"espec[ií]fic[ao]s? del cliente|personalizad[ao]s?)\b"
)
_PHYSICAL_SECURITY_REQUEST = re.compile(
    r"(?is)\b(?:reveal|show|print|list|give me|tell me|map|describe|muestra|"
    r"revela|imprime|lista|dame|dime|mu[eé]strame|mapea|describe)\b.{0,100}"
    r"\b(?:exact|precise|internal|private|secure|exact[ao]s?|precis[ao]s?|"
    r"intern[ao]s?|privad[ao]s?|segur[ao]s?)\b.{0,80}"
    r"\b(?:warehouse|depot|facility|almac[eé]n|centro log[ií]stico)\b.{0,80}"
    r"\b(?:address|location|coordinates?|route|path|aisle|rack|shelf|bin|"
    r"loading dock|security gate|access(?: code)?|direcci[oó]n|ubicaci[oó]n|"
    r"coordenadas?|ruta|recorrido|pasillo|estanter[ií]a|muelle|puerta|acceso)\b|"
    r"\b(?:internal warehouse route|warehouse route|ruta interna del almac[eé]n|"
    r"ruta del almac[eé]n)\b"
)
_PERSONAL_TASK = re.compile(
    r"(?is)(?:write|draft|compose|do|create|help me (?:write|with)|escribe|redacta|"
    r"haz|crea|ay[uú]dame (?:a escribir|con))\b.{0,80}"
    r"(?:essay|homework|assignment|poem|song|cover letter|personal email|recipe|"
    r"travel itinerary|c[oó]digo|programa|ensayo|tarea|poema|canci[oó]n|"
    r"carta de presentaci[oó]n|correo personal|receta|itinerario)"
)
_SMALLTALK = re.compile(
    r"(?is)^\s*(?:hi|hello|hey|good (?:morning|afternoon|evening)|hola|buenos d[ií]as|"
    r"buenas (?:tardes|noches)|how are you|c[oó]mo est[aá]s|thanks|thank you|"
    r"gracias)[!,.?\s]*$"
)
_INVENTORY = re.compile(
    r"(?is)\b(?:inventory|stock|units? available|product\s+(?:id\s*)?#?\d+|"
    r"inventario|existencias?|unidades disponibles|producto\s+(?:id\s*)?#?\d+)\b"
)
_INCOMPLETE_TRACKING = re.compile(
    r"(?is)\b(?:where is|track|status of|help with|d[oó]nde est[aá]|rastrear|"
    r"seguimiento|estado de|ayuda (?:con|para))\b.{0,50}"
    r"(?:my |mi |the |el )?(?:order|package|shipment|pedido|paquete|env[ií]o)\b"
)
_TRACKING_IDENTIFIER = re.compile(
    r"(?i)(?:#\s*)?\d{4,}|"
    r"\b(?=[A-Z0-9-]{6,}\b)(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b"
)
_GENERAL_LOGISTICS = re.compile(
    r"(?is)\b(?:what is|explain|how does|qu[eé] es|explica|c[oó]mo funciona)\b.{0,80}"
    r"(?:logistics|reverse logistics|last mile|log[ií]stica|log[ií]stica inversa|"
    r"[uú]ltima milla)\b"
)
_CASUAL_TRIVIA = re.compile(
    r"(?is)^\s*(?:what time is it in tokyo|what(?:'s| is) the time in tokyo|"
    r"qu[eé] hora es en tokio|qu[eé] hora hay en tokio)[?.!\s]*$"
)
_SUPPORT = re.compile(
    r"(?is)\b(?:returns?|refunds?|incidents?|tickets?|polic(?:y|ies)|procedures?|"
    r"tracking|delivery|carrier|devoluciones?|reembolsos?|incidencias?|casos?|"
    r"pol[ií]ticas?|procedimientos?|seguimiento|entrega|transportista)\b"
)
_POLICY_APPLICATION = re.compile(
    r"(?is)\b(?:apply|use|select|aplica|usa|usar|selecciona)\b.{0,80}"
    r"\b(?:policy|pol[ií]tica)\b|"
    r"\b(?:policy|pol[ií]tica)\b.{0,80}"
    r"\b(?:apply|use|select|aplica|usa|usar|selecciona)\b"
)
_REQUESTS_ES = re.compile(r"(?i)\b(?:Spain|Spanish|Espa[nñ]a|espa[nñ]ol[ae]?)\b")
_REQUESTS_US = re.compile(
    r"(?i)\b(?:USA|United States|American|Estados Unidos|estadounidense)\b"
)
_SPANISH_SIGNAL = re.compile(
    r"(?i)[áéíóúñ¿¡]|\b(?:hola|gracias|ignora|revela|muestra|mu[eé]strame|dime|"
    r"haz|escribe|ay[uú]dame|tarifa|almac[eé]n|ruta|ensayo|tarea|pol[ií]tica|"
    r"pedido|env[ií]o|incidencia|devoluciones|c[oó]mo|cu[aá]l|d[oó]nde)\b"
)


def _response_language(text: str) -> str:
    return "es" if _SPANISH_SIGNAL.search(text) else "en"


def _allowed(
    value: GuardrailInput,
    *,
    category: InputCategory = InputCategory.support,
    reason: GuardrailReasonCode = GuardrailReasonCode.support_request,
) -> GuardrailDecision:
    return GuardrailDecision(
        category=category,
        action=(
            GuardrailAction.allow_internal_inventory
            if category is InputCategory.internal_inventory
            else GuardrailAction.allow_support
        ),
        reason_code=reason,
        response_language=_response_language(value.user_text),
        effective_question=value.user_text,
    )


def classify_support_input(value: GuardrailInput) -> GuardrailDecision:
    """Return a closed decision for one normalized Support turn."""

    text = value.user_text
    response_language = _response_language(text)
    if _PROMPT_EXFILTRATION.search(text):
        return GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.prompt_exfiltration,
            response_language=response_language,
        )
    if _SECRET_EXFILTRATION.search(text):
        return GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.secret_exfiltration,
            response_language=response_language,
        )
    if _CONFIDENTIAL_COMMERCIAL_REQUEST.search(text):
        return GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.confidential_commercial_request,
            response_language=response_language,
        )
    if _PHYSICAL_SECURITY_REQUEST.search(text):
        return GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.physical_security_request,
            response_language=response_language,
        )
    if _INSTRUCTION_OVERRIDE.search(text):
        return GuardrailDecision(
            category=InputCategory.security,
            action=GuardrailAction.reject_security,
            reason_code=GuardrailReasonCode.instruction_override,
            response_language=response_language,
        )

    if _POLICY_APPLICATION.search(text):
        requested_country = (
            "ES"
            if _REQUESTS_ES.search(text) or re.search(r"\bES\b", text)
            else "US"
            if _REQUESTS_US.search(text) or re.search(r"\bUS\b", text)
            else None
        )
        if requested_country is not None:
            jurisdiction = value.trusted_jurisdiction
            if (
                jurisdiction is None
                or jurisdiction.provenance is JurisdictionProvenance.unavailable
            ):
                return GuardrailDecision(
                    category=InputCategory.incomplete,
                    action=GuardrailAction.clarify,
                    reason_code=GuardrailReasonCode.jurisdiction_unavailable,
                    response_language=response_language,
                )
            if requested_country != jurisdiction.applicable_country:
                return GuardrailDecision(
                    category=InputCategory.security,
                    action=GuardrailAction.reject_security,
                    reason_code=GuardrailReasonCode.jurisdiction_override,
                    response_language=response_language,
                )

    if _PERSONAL_TASK.search(text):
        return GuardrailDecision(
            category=InputCategory.personal,
            action=GuardrailAction.reject_personal,
            reason_code=GuardrailReasonCode.out_of_scope_personal,
            response_language=response_language,
        )
    if _SMALLTALK.fullmatch(text):
        return GuardrailDecision(
            category=InputCategory.smalltalk,
            action=GuardrailAction.redirect_smalltalk,
            reason_code=GuardrailReasonCode.smalltalk,
            response_language=response_language,
        )
    if _CASUAL_TRIVIA.fullmatch(text):
        return GuardrailDecision(
            category=InputCategory.smalltalk,
            action=GuardrailAction.redirect_smalltalk,
            reason_code=GuardrailReasonCode.casual_question,
            response_language=response_language,
        )
    if _INCOMPLETE_TRACKING.search(text) and not _TRACKING_IDENTIFIER.search(text):
        return GuardrailDecision(
            category=InputCategory.incomplete,
            action=GuardrailAction.clarify,
            reason_code=GuardrailReasonCode.missing_identifier,
            response_language=response_language,
        )
    if _INVENTORY.search(text):
        return _allowed(
            value,
            category=InputCategory.internal_inventory,
            reason=GuardrailReasonCode.internal_inventory_request,
        )
    if _SUPPORT.search(text):
        return _allowed(value)
    if _GENERAL_LOGISTICS.search(text):
        return GuardrailDecision(
            category=InputCategory.general_logistics,
            action=GuardrailAction.redirect_smalltalk,
            reason_code=GuardrailReasonCode.general_logistics_request,
            response_language=response_language,
        )
    return _allowed(value)


def safe_guardrail_response(decision: GuardrailDecision) -> str:
    """Map non-allowed decisions to constant, non-reflective responses."""

    messages = _RESPONSES[decision.response_language]
    if decision.reason_code is GuardrailReasonCode.confidential_commercial_request:
        return messages["commercial"]
    if decision.reason_code is GuardrailReasonCode.physical_security_request:
        return messages["physical"]
    if decision.action is GuardrailAction.reject_security:
        return messages["instruction"]
    if decision.action is GuardrailAction.reject_personal:
        return messages["personal"]
    if decision.reason_code is GuardrailReasonCode.general_logistics_request:
        return messages["general_logistics"]
    if decision.reason_code is GuardrailReasonCode.casual_question:
        return messages["casual"]
    if decision.action is GuardrailAction.redirect_smalltalk:
        return messages["smalltalk"]
    if decision.reason_code is GuardrailReasonCode.jurisdiction_unavailable:
        return messages["jurisdiction"]
    if decision.action is GuardrailAction.clarify:
        return messages["identifier"]
    raise ValueError("allowed decisions do not have a guardrail response")
