from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import (
    build_global_safe_preview, build_safe_preview, evaluate_section_compliance,
)
from data.pipelines.rfp_response.contracts import (
    DepartmentHandoffResult, GlobalSLA, RequestedAspect, RFPResponseHandoff,
    RFPResponseStart, SafetyInspection, VolumeDiscountRow,
)
from data.pipelines.rfp_response.evaluators import (
    evaluate_department, evaluate_global, evaluate_relevance,
    inspect_department_safety, inspect_global_safety,
)
from data.pipelines.rfp_response.generators import (
    DeterministicContentProvider, generate_department_draft, generate_global_terms,
)
from data.pipelines.rfp_response.sources import OfficialKnowledgeCatalog
from data.pipelines.rfp_response.hashing import canonical_hash


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
HASH = "c" * 64


class Score:
    def __init__(self, score: float) -> None:
        self.score = score


class PassingEnglishReadability:
    def __init__(self, _text: str) -> None:
        pass

    def flesch(self) -> Score:
        return Score(70)

    def flesch_kincaid(self) -> Score:
        return Score(8)

    def gunning_fog(self) -> Score:
        return Score(10)


def request(*, departments=(DepartmentId.warehouse,), countries=("US",),
            currency="USD", volume=20, own_carrier=False, language="en",
            client_country="match") -> RFPResponseStart:
    aspects = [RequestedAspect(aspect_id=f"{department.value}:01:1-2",
        department=department,
        fact=f"Client requests {department.value} service in scope.",
        requested_action=f"Provide a {department.value} response.",
        line_start=1, line_end=2) for department in departments]
    return RFPResponseStart(ticket_id=uuid4(), run_id=uuid4(), source_handoff_hash=HASH,
        summary_for_sales="Prepare the requested TrackFlow service response.",
        key_aspects=aspects,
        selected_departments=list(departments), operation_countries=list(countries),
        client_country_code=(countries[0] if client_country == "match" and len(countries) == 1
                             else None if client_country == "match" else client_country),
        proposal_currency=currency, average_monthly_volume_m3=volume,
        customer_managed_carrier=own_carrier, language=language)


def test_official_catalog_has_stable_hashes_for_all_four_documents() -> None:
    catalog = OfficialKnowledgeCatalog()
    sources = (
        catalog.get("sla-delivery", "monthly-sla-and-compensation"),
        catalog.get("returns-policy", "return-window-and-costs"),
        catalog.get("carrier-coverage", "selection-and-exceptions"),
        catalog.get("storage-pricing", "standard-rates-and-grace-period"),
    )
    assert len({source.evidence.document_id for source in sources}) == 4
    assert all(len(source.evidence.evidence_hash) == 64 for source in sources)


def test_deterministic_provider_receives_only_approved_claims_and_pending_codes() -> None:
    captured = {}

    class RecordingProvider(DeterministicContentProvider):
        def render(self, *, language, summary_for_sales, key_aspects, claims, pending_codes,
                   feedback):
            captured.update(language=language, claims=tuple(claims),
                            pending_codes=tuple(pending_codes),
                            summary_for_sales=summary_for_sales,
                            key_aspects=tuple(key_aspects), feedback=tuple(feedback))
            return super().render(language=language, summary_for_sales=summary_for_sales,
                                  key_aspects=key_aspects, claims=claims,
                                  pending_codes=pending_codes, feedback=feedback)

    item_request = request(departments=(DepartmentId.reverse,))
    draft = generate_department_draft(item_request, department=DepartmentId.reverse,
        iteration_count=1, generated_at=NOW, content_provider=RecordingProvider())
    assert captured["pending_codes"] == ("RETURN_PROCESSING_RATE_REQUIRED",)
    assert captured["summary_for_sales"] == item_request.summary_for_sales
    assert captured["key_aspects"]
    assert all("# Returns Policy" not in claim for claim in captured["claims"])
    assert draft.generated_content


