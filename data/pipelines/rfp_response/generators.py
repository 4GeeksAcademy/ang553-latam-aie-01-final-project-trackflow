"""Grounded departmental and global commercial-term generators."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol, Sequence
from uuid import UUID, uuid4

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    AspectResponse, CommercialTerm, DepartmentDraft, EvidenceReference, GlobalCommercialTermsDraft,
    GlobalCurrency, GlobalSLA, MissingCommercialInput, RFPResponseStart,
    SupportedClaim, VolumeDiscountRow,
)
from data.pipelines.rfp_response.sources import OfficialKnowledgeCatalog


def _claim(statement: str, evidence: EvidenceReference) -> SupportedClaim:
    return SupportedClaim(statement=statement, evidence=[evidence])


def _localized(language: str, english: str, spanish: str) -> str:
    return spanish if language == "es" else english


class DepartmentContentProvider(Protocol):
    """Provider seam; receives approved claims, never complete source documents."""

    def render(self, *, language: str, summary_for_sales: str,
               key_aspects: Sequence[str], claims: Sequence[str],
               pending_codes: Sequence[str], feedback: Sequence[str]) -> str: ...


class DeterministicContentProvider:
    """Offline provider used by QA; it does not represent FourGeeks validation."""

    def render(self, *, language: str, summary_for_sales: str,
               key_aspects: Sequence[str], claims: Sequence[str],
               pending_codes: Sequence[str], feedback: Sequence[str]) -> str:
        lines = list(claims)
        prefix = "Dato comercial pendiente" if language == "es" else "Pending commercial input"
        lines.extend(f"{prefix}: {code}." for code in pending_codes)
        return "\n".join(lines)


def _input_evidence(request: RFPResponseStart, field: str) -> EvidenceReference:
    return EvidenceReference(document_id="rfp-intake-v1", section_id=field,
                             evidence_hash=request.source_handoff_hash)


def _carrier_confirmation_evidence(request: RFPResponseStart) -> EvidenceReference:
    return EvidenceReference(
        document_id="admin-confirmation", section_id="carrier_model",
        evidence_hash=request.carrier_confirmation_hash or request.source_handoff_hash,
    )


_SENSITIVE_CLIENT_COMMERCIAL = re.compile(
    r"(?i)\b(negotiated|negociad[ao]|confidential|confidencial|"
    r"UPS|FedEx|DHL|MRW|SEUR)\b.*\b(rate|price|tariff|cost|tarifa|precio|coste)\b"
)
_READABILITY_FEEDBACK = "Shorten sentences and use clearer language before regenerating the section."


def _safe_aspect_context(fact: str, requested_action: str) -> str:
    combined = f"{fact} | {requested_action}"
    return "[sensitive commercial request withheld]" if _SENSITIVE_CLIENT_COMMERCIAL.search(combined) else combined


def _select_answer_claim(department: DepartmentId, fact: str, requested_action: str,
                         claims: Sequence[SupportedClaim]) -> SupportedClaim | None:
    combined = f"{fact} {requested_action}".casefold()
    if re.search(r"\b(onboarding|calendar|calendario|schedule|timeline|implementation|implementación)\b", combined):
        return None
    if _SENSITIVE_CLIENT_COMMERCIAL.search(combined):
        return None
    if department in {DepartmentId.lastmile, DepartmentId.reverse} and re.search(
            r"\b(rate|price|tariff|cost|tarifa|precio|coste)\b", combined):
        return None

    generic = {
        DepartmentId.warehouse: r"\b(?:provide|prepare|proporcionar|preparar)\b.*\bwarehouse\b.*\b(?:response|respuesta)\b",
        DepartmentId.lastmile: r"\b(?:provide|prepare|proporcionar|preparar)\b.*\b(?:lastmile|last-mile|última milla)\b.*\b(?:response|respuesta)\b",
        DepartmentId.reverse: r"\b(?:provide|prepare|proporcionar|preparar)\b.*\b(?:reverse|returns?|devoluciones?)\b.*\b(?:response|respuesta)\b",
    }[department]
    if re.search(generic, combined):
        return claims[0] if claims else None

    topics = {
        DepartmentId.warehouse: (
            (r"\b(?:grace|gracia)\b", r"\b(?:grace|gracia)\b"),
            (r"\b(?:long-term|larga estancia|180|no movement|sin movimiento)\b",
             r"\b(?:long-term|larga estancia|180|sin movimiento|no movement)\b"),
            (r"\b(?:storage|inventory|almacenamiento|almacén|almacen|inventario|rate|price|tarifa|precio)\b",
             r"\b(?:storage|almacenamiento|usd|eur)\b"),
        ),
        DepartmentId.lastmile: (
            (r"\b(?:delivery time|shipping time|transit|entrega|envío|envio)\b",
             r"\b(?:shipping|envío|envio|3 to 5|3 a 5)\b"),
            (r"\b(?:coverage|carrier|cobertura|transportista)\b",
             r"\b(?:coverage|carrier|cobertura|transportista|last-mile|última milla)\b"),
        ),
        DepartmentId.reverse: (
            (r"\b(?:inspect|classif|inspeccion|clasific)\w*\b", r"\b(?:inspect|classif|inspeccion|clasific)\w*\b"),
            (r"\b(?:return window|return policy|plazo|política|politica)\b", r"\b(?:30 days|30 días|plazo)\b"),
            (r"\b(?:international|internacional)\w*\b", r"\b(?:international|internacional)\w*\b"),
            (r"\b(?:reverse logistics|resell|reglas|logística inversa|logistica inversa)\b",
             r"\b(?:reverse-logistics|resell|reglas|logística inversa|logistica inversa)\b"),
        ),
    }[department]
    for request_pattern, claim_pattern in topics:
        if not re.search(request_pattern, combined):
            continue
        for claim in claims:
            if re.search(claim_pattern, claim.statement.casefold()):
                return claim
    return None


def generate_department_draft(request: RFPResponseStart, *, department: DepartmentId,
                              iteration_count: int, generated_at: datetime,
                              catalog: OfficialKnowledgeCatalog | None = None,
                              content_provider: DepartmentContentProvider | None = None,
                              generation_feedback: Sequence[str] = ()) -> DepartmentDraft:
    if department not in request.selected_departments:
        raise ValueError("department is outside the approved response scope")
    if not 1 <= iteration_count <= 3:
        raise ValueError("only three real generations are allowed")
    catalog = catalog or OfficialKnowledgeCatalog()
    content_provider = content_provider or DeterministicContentProvider()
    sources = catalog.for_department(department.value)
    claims: list[SupportedClaim] = []
    terms: list[CommercialTerm] = []
    pending: list[MissingCommercialInput] = []
    commitments: list[SupportedClaim] = []
    carrier_model = "not_applicable"
    requested_aspects = [item for item in request.key_aspects if item.department == department]
    revise_for_readability = _READABILITY_FEEDBACK in generation_feedback

    if department == DepartmentId.warehouse:
        rates = sources[0].evidence
        if "US" in request.operation_countries:
            claims.append(_claim(_localized(request.language,
                "Los Angeles storage is 18 USD per cubic meter per month.",
                "El almacenamiento en Los Ángeles cuesta 18 USD por metro cúbico al mes."), rates))
            terms.append(CommercialTerm(term_id="storage_rate_us", status="documented",
                value="18 USD per cubic meter per month", currency="USD", evidence=[rates]))
        if "ES" in request.operation_countries:
            claims.append(_claim(_localized(request.language,
                "Zaragoza storage is 16 EUR per cubic meter per month.",
                "El almacenamiento en Zaragoza cuesta 16 EUR por metro cúbico al mes."), rates))
            terms.append(CommercialTerm(term_id="storage_rate_es", status="documented",
                value="16 EUR per cubic meter per month", currency="EUR", evidence=[rates]))
        if not request.operation_countries:
            terms.append(CommercialTerm(term_id="storage_rate", status="pending", evidence=[]))
            pending.append(MissingCommercialInput(code="WAREHOUSE_LOCATION_REQUIRED",
                description="The warehouse location is required to select the documented storage rate.",
                owner="Ana Whitfield"))
        claims.append(_claim(_localized(request.language,
            "New inventory receives a 30-day storage grace period.",
            "El inventario nuevo tiene un periodo de gracia de almacenamiento de 30 días."), rates))
        terms.append(CommercialTerm(term_id="storage_grace_period", status="documented",
            value="30 days", evidence=[rates]))
        long_term = sources[1].evidence
        claims.append(_claim(_localized(request.language,
            "Inventory with no movement for more than 180 days incurs a long-term inventory fee of 150% over the standard rate from day 181.",
            "El inventario sin movimiento durante más de 180 días incurre desde el día 181 en una tarifa de larga estancia del 150% sobre la tarifa estándar."), long_term))
        terms.append(CommercialTerm(term_id="long_term_inventory_rate", status="documented",
            value="150% over the applicable standard rate from day 181", evidence=[long_term]))

    elif department == DepartmentId.lastmile:
        carrier_model = ("customer_managed" if request.customer_managed_carrier is True
                         else "trackflow_managed" if request.customer_managed_carrier is False
                         else "pending")
        coverage = [source for source in sources if source.chunk.source_document == "carrier-coverage"]
        for country, index, statement in (
            ("US", 0, ("TrackFlow supports UPS, FedEx, and DHL in the United States."
                       if revise_for_readability else
                       "TrackFlow carrier coverage in the United States includes UPS, FedEx, and DHL.")),
            ("ES", 1, ("TrackFlow supports MRW, SEUR, DHL, and local carriers in Spain."
                       if revise_for_readability else
                       "TrackFlow carrier coverage in Spain includes MRW, SEUR, DHL, and local Zaragoza carriers.")),
        ):
            if country in request.operation_countries:
                spanish = (("TrackFlow trabaja con UPS, FedEx y DHL en Estados Unidos."
                            if revise_for_readability else
                            "La cobertura de transportistas de TrackFlow en Estados Unidos incluye UPS, FedEx y DHL.")
                           if country == "US" else
                           ("TrackFlow trabaja con MRW, SEUR, DHL y transportistas locales en España."
                            if revise_for_readability else
                            "La cobertura de transportistas de TrackFlow en España incluye MRW, SEUR, DHL y transportistas locales de Zaragoza."))
                claims.append(_claim(_localized(request.language, statement, spanish), coverage[index].evidence))
        if not request.operation_countries:
            pending.append(MissingCommercialInput(code="DELIVERY_COUNTRY_REQUIRED",
                description="Delivery countries are required to select documented carrier coverage.",
                owner="Carlos Vega"))
        if request.customer_managed_carrier is True:
            terms.append(CommercialTerm(term_id="last_mile_rate", status="not_applicable", evidence=[]))
            confirmation = _carrier_confirmation_evidence(request)
            if revise_for_readability:
                claims.extend([
                    _claim(_localized(request.language,
                        "The customer manages the carrier.",
                        "El cliente gestiona el transportista."), confirmation),
                    _claim(_localized(request.language,
                        "TrackFlow does not set its last-mile price or delivery promise.",
                        "TrackFlow no fija su precio ni su compromiso de entrega de última milla."),
                        confirmation),
                ])
            else:
                claims.append(_claim(_localized(request.language,
                    "The customer-managed carrier remains outside TrackFlow last-mile pricing and delivery commitments.",
                    "El transportista gestionado por el cliente queda fuera de las tarifas y compromisos de última milla de TrackFlow."),
                    confirmation))
        elif request.customer_managed_carrier is False:
            terms.append(CommercialTerm(term_id="last_mile_rate", status="pending", evidence=[]))
            pending.append(MissingCommercialInput(code="LAST_MILE_RATE_REQUIRED",
                description="No approved client-specific last-mile price exists in the official documents.",
                owner="Carlos Vega"))
            delivery = sources[-1].evidence
            if request.operation_countries:
                commitments.append(_claim(_localized(request.language,
                    "Standard domestic shipping is documented as 3 to 5 business days.",
                    "El envío nacional estándar está documentado en un plazo de 3 a 5 días hábiles."), delivery))
                claims.extend(commitments)
        else:
            terms.append(CommercialTerm(term_id="last_mile_rate", status="pending", evidence=[]))
            pending.append(MissingCommercialInput(code="CARRIER_MODEL_REQUIRED",
                description="Confirm whether TrackFlow or the customer manages the last-mile carrier.",
                owner="Carlos Vega"))

    else:
        process, policy, international, scope = (source.evidence for source in sources)
        claims.extend([
            _claim(_localized(request.language, "TrackFlow inspects returned products and classifies their disposition.",
                              "TrackFlow inspecciona los productos devueltos y clasifica su destino."), process),
            _claim(_localized(request.language, "The standard return window is 30 days from delivery unless the client contract differs.",
                              "El plazo estándar de devolución es de 30 días desde la entrega, salvo que el contrato del cliente indique otro plazo."), policy),
            _claim(_localized(request.language, "International returns require manual handling by the Returns team.",
                              "Las devoluciones internacionales requieren gestión manual del equipo de Devoluciones."), international),
            _claim(_localized(request.language, "TrackFlow executes client-defined reverse-logistics rules and does not resell products on its own account.",
                              "TrackFlow ejecuta las reglas de logística inversa definidas por el cliente y no revende productos por cuenta propia."), scope),
        ])
        terms.append(CommercialTerm(term_id="return_processing_rate", status="pending", evidence=[]))
        pending.append(MissingCommercialInput(code="RETURN_PROCESSING_RATE_REQUIRED",
            description="No approved return-processing price exists in the official documents.",
            owner="Sofía Ramos"))

    official_claims = [claim for claim in claims
                       if all(ref.document_id != "rfp-intake-v1" for ref in claim.evidence)]
    aspect_responses: list[AspectResponse] = []
    if official_claims:
        for aspect in requested_aspects:
            source_claim = _select_answer_claim(
                department, aspect.fact, aspect.requested_action, official_claims)
            if source_claim is None:
                continue
            statement = f"TrackFlow response: {source_claim.statement}"
            claims.append(_claim(statement, source_claim.evidence[0]))
            aspect_responses.append(AspectResponse(aspect_id=aspect.aspect_id,
                response_statement=statement, evidence=source_claim.evidence))

    return DepartmentDraft(draft_id=uuid4(), ticket_id=request.ticket_id, run_id=request.run_id,
        department=department, iteration_count=iteration_count,
        draft_status="provisional_missing_inputs" if pending else "complete_candidate",
        language=request.language, generation_feedback=list(generation_feedback),
        generated_content=content_provider.render(
            language=request.language, summary_for_sales=request.summary_for_sales,
            key_aspects=[_safe_aspect_context(item.fact, item.requested_action)
                         for item in requested_aspects],
            claims=[claim.statement for claim in claims],
            pending_codes=[item.code for item in pending], feedback=generation_feedback),
        client_requirements=requested_aspects, aspect_responses=aspect_responses,
        supported_claims=claims, commercial_terms=terms,
        pending_commercial_items=pending, carrier_model=carrier_model,
        last_mile_commitments=commitments, generated_at=generated_at)


def generate_global_terms(request: RFPResponseStart, *, department_draft_ids: list[UUID],
                          iteration_count: int, generated_at: datetime,
                          catalog: OfficialKnowledgeCatalog | None = None,
                          generation_feedback: Sequence[str] = ()) -> GlobalCommercialTermsDraft:
    if not 1 <= iteration_count <= 3:
        raise ValueError("only three real global generations are allowed")
    catalog = catalog or OfficialKnowledgeCatalog()
    sla_source, exclusion_source, discount_source = catalog.for_global_terms()
    pending: list[MissingCommercialInput] = []
    if request.proposal_currency:
        currency = GlobalCurrency(value=request.proposal_currency, status="documented",
            evidence=[_input_evidence(request, "proposal_currency")])
    else:
        currency = GlobalCurrency(value=None, status="pending", evidence=[])
        pending.append(MissingCommercialInput(code="PROPOSAL_CURRENCY_REQUIRED",
            description="The proposal currency is not documented in the intake handoff.", owner="Miguel Torres"))
    lastmile_selected = DepartmentId.lastmile in request.selected_departments
    lastmile_applicable = lastmile_selected and request.customer_managed_carrier is False
    if lastmile_applicable:
        sla = GlobalSLA(applicability="applicable", percentage=90,
            measurement_period="monthly by country", scope=["TrackFlow-managed last-mile delivery"],
            exclusions=["Declared high-demand dates", "Customs hold times"],
            evidence=[sla_source.evidence, exclusion_source.evidence])
    elif lastmile_selected and request.customer_managed_carrier is None:
        sla = GlobalSLA(applicability="pending", scope=[], exclusions=[], evidence=[])
        pending.append(MissingCommercialInput(code="CARRIER_MODEL_REQUIRED",
            description="SLA applicability requires confirmation of the last-mile carrier model.",
            owner="Carlos Vega"))
    else:
        sla = GlobalSLA(applicability="not_applicable", scope=[],
                        exclusions=["No TrackFlow-managed last-mile service in scope"], evidence=[])
    discounts = [VolumeDiscountRow(volume_from=50, volume_to=None, discount_percentage=None,
        condition="Average monthly storage volume above 50 cubic meters is eligible for negotiation with Miguel Torres; no percentage is documented.",
        status="pending", evidence=[discount_source.evidence])]
    warehouse_selected = DepartmentId.warehouse in request.selected_departments
    if warehouse_selected and request.average_monthly_volume_m3 is None:
        pending.append(MissingCommercialInput(code="MONTHLY_STORAGE_VOLUME_REQUIRED",
            description="Average monthly storage volume is required to determine discount applicability.",
            owner="Miguel Torres"))
    elif warehouse_selected and request.average_monthly_volume_m3 > 50:
        pending.append(MissingCommercialInput(code="VOLUME_DISCOUNT_APPROVAL_REQUIRED",
            description="A preferential storage rate requires negotiation and approval by Miguel Torres.",
            owner="Miguel Torres"))
    return GlobalCommercialTermsDraft(draft_id=uuid4(), ticket_id=request.ticket_id,
        run_id=request.run_id, global_iteration_count=iteration_count,
        generation_feedback=list(generation_feedback),
        currency=currency, sla=sla, volume_discounts=discounts,
        source_section_ids=department_draft_ids, pending_commercial_items=pending,
        generated_at=generated_at)


def volume_discount_required(request: RFPResponseStart) -> bool:
    return (DepartmentId.warehouse in request.selected_departments
            and (request.average_monthly_volume_m3 is None
                 or request.average_monthly_volume_m3 > 50))
