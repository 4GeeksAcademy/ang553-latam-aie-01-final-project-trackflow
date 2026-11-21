from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.compliance import (
    PreviewTechnicalError, build_global_safe_preview, build_safe_preview, evaluate_global_compliance,
    evaluate_section_compliance,
)
from data.pipelines.rfp_response.contracts import (
    CommercialTerm, ConsistencyIssue, DepartmentDraft, DepartmentHandoffResult,
    EvidenceReference, GlobalCommercialTermsDraft, GlobalCurrency, GlobalSLA,
    MissingCommercialInput, RFPResponseHandoff, RelevanceEvaluation,
    SafetyInspection, SupportedClaim, VolumeDiscountRow,
)
from data.pipelines.rfp_response.evaluation import (
    reduce_proposal_evaluations, reduce_section_evaluations,
)
from data.pipelines.rfp_response.readability import (
    ReadabilityTechnicalError, evaluate_readability,
)


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
HASH = "a" * 64


def evidence() -> EvidenceReference:
    return EvidenceReference(document_id="trackflow-storage", section_id="rates", evidence_hash=HASH)


def claim(text: str = "TrackFlow operates a Zaragoza warehouse.") -> SupportedClaim:
    return SupportedClaim(statement=text, evidence=[evidence()])


def missing(code: str = "STORAGE_RATE") -> MissingCommercialInput:
    return MissingCommercialInput(code=code, description="Commercial value is not documented", owner="Miguel Torres")


def draft(*, iteration: int = 1, pending: bool = False,
          carrier: str = "not_applicable", terms: list[CommercialTerm] | None = None,
          commitments: list[SupportedClaim] | None = None) -> DepartmentDraft:
    return DepartmentDraft(
        draft_id=uuid4(), ticket_id=uuid4(), run_id=uuid4(), department=DepartmentId.warehouse,
        iteration_count=iteration,
        draft_status="provisional_missing_inputs" if pending else "complete_candidate",
        language="en", generated_content="TrackFlow provides documented warehouse services.",
        supported_claims=[claim()], commercial_terms=terms or [],
        pending_commercial_items=[missing()] if pending else [], carrier_model=carrier,
        last_mile_commitments=commitments or [], generated_at=NOW,
    )


def inspection(item: DepartmentDraft, status: str = "verified_safe") -> SafetyInspection:
    return SafetyInspection(draft_id=item.draft_id, status=status,
        reason_codes=[] if status == "verified_safe" else ["UNVERIFIED_CONTENT"], inspected_at=NOW)


class Score:
    def __init__(self, value: float) -> None:
        self.score = value


class EnglishReadability:
    def __init__(self, _text: str) -> None:
        pass

    def flesch(self) -> Score:
        return Score(65)

    def flesch_kincaid(self) -> Score:
        return Score(9)

    def gunning_fog(self) -> Score:
        return Score(11)


def test_selected_department_requires_a_real_first_draft() -> None:
    item = draft(pending=True)
    assert item.iteration_count == 1
    assert item.generated_content
    assert item.draft_status == "provisional_missing_inputs"
    assert item.pending_commercial_items[0].blocks_commercial_completeness is True


def test_unknown_commercial_value_cannot_be_smuggled_into_pending_term() -> None:
    with pytest.raises(ValidationError, match="cannot carry"):
        CommercialTerm(term_id="storage_rate", status="pending", value="18", currency="EUR", evidence=[])


def test_safe_preview_fails_closed_for_blocked_indeterminate_and_technical() -> None:
    item = draft()
    for status in ("blocked", "indeterminate", "technical_error"):
        preview = build_safe_preview(item, inspection(item, status))
        assert preview.content is None
        assert preview.gate_status == status
    passed = build_safe_preview(item, inspection(item))
    assert passed.content == item.generated_content


def test_missing_required_commercial_term_forces_human_input_and_no_regeneration() -> None:
    item = draft(pending=True)
    preview = build_safe_preview(item, inspection(item))
    compliance = evaluate_section_compliance(item,
        required_commercial_terms=frozenset({"storage_rate"}), preview=preview)
    result = reduce_section_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        department=item.department, draft_id=item.draft_id, draft_hash=HASH,
        iteration_count=1,
        readability=evaluate_readability(item.generated_content, language="en", english_factory=EnglishReadability),
        relevance=RelevanceEvaluation(**{"pass": True}, score=100, feedback=[]),
        compliance=compliance, evaluated_at=NOW)
    assert result.overall_pass is False
    assert result.blocks_commercial_completeness is True
    assert result.requires_human_input is True
    assert result.regenerable is False
    assert result.terminal_status == "needs_human_input"