@pytest.mark.parametrize(("department", "pending_code"), [
    (DepartmentId.warehouse, None),
    (DepartmentId.lastmile, "LAST_MILE_RATE_REQUIRED"),
    (DepartmentId.reverse, "RETURN_PROCESSING_RATE_REQUIRED"),
])
def test_each_department_generates_grounded_or_explicitly_pending_draft(department, pending_code) -> None:
    item_request = request(departments=(department,))
    draft = generate_department_draft(item_request, department=department,
                                      iteration_count=1, generated_at=NOW)
    assert draft.department == department
    assert draft.generated_content and draft.supported_claims
    evidence = [ref for claim in draft.supported_claims for ref in claim.evidence]
    assert all(ref.document_id != "trackflow-demo" for ref in evidence)
    if pending_code:
        assert draft.draft_status == "provisional_missing_inputs"
        assert pending_code in {item.code for item in draft.pending_commercial_items}
    else:
        assert draft.draft_status == "complete_candidate"
        assert next(term for term in draft.commercial_terms
                    if term.term_id == "storage_rate_us").value.startswith("18 USD")


def test_warehouse_without_location_preserves_real_provisional_draft_and_evaluation() -> None:
    item_request = request(countries=(), currency=None, volume=None)
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    result, feedback = evaluate_department(item_request, draft, evaluated_at=NOW,
                                            english_factory=PassingEnglishReadability)
    assert draft.generated_content
    assert draft.iteration_count == 1
    assert result.overall_pass is False
    assert result.requires_human_input is True
    assert "storage_rate" in feedback[0]


def test_documented_warehouse_draft_passes_all_section_evaluators() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    result, feedback = evaluate_department(item_request, draft, evaluated_at=NOW,
                                            english_factory=PassingEnglishReadability)
    assert result.overall_pass is True
    assert result.terminal_status == "passed"
    assert feedback == []


def test_real_briefing_rule_keeps_undocumented_lastmile_price_pending() -> None:
    item_request = request(departments=(DepartmentId.lastmile,))
    draft = generate_department_draft(item_request, department=DepartmentId.lastmile,
                                      iteration_count=1, generated_at=NOW)
    result, feedback = evaluate_department(item_request, draft, evaluated_at=NOW,
                                            english_factory=PassingEnglishReadability)
    rate = next(term for term in draft.commercial_terms if term.term_id == "last_mile_rate")
    assert rate.status == "pending" and rate.value is None
    assert result.blocks_commercial_completeness and result.requires_human_input
    assert any("last_mile_rate" in item for item in feedback)


def test_customer_carrier_has_no_trackflow_rate_or_commitment() -> None:
    item_request = request(departments=(DepartmentId.lastmile,), own_carrier=True)
    draft = generate_department_draft(item_request, department=DepartmentId.lastmile,
                                      iteration_count=1, generated_at=NOW)
    result, _ = evaluate_department(item_request, draft, evaluated_at=NOW,
                                    english_factory=PassingEnglishReadability)
    rate = next(term for term in draft.commercial_terms if term.term_id == "last_mile_rate")
    assert rate.status == "not_applicable"
    assert draft.last_mile_commitments == []
    assert result.overall_pass is True
    confirmation_claim = next(
        claim for claim in draft.supported_claims
        if "customer-managed carrier" in claim.statement
    )
    assert confirmation_claim.evidence[0].document_id == "admin-confirmation"
    assert confirmation_claim.evidence[0].section_id == "carrier_model"
    assert all(
        not (reference.document_id == "rfp-intake-v1"
             and reference.section_id == "customer_managed_carrier")
        for claim in draft.supported_claims for reference in claim.evidence
    )


def test_unknown_carrier_model_remains_pending_without_lastmile_commitments() -> None:
    item_request = request(departments=(DepartmentId.lastmile,), own_carrier=None)
    draft = generate_department_draft(item_request, department=DepartmentId.lastmile,
                                      iteration_count=1, generated_at=NOW)
    global_draft = generate_global_terms(item_request,
        department_draft_ids=[draft.draft_id], iteration_count=1, generated_at=NOW)
    assert draft.carrier_model == "pending"
    assert draft.last_mile_commitments == []
    assert "CARRIER_MODEL_REQUIRED" in {
        item.code for item in draft.pending_commercial_items
    }
    assert "LAST_MILE_RATE_REQUIRED" not in {
        item.code for item in draft.pending_commercial_items
    }
    result, feedback = evaluate_department(
        item_request, draft, evaluated_at=NOW,
        english_factory=PassingEnglishReadability,
    )
    assert result.requires_human_input is True
    assert result.blocks_commercial_completeness is True
    assert all("last_mile_rate" not in item for item in feedback)
    assert global_draft.sla.applicability == "pending"
    assert {item.code for item in global_draft.pending_commercial_items} == {
        "CARRIER_MODEL_REQUIRED"
    }


