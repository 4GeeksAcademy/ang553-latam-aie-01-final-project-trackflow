"""Deterministic relevance, safety, compliance and actionable feedback."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import (
    build_global_safe_preview, build_safe_preview, evaluate_global_compliance,
    evaluate_section_compliance,
)
from data.pipelines.rfp_response.contracts import (
    AspectEvaluation, ConsistencyIssue, DepartmentDraft, EvidenceReference,
    GlobalCommercialTermsDraft, RFPResponseStart, RelevanceEvaluation,
    SafetyInspection, SectionEvaluationResult,
)
from data.pipelines.rfp_response.evaluation import reduce_section_evaluations
from data.pipelines.rfp_response.generators import (
    generate_department_draft, generate_global_terms, volume_discount_required,
)
from data.pipelines.rfp_response.grounding import grounding_violation
from data.pipelines.rfp_response.hashing import canonical_claim_id, canonical_hash
from data.pipelines.rfp_response.readability import evaluate_readability
from data.pipelines.rfp_response.sources import OfficialKnowledgeCatalog


_SENSITIVE = re.compile(
    r"(?i)\b(confidential|confidencial|internal rate|tarifa interna|precio interno|"
    r"demo rate|seed rate|tarifa de demostración)\b"
)
_NEGOTIATED_CARRIER_RATE = re.compile(
    r"(?is)(?:\b(?:UPS|FedEx|DHL|MRW|SEUR)\b.{0,100}\b(?:negotiated|negociad[ao]|"
    r"\d+(?:\.\d+)?\s*(?:USD|EUR))\b|\b(?:negotiated|negociad[ao])\b.{0,100}"
    r"\b(?:UPS|FedEx|DHL|MRW|SEUR)\b)"
)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def _intake_reference_valid(request: RFPResponseStart, reference: EvidenceReference) -> bool:
    if (reference.document_id != "rfp-intake-v1"
            or reference.evidence_hash != request.source_handoff_hash):
        return False
    allowed = {"proposal_currency"}
    allowed.update(f"key_aspects.{item.aspect_id}" for item in request.key_aspects)
    return reference.section_id in allowed


def _admin_confirmation_reference_valid(
        request: RFPResponseStart, reference: EvidenceReference) -> bool:
    return (
        reference.document_id == "admin-confirmation"
        and reference.section_id == "carrier_model"
        and reference.evidence_hash == (
            request.carrier_confirmation_hash or request.source_handoff_hash)
    )


def _reference_text(request: RFPResponseStart, catalog: OfficialKnowledgeCatalog,
                    reference: EvidenceReference) -> str | None:
    official = catalog.source_text(reference)
    if official is not None:
        return official
    if _admin_confirmation_reference_valid(request, reference):
        return str(request.customer_managed_carrier)
    if not _intake_reference_valid(request, reference):
        return None
    if reference.section_id == "proposal_currency":
        return request.proposal_currency
    aspect_id = reference.section_id.removeprefix("key_aspects.")
    aspect = next((item for item in request.key_aspects if item.aspect_id == aspect_id), None)
    return None if aspect is None else f"{aspect.fact}\n{aspect.requested_action}"


def _numbers_supported(statement: str, evidence_texts: list[str]) -> bool:
    claimed = set(_NUMBER.findall(statement))
    supported = set(_NUMBER.findall("\n".join(evidence_texts)))
    return claimed.issubset(supported)


def inspect_department_safety(request: RFPResponseStart, draft: DepartmentDraft, *,
                              inspected_at: datetime,
                              catalog: OfficialKnowledgeCatalog | None = None) -> SafetyInspection:
    catalog = catalog or OfficialKnowledgeCatalog()
    allowed = {claim.statement for claim in draft.supported_claims}
    prefix = "Dato comercial pendiente" if draft.language == "es" else "Pending commercial input"
    allowed.update(f"{prefix}: {item.code}." for item in draft.pending_commercial_items)
    lines = {line.strip() for line in draft.generated_content.splitlines() if line.strip()}
    reasons: list[str] = []
    material_text = draft.generated_content + "\n" + "\n".join(
        term.value or "" for term in draft.commercial_terms)
    if _SENSITIVE.search(material_text):
        reasons.append("possible_confidential_content")
    if _NEGOTIATED_CARRIER_RATE.search(material_text):
        reasons.append("negotiated_carrier_rate_not_disclosable")
    if not lines or not lines.issubset(allowed):
        reasons.append("content_provenance_indeterminate")
    expected = generate_department_draft(request, department=draft.department,
        iteration_count=draft.iteration_count, generated_at=draft.generated_at,
        catalog=catalog, generation_feedback=draft.generation_feedback)
    if (draft.client_requirements != expected.client_requirements
            or draft.commercial_terms != expected.commercial_terms
            or draft.pending_commercial_items != expected.pending_commercial_items
            or draft.carrier_model != expected.carrier_model
            or draft.last_mile_commitments != expected.last_mile_commitments):
        reasons.append("material_contract_differs_from_authorized_generation")
    requirements = {item.aspect_id: item for item in draft.client_requirements}
    claims_by_statement = {claim.statement: claim for claim in draft.supported_claims}
    for response in draft.aspect_responses:
        requirement = requirements.get(response.aspect_id)
        claim = claims_by_statement.get(response.response_statement)
        if (requirement is None or claim is None or claim.evidence != response.evidence):
            reasons.append("aspect_response_evidence_unverified")
            continue
        if response.response_statement.strip().casefold() in {
                requirement.fact.strip().casefold(),
                requirement.requested_action.strip().casefold()}:
            reasons.append("copied_requirement_is_not_a_response")
    allowed_documents = {
        (source.evidence.document_id, source.evidence.section_id)
        for source in catalog.for_department(draft.department.value)
    }
    expected_evidence = {
        (reference.document_id, reference.section_id, reference.evidence_hash)
        for claim in expected.supported_claims for reference in claim.evidence}
    for claim in draft.supported_claims:
        texts: list[str] = []
        for reference in claim.evidence:
            text = _reference_text(request, catalog, reference)
            if text is None or (
                    reference.document_id != "rfp-intake-v1"
                    and not _admin_confirmation_reference_valid(request, reference)
                    and (reference.document_id, reference.section_id) not in allowed_documents):
                reasons.append("claim_evidence_unverified")
                break
            texts.append(text)
        if (not claim.evidence
                or any((reference.document_id, reference.section_id,
                        reference.evidence_hash) not in expected_evidence
                       for reference in claim.evidence)):
            reasons.append("claim_evidence_not_authorized_for_department")
            continue
        supporting_statements = [expected_claim.statement
            for expected_claim in expected.supported_claims
            if any(reference in claim.evidence for reference in expected_claim.evidence)]
        grounded_to_one = any(
            grounding_violation(claim.statement, [statement]) is None
            for statement in supporting_statements)
        if (not grounded_to_one
                and grounding_violation(claim.statement, supporting_statements)):
            reasons.append("claim_text_not_grounded_in_authorized_claims")
        if texts and not _numbers_supported(claim.statement, texts):
            reasons.append("claim_material_value_unverified")
    for term in draft.commercial_terms:
        if term.status != "documented":
            continue
        texts = [text for reference in term.evidence
                 if (text := _reference_text(request, catalog, reference)) is not None]
        if len(texts) != len(term.evidence) or not _numbers_supported(term.value or "", texts):
            reasons.append("commercial_term_evidence_unverified")
    return SafetyInspection(draft_id=draft.draft_id,
        status="verified_safe" if not reasons else "blocked",
        reason_codes=list(dict.fromkeys(reasons)), inspected_at=inspected_at)


def inspect_global_safety(request: RFPResponseStart, draft: GlobalCommercialTermsDraft, *,
                          inspected_at: datetime,
                          catalog: OfficialKnowledgeCatalog | None = None) -> SafetyInspection:
    catalog = catalog or OfficialKnowledgeCatalog()
    reasons: list[str] = []
    serialized = draft.model_dump_json()
    if _SENSITIVE.search(serialized):
        reasons.append("possible_confidential_content")
    expected = generate_global_terms(request,
        department_draft_ids=draft.source_section_ids,
        iteration_count=draft.global_iteration_count,
        generated_at=draft.generated_at, catalog=catalog,
        generation_feedback=draft.generation_feedback)
    if (draft.currency != expected.currency or draft.sla != expected.sla
            or draft.volume_discounts != expected.volume_discounts
            or draft.pending_commercial_items != expected.pending_commercial_items):
        reasons.append("global_terms_differ_from_authorized_generation")
    if (draft.currency.status == "documented"
            and (draft.currency.value != request.proposal_currency
                 or len(draft.currency.evidence) != 1
                 or not _intake_reference_valid(request, draft.currency.evidence[0])
                 or draft.currency.evidence[0].section_id != "proposal_currency")):
        reasons.append("currency_provenance_indeterminate")
    if draft.sla.applicability == "applicable":
        if (draft.sla.percentage != 90
                or draft.sla.measurement_period != "monthly by country"
                or draft.sla.scope != ["TrackFlow-managed last-mile delivery"]
                or not draft.sla.evidence
                or not all(catalog.matches(reference) for reference in draft.sla.evidence)):
            reasons.append("sla_provenance_indeterminate")
    approved_condition = ("Average monthly storage volume above 50 cubic meters is eligible "
                          "for negotiation with Miguel Torres; no percentage is documented.")
    for row in draft.volume_discounts:
        if (row.status != "pending" or row.discount_percentage is not None
                or row.volume_from != 50 or row.condition != approved_condition
                or len(row.evidence) != 1 or not catalog.matches(row.evidence[0])
                or row.evidence[0].section_id != "preferential-rates"):
            reasons.append("discount_provenance_indeterminate")
    return SafetyInspection(draft_id=draft.draft_id,
        status="verified_safe" if not reasons else "blocked",
        reason_codes=list(dict.fromkeys(reasons)), inspected_at=inspected_at)


def evaluate_relevance(request: RFPResponseStart, draft: DepartmentDraft,
                       catalog: OfficialKnowledgeCatalog | None = None) -> RelevanceEvaluation:
    catalog = catalog or OfficialKnowledgeCatalog()
    aspects = [item for item in request.key_aspects if item.department == draft.department]
    content = " ".join(draft.generated_content.split())
    folded = content.casefold()
    contradiction = any(phrase in folded for phrase in (
        "will not", "cannot provide", "not provide", "no podemos", "no se proporcionará"))
    results: list[AspectEvaluation] = []
    feedback: list[str] = []
    responses = {item.aspect_id: item for item in draft.aspect_responses}
    claims = draft.supported_claims
    allowed_evidence = {
        (source.evidence.document_id, source.evidence.section_id,
         source.evidence.evidence_hash)
        for source in catalog.for_department(draft.department.value)
    }
    requirements_match = draft.client_requirements == aspects
    for aspect in aspects:
        response = responses.get(aspect.aspect_id)
        supporting_claim = next((claim for claim in claims
            if response is not None and claim.statement == response.response_statement
            and claim.evidence == response.evidence), None)
        supporting_ids: list[str] = []
        if contradiction and any(token in folded for token in re.findall(
                r"[a-záéíóúüñ]{4,}", aspect.requested_action.casefold())):
            status = "contradicted"
            explanation = "Generated content contradicts the requested action."
            feedback.append(f"Resolve contradiction for RFP aspect {aspect.aspect_id}: {aspect.requested_action}")
        elif (requirements_match and response is not None
                and response.response_statement in draft.generated_content
                and supporting_claim is not None
                and all((reference.document_id, reference.section_id,
                         reference.evidence_hash) in allowed_evidence
                        for reference in response.evidence)
                and response.response_statement.strip().casefold() not in {
                    aspect.fact.strip().casefold(), aspect.requested_action.strip().casefold(),
                    f"rfp requirement: {aspect.fact}".casefold(),
                    f"requested outcome: {aspect.requested_action}".casefold(),
                }):
            status = "addressed"
            explanation = "A distinct TrackFlow response is present and grounded in approved company evidence."
            supporting_ids = [canonical_claim_id(supporting_claim)]
        else:
            status = "missing"
            explanation = "No distinct, evidence-backed TrackFlow response covers this client requirement."
            feedback.append(f"Address missing RFP aspect {aspect.aspect_id}: {aspect.requested_action}")
        results.append(AspectEvaluation(aspect_id=aspect.aspect_id, status=status,
                                        explanation=explanation,
                                        supporting_claim_ids=supporting_ids))
    addressed = sum(item.status == "addressed" for item in results)
    score = 100.0 * addressed / len(results) if results else 0.0
    return RelevanceEvaluation(**{"pass": bool(results) and addressed == len(results)},
                               score=score, feedback=feedback[:20],
                               aspect_results=results)


def required_terms(request: RFPResponseStart, department: DepartmentId) -> frozenset[str]:
    if department == DepartmentId.warehouse:
        return frozenset({f"storage_rate_{country.casefold()}" for country in request.operation_countries}) or frozenset({"storage_rate"})
    if department == DepartmentId.lastmile:
        return (frozenset({"last_mile_rate"})
                if request.customer_managed_carrier is False else frozenset())
    return frozenset({"return_processing_rate"})


def evaluate_department(request: RFPResponseStart, draft: DepartmentDraft, *,
                        evaluated_at: datetime,
                        english_factory: Callable[[str], Any] | None = None) -> tuple[SectionEvaluationResult, list[str]]:
    if (draft.ticket_id, draft.run_id) != (request.ticket_id, request.run_id):
        raise ValueError("draft belongs to a different response request")
    inspection = inspect_department_safety(request, draft, inspected_at=evaluated_at)
    preview = build_safe_preview(draft, inspection)
    readability = evaluate_readability(draft.generated_content, language=draft.language,
                                       english_factory=english_factory)
    relevance = evaluate_relevance(request, draft)
    compliance = evaluate_section_compliance(draft,
        required_commercial_terms=required_terms(request, draft.department), preview=preview)
    draft_hash = canonical_hash(draft)
    result = reduce_section_evaluations(ticket_id=draft.ticket_id, run_id=draft.run_id,
        department=draft.department, draft_id=draft.draft_id, draft_hash=draft_hash,
        iteration_count=draft.iteration_count, readability=readability, relevance=relevance,
        compliance=compliance, evaluated_at=evaluated_at)
    return result, result.actionable_feedback


def evaluate_global(request: RFPResponseStart, draft: GlobalCommercialTermsDraft, *,
                    section_currencies: frozenset[str], consistency_issues: list[ConsistencyIssue],
                    evaluated_at: datetime):
    inspection = inspect_global_safety(request, draft, inspected_at=evaluated_at)
    preview = build_global_safe_preview(draft, inspection)
    result = evaluate_global_compliance(draft,
        volume_discount_required=volume_discount_required(request),
        section_currencies=section_currencies, consistency_issues=consistency_issues,
        preview=preview,
        sla_required=(DepartmentId.lastmile in request.selected_departments
                      and request.customer_managed_carrier is False),
        operation_countries=frozenset(
            request.operation_countries
            or ([request.client_country_code] if request.client_country_code else [])),
        client_country_code=request.client_country_code)
    return result, [finding.message for finding in result.findings if finding.blocking]