def test_customer_managed_carrier_needs_no_last_mile_rate_but_rejects_commitment() -> None:
    no_commitment = draft(carrier="customer_managed", terms=[
        CommercialTerm(term_id="last_mile_rate", status="not_applicable", value=None, currency=None, evidence=[])
    ])
    preview = build_safe_preview(no_commitment, inspection(no_commitment))
    assert evaluate_section_compliance(no_commitment,
        required_commercial_terms=frozenset(), preview=preview).passed is True

    bad = draft(carrier="customer_managed", terms=[
        CommercialTerm(term_id="last_mile_rate", status="documented", value="10 EUR", currency="EUR", evidence=[evidence()])
    ], commitments=[claim("TrackFlow guarantees final-mile delivery.")])
    bad_preview = build_safe_preview(bad, inspection(bad))
    result = evaluate_section_compliance(bad, required_commercial_terms=frozenset(), preview=bad_preview)
    assert result.passed is False
    assert {finding.rule_id for finding in result.findings} == {"TF-CARRIER-OWN-001"}
    assert all(finding.regenerable for finding in result.findings)


def test_preview_indeterminate_blocks_business_pass() -> None:
    item = draft()
    preview = build_safe_preview(item, inspection(item, "indeterminate"))
    compliance = evaluate_section_compliance(item, required_commercial_terms=frozenset(), preview=preview)
    assert compliance.passed is False
    assert compliance.findings[0].requires_human_input is True


def test_preview_technical_error_never_becomes_a_business_evaluation() -> None:
    item = draft()
    preview = build_safe_preview(item, inspection(item, "technical_error"))
    with pytest.raises(PreviewTechnicalError):
        evaluate_section_compliance(item, required_commercial_terms=frozenset(), preview=preview)


def test_readability_failure_regenerates_until_real_third_generation() -> None:
    item = draft()
    preview = build_safe_preview(item, inspection(item))
    compliance = evaluate_section_compliance(item, required_commercial_terms=frozenset(), preview=preview)
    readability = evaluate_readability("", language="en")
    relevance = RelevanceEvaluation(**{"pass": True}, score=100, feedback=[])
    first = reduce_section_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        department=item.department, draft_id=item.draft_id, draft_hash=HASH,
        iteration_count=1, readability=readability, relevance=relevance,
        compliance=compliance, evaluated_at=NOW)
    third = reduce_section_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        department=item.department, draft_id=uuid4(), draft_hash=HASH,
        iteration_count=3, readability=readability, relevance=relevance,
        compliance=compliance, evaluated_at=NOW)
    assert first.regenerable is True and first.terminal_status is None
    assert third.regenerable is False and third.terminal_status == "iteration_limit_reached"


def global_draft(*, currency: GlobalCurrency, sla: GlobalSLA,
                 discounts: list[VolumeDiscountRow]) -> GlobalCommercialTermsDraft:
    return GlobalCommercialTermsDraft(draft_id=uuid4(), ticket_id=uuid4(), run_id=uuid4(),
        global_iteration_count=1, currency=currency, sla=sla, volume_discounts=discounts,
        source_section_ids=[uuid4()], pending_commercial_items=[], generated_at=NOW)


def global_preview(item: GlobalCommercialTermsDraft, status: str = "verified_safe"):
    return build_global_safe_preview(item, SafetyInspection(draft_id=item.draft_id,
        status=status, reason_codes=[] if status == "verified_safe" else ["UNVERIFIED_GLOBAL"],
        inspected_at=NOW))


def test_global_pending_currency_sla_and_discounts_cannot_be_ready() -> None:
    item = global_draft(currency=GlobalCurrency(value=None, status="pending", evidence=[]),
        sla=GlobalSLA(applicability="pending", percentage=None, measurement_period=None,
            scope=[], exclusions=[], evidence=[]),
        discounts=[VolumeDiscountRow(volume_from=50, volume_to=None,
            discount_percentage=None, condition="Subject to Miguel Torres approval",
            status="pending", evidence=[evidence()])])
    result = evaluate_global_compliance(item, volume_discount_required=True,
        section_currencies=frozenset(), consistency_issues=[], preview=global_preview(item))
    assert result.passed is False
    assert {f.rule_id for f in result.findings} == {"TF-CURRENCY-001", "TF-SLA-001", "TF-DISCOUNT-001"}
    assert all(f.requires_human_input and f.blocks_commercial_completeness for f in result.findings)