def test_readability_feedback_changes_customer_carrier_draft_and_passes_real_metrics() -> None:
    item_request = request(departments=(DepartmentId.lastmile,), own_carrier=True)
    first = generate_department_draft(
        item_request, department=DepartmentId.lastmile,
        iteration_count=1, generated_at=NOW,
    )
    first_result, feedback = evaluate_department(
        item_request, first, evaluated_at=NOW,
    )
    assert first_result.overall_pass is False
    assert feedback == [
        "Shorten sentences and use clearer language before regenerating the section."
    ]

    second = generate_department_draft(
        item_request, department=DepartmentId.lastmile,
        iteration_count=2, generated_at=NOW, generation_feedback=feedback,
    )
    second_result, second_feedback = evaluate_department(
        item_request, second, evaluated_at=NOW,
    )

    assert second.generated_content != first.generated_content
    assert "The customer manages the carrier." in second.generated_content
    assert second_result.readability.passed is True
    assert second_result.overall_pass is True
    assert second_result.terminal_status == "passed"
    assert second_feedback == []


def test_department_sources_are_isolated() -> None:
    catalog = OfficialKnowledgeCatalog()
    assert {s.evidence.document_id for s in catalog.for_department("warehouse")} == {"storage-pricing"}
    assert {s.evidence.document_id for s in catalog.for_department("reverse")} == {"returns-policy"}
    assert {s.evidence.document_id for s in catalog.for_department("lastmile")} == {"carrier-coverage", "sla-delivery"}


def test_safe_preview_inspection_fails_closed_for_untraceable_content() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={"generated_content": draft.generated_content + "\nSecret client rate: 7."})
    inspection = inspect_department_safety(item_request, altered, inspected_at=NOW)
    assert inspection.status == "blocked"
    assert "content_provenance_indeterminate" in inspection.reason_codes


def test_global_terms_complete_when_discount_not_applicable() -> None:
    item_request = request(volume=20)
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    result, feedback = evaluate_global(item_request, draft, section_currencies=frozenset({"USD"}),
                                       consistency_issues=[], evaluated_at=NOW)
    assert draft.currency.value == "USD"
    assert draft.sla.applicability == "not_applicable"
    assert draft.volume_discounts[0].discount_percentage is None
    assert result.passed is True and feedback == []


def test_global_preview_is_backend_allowlisted_and_contains_no_internal_structure() -> None:
    item_request = request(volume=20)
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    preview = build_global_safe_preview(draft, SafetyInspection(
        draft_id=draft.draft_id, status="verified_safe", reason_codes=[], inspected_at=NOW))
    public = json.loads(preview.content)

    assert set(public) == {
        "currency", "sla", "volume_discounts", "pending_commercial_items"
    }
    assert set(public["currency"]) == {"value", "status"}
    assert set(public["sla"]) == {
        "applicability", "percentage", "measurement_period", "exclusions"
    }
    assert set(public["volume_discounts"][0]) == {
        "volume_from", "volume_to", "discount_percentage", "condition", "status"
    }
    encoded = preview.content
    for forbidden in (
        "draft_id", "run_id", "ticket_id", "evidence", "evidence_hash",
        "source_section_ids", "contract_version", "generated_at", "generation_feedback",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(("currency", "volume", "rule"), [
    (None, 20, "TF-CURRENCY-001"),
    ("USD", None, "TF-DISCOUNT-001"),
    ("USD", 60, "TF-DISCOUNT-001"),
])
def test_global_terms_incomplete_inputs_never_pass(currency, volume, rule) -> None:
    item_request = request(currency=currency, volume=volume)
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    result, feedback = evaluate_global(item_request, draft, section_currencies=frozenset({"USD"}),
                                       consistency_issues=[], evaluated_at=NOW)
    assert result.passed is False
    assert rule in {finding.rule_id for finding in result.findings}
    assert feedback


def test_generation_limit_counts_real_generations_only() -> None:
    item_request = request()
    third = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=3, generated_at=NOW)
    assert third.iteration_count == 3
    with pytest.raises(ValueError, match="three real generations"):
        generate_department_draft(item_request, department=DepartmentId.warehouse,
                                  iteration_count=4, generated_at=NOW)


