"""Separated section/global compliance and fail-closed preview construction."""

from __future__ import annotations

import json
import re
from uuid import uuid4

from data.pipelines.rfp_response.contracts import (
    ComplianceFinding, ConsistencyIssue, DepartmentDraft, GlobalCommercialTermsDraft,
    GlobalComplianceEvaluation, SafePreview, SafetyInspection, SectionComplianceEvaluation,
)


class PreviewTechnicalError(RuntimeError):
    """The safety gate did not execute; this is not a business finding."""


_RETURN_CONTEXT = re.compile(
    r"(?i)\b(return(?:s|ed|ing)?|refunds?|reverse logistics|"
    r"devoluci(?:ón|ones)|logística inversa)\b"
)
_HOURS = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?|horas?)\b")
_DAYS = re.compile(
    r"(?i)\b(\d+(?:\.\d+)?)\s*(?:business\s+|hábiles?\s+)?(?:days?|días?)\b"
)
_ONE_DAY = re.compile(r"(?i)\b(?:one|a|un|una)\s+(?:business\s+|hábil\s+)?(?:day|día)\b")
_UNSAFE_PUBLIC_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_return_processing_hours(content: str) -> list[float]:
    """Extract only numeric durations stated in explicit return contexts."""

    observed: list[float] = []
    for segment in re.split(r"[\n.!?;]+", content):
        if not _RETURN_CONTEXT.search(segment):
            continue
        hours = [float(value) for value in _HOURS.findall(segment)]
        hours.extend(float(value) * 24 for value in _DAYS.findall(segment))
        if _ONE_DAY.search(segment):
            hours.append(24)
        observed.extend(hours)
    return observed


def _has_sub_48_hour_return_promise(content: str) -> bool:
    return any(value < 48 for value in extract_return_processing_hours(content))


def build_safe_preview(draft: DepartmentDraft, inspection: SafetyInspection) -> SafePreview:
    if inspection.draft_id != draft.draft_id:
        raise ValueError("safety inspection belongs to a different draft")
    status = {"verified_safe": "passed", "blocked": "blocked",
              "indeterminate": "indeterminate", "technical_error": "technical_error"}[inspection.status]
    passed = inspection.status == "verified_safe"
    return SafePreview(preview_id=uuid4(), draft_id=draft.draft_id, gate_status=status,
        content=draft.generated_content if passed else None,
        reason_codes=[] if passed else inspection.reason_codes)