def test_global_preview_is_fail_closed_and_technical_error_is_not_business() -> None:
    item = global_draft(currency=GlobalCurrency(value="EUR", status="documented", evidence=[evidence()]),
        sla=GlobalSLA(applicability="not_applicable", percentage=None,
            measurement_period=None, scope=[], exclusions=[], evidence=[]), discounts=[])
    indeterminate = evaluate_global_compliance(item, volume_discount_required=False,
        section_currencies=frozenset({"EUR"}), consistency_issues=[],
        preview=global_preview(item, "indeterminate"))
    assert indeterminate.passed is False
    assert "TF-CONFIDENTIAL-001" in {finding.rule_id for finding in indeterminate.findings}
    with pytest.raises(PreviewTechnicalError):
        evaluate_global_compliance(item, volume_discount_required=False,
            section_currencies=frozenset({"EUR"}), consistency_issues=[],
            preview=global_preview(item, "technical_error"))


def test_global_correctable_consistency_is_regenerable_but_not_approved() -> None:
    item = global_draft(currency=GlobalCurrency(value="EUR", status="documented", evidence=[evidence()]),
        sla=GlobalSLA(applicability="applicable", percentage=90,
            measurement_period="monthly", scope=["warehouse"], exclusions=[], evidence=[evidence()]),
        discounts=[])
    result = evaluate_global_compliance(item, volume_discount_required=False,
        section_currencies=frozenset({"EUR"}), consistency_issues=[
            ConsistencyIssue(code="SLA_COPY", description="Section copied an obsolete SLA", resolvable_from_evidence=True)
        ], preview=global_preview(item))
    assert result.passed is False
    assert result.findings[0].regenerable is True
    assert result.findings[0].requires_human_input is False


def test_correctable_global_result_regenerates_then_stops_at_real_limit() -> None:
    item = draft()
    preview = build_safe_preview(item, inspection(item))
    section = reduce_section_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        department=item.department, draft_id=item.draft_id, draft_hash=HASH, iteration_count=1,
        readability=evaluate_readability(item.generated_content, language="en", english_factory=EnglishReadability),
        relevance=RelevanceEvaluation(**{"pass": True}, score=100, feedback=[]),
        compliance=evaluate_section_compliance(item, required_commercial_terms=frozenset(), preview=preview),
        evaluated_at=NOW)
    global_item = global_draft(currency=GlobalCurrency(value="EUR", status="documented", evidence=[evidence()]),
        sla=GlobalSLA(applicability="not_applicable", percentage=None, measurement_period=None,
            scope=[], exclusions=[], evidence=[]), discounts=[
                VolumeDiscountRow(volume_from=50, volume_to=None, discount_percentage=None,
                    condition="Subject to Miguel Torres approval", status="pending", evidence=[evidence()])])
    compliance = evaluate_global_compliance(global_item, volume_discount_required=False,
        section_currencies=frozenset({"EUR"}), consistency_issues=[
            ConsistencyIssue(code="COPY", description="Correctable copy conflict", resolvable_from_evidence=True)
        ], preview=global_preview(global_item))
    first = reduce_proposal_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        section_results=[section], global_draft_id=global_item.draft_id,
        global_iteration_count=1, global_compliance=compliance, evaluated_at=NOW)
    third = reduce_proposal_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        section_results=[section], global_draft_id=uuid4(), global_iteration_count=3,
        global_compliance=compliance, evaluated_at=NOW)
    assert first.outcome == "regenerate" and first.regenerable is True and first.terminal_status is None
    assert third.outcome == "needs_human_review" and third.regenerable is False
    assert third.terminal_status == "iteration_limit_reached"