def test_spanish_generation_and_readability_are_evaluated_in_spanish() -> None:
    item_request = request(language="es")
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    result, _ = evaluate_department(item_request, draft, evaluated_at=NOW)
    assert "almacenamiento" in draft.generated_content
    assert result.readability.language == "es"
    assert result.readability.execution_status == "evaluated"


def test_storage_discount_does_not_block_proposal_without_warehouse_scope() -> None:
    item_request = request(departments=(DepartmentId.lastmile,), volume=None, own_carrier=True)
    section = generate_department_draft(item_request, department=DepartmentId.lastmile,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    result, _ = evaluate_global(item_request, draft, section_currencies=frozenset({"USD"}),
                                consistency_issues=[], evaluated_at=NOW)
    assert "MONTHLY_STORAGE_VOLUME_REQUIRED" not in {
        item.code for item in draft.pending_commercial_items
    }
    assert result.passed is True


def test_evaluator_and_store_use_identical_canonical_draft_hash() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    result, _ = evaluate_department(item_request, draft, evaluated_at=NOW,
                                    english_factory=PassingEnglishReadability)
    assert result.draft_hash == canonical_hash(draft)


def test_relevance_rejects_claims_when_requested_aspect_is_omitted_from_content() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    stripped = "\n".join(line for line in draft.generated_content.splitlines()
                           if not line.startswith("TrackFlow response:"))
    altered = draft.model_copy(update={"generated_content": stripped})
    relevance = evaluate_relevance(item_request, altered)
    assert relevance.passed is False
    assert relevance.aspect_results[0].status == "missing"
    assert relevance.feedback


def test_onboarding_calendar_repeated_as_requirement_is_not_an_answer() -> None:
    item_request = request()
    aspect = RequestedAspect(aspect_id="warehouse:onboarding:1-2",
        department=DepartmentId.warehouse,
        fact="The client needs warehouse onboarding before launch.",
        requested_action="Provide a detailed onboarding calendar.",
        line_start=1, line_end=2)
    item_request = item_request.model_copy(update={"key_aspects": [aspect]})
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    repeated = draft.model_copy(update={"generated_content":
        draft.generated_content +
        f"\nRFP requirement: {aspect.fact}\nRequested outcome: {aspect.requested_action}"})
    relevance = evaluate_relevance(item_request, repeated)
    assert draft.aspect_responses == []
    assert relevance.passed is False
    assert relevance.aspect_results[0].status == "missing"


def test_unknown_semantic_request_is_not_matched_by_department_name_alone() -> None:
    item_request = request()
    aspect = RequestedAspect(aspect_id="warehouse:sustainability:1-2",
        department=DepartmentId.warehouse,
        fact="The warehouse must use certified carbon-neutral energy.",
        requested_action="Provide the certification and annual emissions report.",
        line_start=1, line_end=2)
    item_request = item_request.model_copy(update={"key_aspects": [aspect]})
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    relevance = evaluate_relevance(item_request, draft)
    assert draft.aspect_responses == []
    assert relevance.passed is False
    assert relevance.aspect_results[0].status == "missing"


def test_reverse_promise_below_48_hours_is_blocking_and_regenerable() -> None:
    item_request = request(departments=(DepartmentId.reverse,))
    draft = generate_department_draft(item_request, department=DepartmentId.reverse,
                                      iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={
        "generated_content": draft.generated_content + "\nReturns are processed in 12 hours."
    })
    preview = build_safe_preview(altered, SafetyInspection(draft_id=altered.draft_id,
        status="verified_safe", reason_codes=[], inspected_at=NOW))
    compliance = evaluate_section_compliance(altered,
        required_commercial_terms=frozenset({"return_processing_rate"}), preview=preview)
    finding = next(item for item in compliance.findings if item.rule_id == "TF-RETURN-048")
    assert finding.blocking and finding.regenerable


@pytest.mark.parametrize("promise", [
    "Returns are processed in 24 h.",
    "Return processing completes in 24 hours.",
    "Las devoluciones se procesan en 24 horas.",
    "Returns are completed in 1 day.",
    "Las devoluciones se completan en 1 día.",
    "Returns are completed in one day.",
])
def test_sub_48_hour_return_promises_are_detected_in_any_section(promise) -> None:
    item_request = request(departments=(DepartmentId.warehouse,))
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={"generated_content": draft.generated_content + "\n" + promise})
    preview = build_safe_preview(altered, SafetyInspection(draft_id=altered.draft_id,
        status="verified_safe", reason_codes=[], inspected_at=NOW))
    result = evaluate_section_compliance(altered,
        required_commercial_terms=frozenset({"storage_rate_us"}), preview=preview)
    assert "TF-RETURN-048" in {item.rule_id for item in result.findings}


