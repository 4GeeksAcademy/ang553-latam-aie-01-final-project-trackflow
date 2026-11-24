from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    CommercialTerm,
    DepartmentDraft,
    EvidenceReference,
    GlobalCommercialTermsDraft,
    GlobalCurrency,
    GlobalSLA,
    SupportedClaim,
)
from services.rfp_approval.assessment_coordinator import (
    InitialAssessmentSources,
    _department_draft_from_revision,
    _requires_historical_department_rollup,
    build_initial_assessments,
)
from pipelines.rfp_approval.contracts import (
    MonthlyStorageVolumeInput,
    QuantityScope,
    WarehouseCapacityInput,
)
from services.rfp_approval.store import ApprovalStoreError


NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


def _evidence() -> list[EvidenceReference]:
    return [
        EvidenceReference(
            document_id="qa-rfp",
            section_id="commercial",
            evidence_hash="a" * 64,
        )
    ]


def _draft(
    department: DepartmentId,
    *,
    content: str,
    currency: str = "USD",
) -> DepartmentDraft:
    evidence = _evidence()
    return DepartmentDraft(
        draft_id=uuid4(),
        ticket_id=uuid4(),
        run_id=uuid4(),
        department=department,
        iteration_count=1,
        draft_status="complete_candidate",
        language="en",
        generated_content=content,
        supported_claims=[SupportedClaim(statement="QA supported claim", evidence=evidence)],
        commercial_terms=[
            CommercialTerm(
                term_id="client_rate",
                status="documented",
                value="Documented QA rate",
                currency=currency,
                evidence=evidence,
            )
        ],
        pending_commercial_items=[],
        carrier_model="trackflow_managed",
        last_mile_commitments=[],
        generated_at=NOW,
    )


def _global(currency: str = "USD") -> GlobalCommercialTermsDraft:
    evidence = _evidence()
    return GlobalCommercialTermsDraft(
        draft_id=uuid4(),
        ticket_id=uuid4(),
        run_id=uuid4(),
        global_iteration_count=1,
        currency=GlobalCurrency(
            value=currency,
            status="documented",
            evidence=evidence,
        ),
        sla=GlobalSLA(
            applicability="not_applicable",
            percentage=None,
            measurement_period=None,
            scope=[],
            exclusions=[],
            evidence=[],
        ),
        volume_discounts=[],
        source_section_ids=[uuid4()],
        pending_commercial_items=[],
        generated_at=NOW,
    )


def _sources(
    departments: tuple[DepartmentId, ...],
    *,
    contents: dict[DepartmentId, str] | None = None,
    section_currency: str = "USD",
    global_currency: str = "USD",
    monthly_volume: Decimal | None = Decimal("100"),
    capacity: WarehouseCapacityInput | None = None,
) -> InitialAssessmentSources:
    contents = contents or {}
    revisions = tuple(
        {
            "department_id": department.value,
            "revision_id": uuid4(),
            "content_hash": chr(98 + index) * 64,
        }
        for index, department in enumerate(departments)
    )
    drafts = {
        department: _draft(
            department,
            content=contents.get(department, "No return turnaround is promised."),
            currency=section_currency,
        )
        for department in departments
    }
    return InitialAssessmentSources(
        active_departments=departments,
        metadata={
            "client_country_code": "US",
            "monthly_volume_value": monthly_volume,
            "monthly_volume_unit": "orders/month" if monthly_volume is not None else None,
            "monthly_volume_source_ref": (
                {"document_id": "qa-rfp"} if monthly_volume is not None else None
            ),
        },
        revisions=revisions,
        department_drafts=drafts,
        global_draft=_global(global_currency),
        capacity_input_id=uuid4() if capacity is not None else None,
        capacity=capacity,
    )


def test_volume_trigger_is_no_conflict_when_lastmile_is_not_active() -> None:
    assessments = build_initial_assessments(
        _sources((DepartmentId.warehouse, DepartmentId.reverse)),
        assessed_at=NOW,
    )

    volume, returns, currency = assessments
    assert volume.outcome == "no_conflict"
    assert volume.facts.comparison_status == "not_applicable"
    assert returns.outcome == "no_conflict"
    assert returns.facts.observed == []
    assert currency.outcome == "no_conflict"


def test_active_lastmile_requests_capacity_without_fabricating_it() -> None:
    assessments = build_initial_assessments(
        _sources((DepartmentId.warehouse, DepartmentId.lastmile)),
        assessed_at=NOW,
    )

    volume = assessments[0]
    assert volume.outcome == "insufficient_information"
    assert volume.missing_input_codes == ["WAREHOUSE_CAPACITY_REQUIRED"]
    assert volume.facts.available_capacity is None
    assert volume.facts.capacity_input_id is None


def test_authorized_monthly_volume_supplies_missing_pdf_fact_without_conversion() -> None:
    capacity = WarehouseCapacityInput(
        site_code="WH-QA-01",
        capacity_value=Decimal("100"),
        capacity_unit="m3",
        period="month",
        available_from=date(2026, 11, 1),
    )
    sources = _sources(
        (DepartmentId.warehouse, DepartmentId.lastmile),
        monthly_volume=None,
        capacity=capacity,
    )
    volume_id = uuid4()
    sources = replace(
        sources,
        volume_input_id=volume_id,
        volume=MonthlyStorageVolumeInput(
                value=Decimal("42.1250"),
                unit="m3",
                period="month",
                operational_scope=QuantityScope(
                    scope_code="monthly-storage-volume",
                    service_code="warehouse-storage",
                ),
                effective_from=date(2026, 11, 1),
            ),
    )
    assessment = build_initial_assessments(sources, assessed_at=NOW)[0]
    assert assessment.outcome == "no_conflict"
    assert assessment.facts.required_volume.value == Decimal("42.1250")
    assert assessment.facts.required_volume_input_id == volume_id