def serialize_global_public_preview(draft: GlobalCommercialTermsDraft) -> str:
    """Return the strict public allowlist; persisted draft provenance stays internal."""
    def public_text(value: str, *, limit: int = 500) -> str:
        return _UNSAFE_PUBLIC_TEXT.sub("", value).strip()[:limit]

    public_content = {
        "currency": {
            "value": draft.currency.value,
            "status": draft.currency.status,
        },
        "sla": {
            "applicability": draft.sla.applicability,
            "percentage": draft.sla.percentage,
            "measurement_period": (
                public_text(draft.sla.measurement_period)
                if draft.sla.measurement_period else None
            ),
            "exclusions": [public_text(item) for item in draft.sla.exclusions],
        },
        "volume_discounts": [{
            "volume_from": row.volume_from,
            "volume_to": row.volume_to,
            "discount_percentage": row.discount_percentage,
            "condition": public_text(row.condition),
            "status": row.status,
        } for row in draft.volume_discounts],
        "pending_commercial_items": [{
            "code": public_text(item.code, limit=64),
            "description": public_text(item.description, limit=800),
            "owner": public_text(item.owner, limit=120),
        } for item in draft.pending_commercial_items],
    }
    return json.dumps(public_content, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def build_global_safe_preview(draft: GlobalCommercialTermsDraft,
                              inspection: SafetyInspection) -> SafePreview:
    if inspection.draft_id != draft.draft_id:
        raise ValueError("safety inspection belongs to a different global draft")
    status = {"verified_safe": "passed", "blocked": "blocked",
              "indeterminate": "indeterminate", "technical_error": "technical_error"}[inspection.status]
    passed = inspection.status == "verified_safe"
    content = serialize_global_public_preview(draft) if passed else None
    return SafePreview(preview_id=uuid4(), draft_id=draft.draft_id, gate_status=status,
        content=content, reason_codes=[] if passed else inspection.reason_codes)


def evaluate_section_compliance(draft: DepartmentDraft, *,
        required_commercial_terms: frozenset[str], preview: SafePreview) -> SectionComplianceEvaluation:
    if preview.draft_id != draft.draft_id:
        raise ValueError("preview belongs to a different draft")
    if preview.gate_status == "technical_error":
        raise PreviewTechnicalError("safe-preview gate did not execute")
    findings: list[ComplianceFinding] = []
    terms = {term.term_id: term for term in draft.commercial_terms}
    for term_id in sorted(required_commercial_terms):
        if term_id not in terms or terms[term_id].status != "documented":
            findings.append(ComplianceFinding(rule_id="TF-COMMERCIAL-001", scope="section",
                message=f"Commercial term is pending for indicative human review: {term_id}", blocking=False,
                regenerable=False, requires_human_input=True, blocks_commercial_completeness=True))
    if draft.pending_commercial_items and not any(
            finding.blocks_commercial_completeness for finding in findings):
        findings.append(ComplianceFinding(rule_id="TF-COMMERCIAL-001", scope="section",
            message="Commercial inputs remain pending for indicative human review: " + ", ".join(
                item.code for item in draft.pending_commercial_items),
            blocking=False, regenerable=False, requires_human_input=True,
            blocks_commercial_completeness=True))
    if draft.carrier_model == "customer_managed":
        rate = terms.get("last_mile_rate")
        if rate is not None and rate.status != "not_applicable":
            findings.append(ComplianceFinding(rule_id="TF-CARRIER-OWN-001", scope="section",
                message="Customer-managed carrier cannot receive a TrackFlow last-mile rate",
                blocking=True, regenerable=True, requires_human_input=False,
                blocks_commercial_completeness=False))
        if draft.last_mile_commitments:
            findings.append(ComplianceFinding(rule_id="TF-CARRIER-OWN-001", scope="section",
                message="Customer-managed carrier cannot receive TrackFlow last-mile commitments",
                blocking=True, regenerable=True, requires_human_input=False,
                blocks_commercial_completeness=False))
    if _has_sub_48_hour_return_promise(draft.generated_content):
        findings.append(ComplianceFinding(rule_id="TF-RETURN-048", scope="section",
            message="Remove the unsupported return-processing promise below 48 hours",
            blocking=True, regenerable=True, requires_human_input=False,
            blocks_commercial_completeness=False))
    if preview.gate_status != "passed":
        findings.append(ComplianceFinding(rule_id="TF-CONFIDENTIAL-001", scope="section",
            message="Draft was not verified safe for Backoffice display", blocking=True,
            regenerable=preview.gate_status == "blocked",
            requires_human_input=preview.gate_status == "indeterminate",
            blocks_commercial_completeness=False))
    return SectionComplianceEvaluation(**{"pass": not any(f.blocking for f in findings)}, findings=findings)


def evaluate_global_compliance(draft: GlobalCommercialTermsDraft, *, volume_discount_required: bool,
        section_currencies: frozenset[str], consistency_issues: list[ConsistencyIssue],
        preview: SafePreview, sla_required: bool = False,
        operation_countries: frozenset[str] = frozenset(),
        client_country_code: str | None = None) -> GlobalComplianceEvaluation:
    if preview.draft_id != draft.draft_id:
        raise ValueError("preview belongs to a different global draft")
    if preview.gate_status == "technical_error":
        raise PreviewTechnicalError("global safe-preview gate did not execute")
    findings: list[ComplianceFinding] = []
    if draft.currency.status != "documented":
        findings.append(ComplianceFinding(rule_id="TF-CURRENCY-001", scope="proposal",
            message="Proposal currency is pending", blocking=True, regenerable=False,
            requires_human_input=True, blocks_commercial_completeness=True))
    elif section_currencies and section_currencies != {draft.currency.value}:
        findings.append(ComplianceFinding(rule_id="TF-CURRENCY-001", scope="proposal",
            message="Section currencies are inconsistent with proposal currency", blocking=True,
            regenerable=True, requires_human_input=False, blocks_commercial_completeness=False))
    expected_currency = {"US": "USD", "ES": "EUR"}
    currency_scope_ambiguous = (
        len(operation_countries) > 1
        or (client_country_code is not None and len(operation_countries) == 1
            and client_country_code not in operation_countries)
    )
    if draft.currency.status == "documented" and currency_scope_ambiguous:
        findings.append(ComplianceFinding(rule_id="TF-CURRENCY-SCOPE-001", scope="proposal",
            message="Client country and operation countries do not determine one official proposal currency",
            blocking=True, regenerable=False, requires_human_input=True,
            blocks_commercial_completeness=True))
    elif (draft.currency.status == "documented" and len(operation_countries) == 1
            and draft.currency.value != expected_currency[next(iter(operation_countries))]):
        findings.append(ComplianceFinding(rule_id="TF-CURRENCY-COUNTRY-001", scope="proposal",
            message="Proposal currency conflicts with the single operation country",
            blocking=True, regenerable=True, requires_human_input=False,
            blocks_commercial_completeness=False))
    if sla_required and draft.sla.applicability != "applicable":
        findings.append(ComplianceFinding(rule_id="TF-SLA-001", scope="proposal",
            message="TrackFlow-managed last-mile scope requires the documented SLA",
            blocking=True, regenerable=True, requires_human_input=False,
            blocks_commercial_completeness=False))
    elif draft.sla.applicability == "applicable" and (
            draft.sla.percentage != 90
            or draft.sla.measurement_period != "monthly by country"
            or draft.sla.scope != ["TrackFlow-managed last-mile delivery"]):
        findings.append(ComplianceFinding(rule_id="TF-SLA-001", scope="proposal",
            message="SLA must preserve 90% monthly by country for TrackFlow-managed last-mile delivery",
            blocking=True, regenerable=True, requires_human_input=False,
            blocks_commercial_completeness=False))
    elif draft.sla.applicability == "pending":
        findings.append(ComplianceFinding(rule_id="TF-SLA-001", scope="proposal",
            message="SLA applicability is pending", blocking=True, regenerable=False,
            requires_human_input=True, blocks_commercial_completeness=True))
    if not draft.volume_discounts:
        findings.append(ComplianceFinding(rule_id="TF-DISCOUNT-TABLE-001", scope="proposal",
            message="Proposal must contain the volume-discount table, including explicit pending values",
            blocking=True, regenerable=True, requires_human_input=False,
            blocks_commercial_completeness=False))
    elif volume_discount_required and any(r.status == "pending" for r in draft.volume_discounts):
        findings.append(ComplianceFinding(rule_id="TF-DISCOUNT-001", scope="proposal",
            message="Volume discount remains pending for indicative human review", blocking=False,
            regenerable=False, requires_human_input=True, blocks_commercial_completeness=True))
    for issue in consistency_issues:
        findings.append(ComplianceFinding(rule_id="TF-CONSISTENCY-001", scope="proposal",
            message=issue.description, blocking=True, regenerable=issue.resolvable_from_evidence,
            requires_human_input=not issue.resolvable_from_evidence,
            blocks_commercial_completeness=not issue.resolvable_from_evidence))
    if preview.gate_status != "passed":
        findings.append(ComplianceFinding(rule_id="TF-CONFIDENTIAL-001", scope="proposal",
            message="Global commercial terms were not verified safe for display", blocking=True,
            regenerable=preview.gate_status == "blocked",
            requires_human_input=preview.gate_status == "indeterminate",
            blocks_commercial_completeness=False))
    return GlobalComplianceEvaluation(**{"pass": not any(f.blocking for f in findings)}, findings=findings)