def test_unrelated_24_hour_number_is_not_treated_as_return_turnaround() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={
        "generated_content": draft.generated_content + "\nWarehouse support replies in 24 h."
    })
    preview = build_safe_preview(altered, SafetyInspection(draft_id=altered.draft_id,
        status="verified_safe", reason_codes=[], inspected_at=NOW))
    result = evaluate_section_compliance(altered,
        required_commercial_terms=frozenset({"storage_rate_us"}), preview=preview)
    assert "TF-RETURN-048" not in {item.rule_id for item in result.findings}


def test_managed_lastmile_without_sla_is_rejected() -> None:
    item_request = request(departments=(DepartmentId.lastmile,))
    section = generate_department_draft(item_request, department=DepartmentId.lastmile,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={"sla": GlobalSLA(applicability="not_applicable",
        percentage=None, measurement_period=None, scope=[], exclusions=[], evidence=[])})
    result, _ = evaluate_global(item_request, altered, section_currencies=frozenset({"USD"}),
                                consistency_issues=[], evaluated_at=NOW)
    assert "TF-SLA-001" in {item.rule_id for item in result.findings}


def test_proposal_without_discount_table_is_rejected() -> None:
    item_request = request(volume=20)
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    altered = draft.model_copy(update={"volume_discounts": []})
    result, _ = evaluate_global(item_request, altered, section_currencies=frozenset({"USD"}),
                                consistency_issues=[], evaluated_at=NOW)
    assert "TF-DISCOUNT-TABLE-001" in {item.rule_id for item in result.findings}


def test_currency_conflicting_with_single_country_is_rejected() -> None:
    item_request = request(countries=("US",), currency="EUR")
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    result, _ = evaluate_global(item_request, draft, section_currencies=frozenset({"EUR"}),
                                consistency_issues=[], evaluated_at=NOW)
    assert "TF-CURRENCY-COUNTRY-001" in {item.rule_id for item in result.findings}


def test_client_es_operation_us_currency_usd_requires_human_review() -> None:
    item_request = request(countries=("US",), currency="USD", client_country="ES")
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    result, _ = evaluate_global(item_request, draft, section_currencies=frozenset({"USD"}),
                                consistency_issues=[], evaluated_at=NOW)
    finding = next(item for item in result.findings
                   if item.rule_id == "TF-CURRENCY-SCOPE-001")
    assert finding.requires_human_input is True
    assert finding.blocks_commercial_completeness is True


def test_confidential_global_rate_is_blocked_before_preview() -> None:
    item_request = request()
    section = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                        iteration_count=1, generated_at=NOW)
    draft = generate_global_terms(item_request, department_draft_ids=[section.draft_id],
                                  iteration_count=1, generated_at=NOW)
    row = draft.volume_discounts[0].model_copy(update={
        "condition": "Confidential internal rate for this client"
    })
    altered = draft.model_copy(update={"volume_discounts": [row]})
    inspection = inspect_global_safety(item_request, altered, inspected_at=NOW)
    assert inspection.status == "blocked"
    assert "possible_confidential_content" in inspection.reason_codes