def test_authorized_monthly_volume_rejects_incompatible_unit_and_pdf_contradiction() -> None:
    base = _sources(
        (DepartmentId.warehouse, DepartmentId.lastmile),
        monthly_volume=Decimal("40"),
    )
    incompatible = replace(
        base,
        volume_input_id=uuid4(),
        volume=MonthlyStorageVolumeInput.model_construct(
                value=Decimal("42"),
            dimension="volume",
                unit="ft3",
                period="month",
                operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
                effective_from=date(2026, 11, 1),
            ),
    )
    assessment = build_initial_assessments(incompatible, assessed_at=NOW)[0]
    assert "MONTHLY_VOLUME_INPUT_INCOMPATIBLE" in assessment.missing_input_codes

    contradictory = replace(
        base,
        volume_input_id=uuid4(),
        volume=MonthlyStorageVolumeInput(
                value=Decimal("42"),
                unit="m3",
                period="month",
                operational_scope=QuantityScope(scope_code="monthly-storage-volume"),
                effective_from=date(2026, 11, 1),
            ),
    )
    assessment = build_initial_assessments(contradictory, assessed_at=NOW)[0]
    assert "MONTHLY_VOLUME_SOURCE_CONTRADICTION" in assessment.missing_input_codes


def test_comparable_authorized_capacity_derives_no_conflict() -> None:
    capacity = WarehouseCapacityInput(
        site_code="WH-QA-01",
        capacity_value=Decimal("150"),
        capacity_unit="orders",
        period="month",
        available_from=date(2026, 11, 1),
    )
    assessments = build_initial_assessments(
        _sources(
            (DepartmentId.warehouse, DepartmentId.lastmile),
            capacity=capacity,
        ),
        assessed_at=NOW,
    )

    volume = assessments[0]
    assert volume.outcome == "no_conflict"
    assert volume.facts.exceeds_capacity is False
    assert volume.facts.available_capacity.value == Decimal("150")


def test_structured_returns_and_currency_conflicts_name_exact_revisions() -> None:
    sources = _sources(
        (DepartmentId.warehouse, DepartmentId.reverse),
        contents={
            DepartmentId.reverse: "Returns are processed in 24 hours.",
        },
        section_currency="EUR",
        global_currency="EUR",
    )
    assessments = build_initial_assessments(sources, assessed_at=NOW)

    returns = assessments[1]
    currency = assessments[2]
    reverse_id = next(
        row["revision_id"]
        for row in sources.revisions
        if row["department_id"] == "reverse"
    )
    assert returns.outcome == "demonstrated"
    assert returns.facts.violating_revision_ids == [reverse_id]
    assert currency.outcome == "demonstrated"
    assert currency.facts.proposal_revision_id in currency.facts.mismatched_revision_ids


def test_persisted_initial_revision_reads_creation_reason_from_jsonb() -> None:
    source = _draft(DepartmentId.lastmile, content="Original Part 2 content")

    hydrated = _department_draft_from_revision(
        {
            "review_revision_no": 1,
            "content_payload": {
                "creation_reason": "part2_import",
                "content": source.generated_content,
            },
        },
        source,
    )

    assert hydrated is source


def test_persisted_corrected_revision_rehydrates_jsonb_draft_id() -> None:
    source = _draft(DepartmentId.lastmile, content="Original Part 2 content")
    corrected_draft_id = uuid4()

    hydrated = _department_draft_from_revision(
        {
            "review_revision_no": 2,
            "created_at": NOW,
            "content_payload": {
                "creation_reason": "authorized_input",
                "draft_id": str(corrected_draft_id),
                "content": "Corrected Last Mile content",
                "commercial_terms": [
                    term.model_dump(mode="json") for term in source.commercial_terms
                ],
                "pending_commercial_items": [],
            },
        },
        source,
    )

    assert hydrated.draft_id == corrected_draft_id
    assert hydrated.generated_content == "Corrected Last Mile content"
    assert hydrated.draft_status == "complete_candidate"


def test_corrected_current_revision_replaces_only_historical_department_rollup() -> None:
    initial = {
        "review_revision_no": 1,
        "content_payload": {"creation_reason": "part2_import"},
    }
    corrected = {
        "review_revision_no": 2,
        "content_payload": {"creation_reason": "authorized_input"},
    }

    assert _requires_historical_department_rollup([initial]) is True
    assert _requires_historical_department_rollup([initial, corrected]) is False


def test_persisted_revision_without_jsonb_creation_reason_fails_closed() -> None:
    source = _draft(DepartmentId.lastmile, content="Original Part 2 content")

    with pytest.raises(ApprovalStoreError) as raised:
        _department_draft_from_revision(
            {
                "creation_reason": "part2_import",
                "review_revision_no": 1,
                "content_payload": {"content": source.generated_content},
            },
            source,
        )
    assert raised.value.code == "revision_creation_reason_invalid"