def test_complete_global_and_section_results_can_be_ready_for_part3() -> None:
    item = draft()
    preview = build_safe_preview(item, inspection(item))
    section_compliance = evaluate_section_compliance(item,
        required_commercial_terms=frozenset(), preview=preview)
    section = reduce_section_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        department=item.department, draft_id=item.draft_id, draft_hash=HASH, iteration_count=1,
        readability=evaluate_readability(item.generated_content, language="en", english_factory=EnglishReadability),
        relevance=RelevanceEvaluation(**{"pass": True}, score=100, feedback=[]),
        compliance=section_compliance, evaluated_at=NOW)
    global_item = global_draft(currency=GlobalCurrency(value="EUR", status="documented", evidence=[evidence()]),
        sla=GlobalSLA(applicability="not_applicable", percentage=None, measurement_period=None,
            scope=[], exclusions=[], evidence=[]), discounts=[
                VolumeDiscountRow(volume_from=50, volume_to=None, discount_percentage=None,
                    condition="Subject to Miguel Torres approval", status="pending", evidence=[evidence()])])
    global_item = global_item.model_copy(update={"ticket_id": item.ticket_id, "run_id": item.run_id})
    global_compliance = evaluate_global_compliance(global_item, volume_discount_required=False,
        section_currencies=frozenset({"EUR"}), consistency_issues=[], preview=global_preview(global_item))
    proposal = reduce_proposal_evaluations(ticket_id=item.ticket_id, run_id=item.run_id,
        section_results=[section], global_draft_id=global_item.draft_id,
        global_iteration_count=1, global_compliance=global_compliance, evaluated_at=NOW)
    assert proposal.overall_pass is True
    assert proposal.outcome == "ready_for_part3"

    handoff = RFPResponseHandoff(ticket_id=item.ticket_id, run_id=item.run_id,
        source_handoff_hash=HASH, outcome="ready_for_part3", all_sections_passed=True,
        global_compliance_passed=True, selected_departments=[item.department],
        department_results=[DepartmentHandoffResult(department=item.department,
            terminal_status="passed", iteration_count=1, final_draft_id=item.draft_id,
            evaluation_result_id=section.evaluation_id, safe_preview_id=preview.preview_id,
            blocks_commercial_completeness=False, requires_human_input=False, blocking_rule_ids=[])],
        global_draft_id=global_item.draft_id, global_evaluation_result_id=proposal.evaluation_id,
        unresolved_items=[], created_at=NOW)
    assert handoff.outcome == "ready_for_part3"


def test_handoff_cannot_claim_ready_when_any_evaluation_is_not_passed() -> None:
    with pytest.raises(ValidationError, match="ready_for_part3"):
        RFPResponseHandoff(ticket_id=uuid4(), run_id=uuid4(), source_handoff_hash=HASH,
            outcome="ready_for_part3", all_sections_passed=False, global_compliance_passed=True,
            selected_departments=[DepartmentId.reverse],
            department_results=[DepartmentHandoffResult(department=DepartmentId.reverse,
                terminal_status="needs_human_input", iteration_count=1,
                final_draft_id=uuid4(), evaluation_result_id=uuid4(), safe_preview_id=None,
                blocks_commercial_completeness=True, requires_human_input=True,
                blocking_rule_ids=["TF-COMMERCIAL-001"])],
            global_draft_id=uuid4(), global_evaluation_result_id=uuid4(),
            unresolved_items=[missing()], created_at=NOW)


def test_readability_is_defined_for_both_languages_and_na_is_not_an_error() -> None:
    english = evaluate_readability("TrackFlow provides clear warehouse services.",
        language="en", english_factory=EnglishReadability)
    spanish = evaluate_readability("TrackFlow presta servicios claros de almacén.", language="es")
    table = evaluate_readability("A | B", language="es", non_narrative=True)
    assert english.passed is True and english.readability_score == 65
    assert spanish.execution_status == "evaluated" and spanish.readability_score is not None
    assert table.execution_status == "not_applicable" and table.passed is None


def test_readability_technical_failure_does_not_fabricate_evaluation() -> None:
    class Broken:
        def __init__(self, _text: str) -> None:
            raise RuntimeError("offline")

    with pytest.raises(ReadabilityTechnicalError):
        evaluate_readability("The warehouse service is documented.", language="en", english_factory=Broken)


def test_default_english_readability_has_no_external_tokenizer_dependency() -> None:
    result = evaluate_readability(
        "TrackFlow provides clear warehouse service. Inventory remains visible.",
        language="en")
    assert result.execution_status == "evaluated"
    assert result.readability_score is not None
    assert {metric.name for metric in result.metrics} == {
        "flesch_reading_ease", "flesch_kincaid_grade", "gunning_fog",
        "average_sentence_words"}


def test_closed_contracts_reject_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        MissingCommercialInput(code="RATE", description="missing", owner="Miguel Torres", unsafe="x")