def test_ready_handoff_rejects_section_with_remaining_blocker() -> None:
    with pytest.raises(ValidationError, match="cannot retain blockers"):
        DepartmentHandoffResult(department=DepartmentId.warehouse,
            terminal_status="passed", iteration_count=1, final_draft_id=uuid4(),
            evaluation_result_id=uuid4(), safe_preview_id=uuid4(),
            blocks_commercial_completeness=True, requires_human_input=False,
            blocking_rule_ids=["TF-COMMERCIAL-001"])


def test_iteration_limit_from_readability_can_have_no_compliance_blockers() -> None:
    result = DepartmentHandoffResult(department=DepartmentId.warehouse,
        terminal_status="iteration_limit_reached", iteration_count=3,
        final_draft_id=uuid4(), evaluation_result_id=uuid4(), safe_preview_id=uuid4(),
        blocks_commercial_completeness=False, requires_human_input=False,
        blocking_rule_ids=[])
    assert result.terminal_status == "iteration_limit_reached"


def test_evidence_reference_must_match_department_and_document_hash() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    wrong = OfficialKnowledgeCatalog().get(
        "carrier-coverage", "united-states-coverage").evidence
    first = draft.supported_claims[0].model_copy(update={"evidence": [wrong]})
    altered = draft.model_copy(update={"supported_claims": [first, *draft.supported_claims[1:]]})
    inspection = inspect_department_safety(item_request, altered, inspected_at=NOW)
    assert inspection.status == "blocked"
    assert "claim_evidence_unverified" in inspection.reason_codes


def test_negotiated_ups_rate_from_intake_is_withheld_and_cannot_authorize_preview() -> None:
    captured = {}

    class RecordingProvider(DeterministicContentProvider):
        def render(self, *, language, summary_for_sales, key_aspects, claims, pending_codes,
                   feedback):
            captured["key_aspects"] = tuple(key_aspects)
            return super().render(language=language, summary_for_sales=summary_for_sales,
                key_aspects=key_aspects, claims=claims, pending_codes=pending_codes,
                feedback=feedback)

    item_request = request(departments=(DepartmentId.lastmile,), own_carrier=True)
    aspect = RequestedAspect(aspect_id="lastmile:ups-rate:1-2",
        department=DepartmentId.lastmile,
        fact="The client has a negotiated UPS rate of 4.20 USD per shipment.",
        requested_action="Include the negotiated UPS price in the proposal.",
        line_start=1, line_end=2)
    item_request = item_request.model_copy(update={"key_aspects": [aspect]})
    draft = generate_department_draft(item_request, department=DepartmentId.lastmile,
        iteration_count=1, generated_at=NOW, content_provider=RecordingProvider())
    relevance = evaluate_relevance(item_request, draft)
    assert captured["key_aspects"] == ("[sensitive commercial request withheld]",)
    assert "4.20" not in draft.generated_content
    assert relevance.passed is False

    leaked = draft.model_copy(update={
        "generated_content": draft.generated_content + "\nUPS negotiated rate is 4.20 USD."
    })
    inspection = inspect_department_safety(item_request, leaked, inspected_at=NOW)
    assert inspection.status == "blocked"
    assert "negotiated_carrier_rate_not_disclosable" in inspection.reason_codes


def test_actionable_feedback_and_missing_aspects_are_inside_persisted_contract() -> None:
    item_request = request()
    draft = generate_department_draft(item_request, department=DepartmentId.warehouse,
                                      iteration_count=1, generated_at=NOW)
    stripped = "\n".join(line for line in draft.generated_content.splitlines()
                           if not line.startswith("TrackFlow response:"))
    altered = draft.model_copy(update={"generated_content": stripped})
    result, _ = evaluate_department(item_request, altered, evaluated_at=NOW,
                                    english_factory=PassingEnglishReadability)
    payload = result.model_dump(mode="json")
    assert payload["actionable_feedback"]
    assert payload["missing_aspect_ids"] == [item_request.key_aspects[0].aspect_id]
