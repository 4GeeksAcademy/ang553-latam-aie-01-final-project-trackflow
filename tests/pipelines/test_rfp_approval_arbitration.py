from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.arbitration import (
    ArbitrationWorkflow,
    ArbitrationWorkflowError,
    revision_limit_allows_correction,
)
from pipelines.rfp_approval.contracts import (
    ArbitrationConflict,
    ArbitrationTarget,
    ConflictAssessment,
    CurrencyMismatchFacts,
    QuantityFact,
    QuantityScope,
    ReturnSLAViolation,
    ReturnsSLAFacts,
    VolumeCapacityFacts,
    conflict_assessment_hash,
    arbitration_conflict_fingerprint,
)
from services.rfp_approval.arbitration import ConflictRecord


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def _assessment(facts, *, missing=None) -> ConflictAssessment:
    if isinstance(facts, VolumeCapacityFacts):
        outcome = (
            "insufficient_information"
            if facts.comparison_status != "comparable"
            else "demonstrated" if facts.exceeds_capacity else "no_conflict"
        )
    else:
        outcome = "demonstrated" if facts.conflict_demonstrated else "no_conflict"
    missing = missing or []
    return ConflictAssessment(
        assessment_id=UUID(int=90),
        trigger=facts.trigger,
        facts=facts,
        outcome=outcome,
        missing_input_codes=missing,
        assessment_hash=conflict_assessment_hash(
            trigger=facts.trigger,
            facts=facts,
            outcome=outcome,
            missing_input_codes=missing,
        ),
        assessed_at=NOW,
    )


def _quantity(value: str, *, unit="m3") -> QuantityFact:
    return QuantityFact(
        value=Decimal(value),
        dimension="volume",
        unit=unit,
        period="month",
        operational_scope=QuantityScope(location_code="US-LAX"),
    )


def test_volume_capacity_routes_only_comparable_demonstrated_conflict_to_miguel() -> None:
    facts = VolumeCapacityFacts(
        required_volume=_quantity("150"),
        available_capacity=_quantity("100"),
        capacity_input_id=UUID(int=1),
        comparison_status="comparable",
        exceeds_capacity=True,
    )
    result = ArbitrationWorkflow().invoke(
        assessment=_assessment(facts),
        targets=[
            ArbitrationTarget(
                department=DepartmentId.lastmile,
                department_revision_id=UUID(int=2),
            )
        ],
        thread_id="rfp-ticket:arbitration:volume-vs-capacity",
    )

    assert result["required_arbitrators"] == ["Miguel Torres"]
    assert result["recommendation"]["requires_lastmile_volume_cost_review"] is True
    assert result["resulting_status"] == "pending_arbitration"


def test_incomparable_capacity_requests_authorized_input_without_arbitrator() -> None:
    facts = VolumeCapacityFacts(
        required_volume=_quantity("100", unit="m3"),
        available_capacity=_quantity("80", unit="pallet"),
        capacity_input_id=UUID(int=1),
        comparison_status="incompatible_units",
        exceeds_capacity=None,
    )
    result = ArbitrationWorkflow().invoke(
        assessment=_assessment(facts, missing=["CAPACITY_NORMALIZATION_REQUIRED"]),
        targets=[
            ArbitrationTarget(
                department=DepartmentId.warehouse,
                department_revision_id=UUID(int=2),
            )
        ],
        thread_id="rfp-ticket:arbitration:volume-vs-capacity",
    )

    assert result["outcome"] == "request_input"
    assert result["required_arbitrators"] == []
    assert result["resulting_status"] == "blocked_input"


@pytest.mark.parametrize(
    "departments,arbitrators",
    [
        ([DepartmentId.reverse], ["Sofía Ramos"]),
        ([DepartmentId.warehouse], ["Miguel Torres"]),
        (
            [DepartmentId.reverse, DepartmentId.lastmile],
            ["Sofía Ramos", "Miguel Torres"],
        ),
    ],
)
def test_returns_sla_routes_each_offending_scope_to_its_fixed_authority(
    departments, arbitrators
) -> None:
    revisions = [UUID(int=10 + index) for index in range(len(departments))]
    facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(department_revision_id=item, sla_hours=Decimal("24"))
            for item in revisions
        ],
        violating_revision_ids=revisions,
        conflict_demonstrated=True,
    )
    targets = [
        ArbitrationTarget(department=department, department_revision_id=revision)
        for department, revision in zip(departments, revisions, strict=True)
    ]

    result = ArbitrationWorkflow().invoke(
        assessment=_assessment(facts),
        targets=targets,
        thread_id="rfp-ticket:arbitration:returns-sla-breach",
    )

    assert result["required_arbitrators"] == arbitrators
    assert result["recommendation"]["minimum_returns_sla_hours"] == 48


def test_currency_mismatch_requires_miguel_and_never_performs_conversion() -> None:
    facts = CurrencyMismatchFacts(
        client_country="ES",
        expected_currency="EUR",
        proposal_revision_id=UUID(int=30),
        proposal_currency="USD",
        monetary_terms=[],
        mismatched_revision_ids=[UUID(int=30)],
        conflict_demonstrated=True,
    )
    result = ArbitrationWorkflow().invoke(
        assessment=_assessment(facts),
        targets=[ArbitrationTarget(proposal_revision_id=UUID(int=30))],
        thread_id="rfp-ticket:arbitration:currency-mismatch",
    )

    assert result["required_arbitrators"] == ["Miguel Torres"]
    assert result["recommendation"]["expected_currency"] == "EUR"
    assert result["recommendation"]["conversion_requires_authorized_input"] is True


def test_revision_limit_is_an_effective_non_looping_barrier() -> None:
    assert revision_limit_allows_correction(2) is True
    assert revision_limit_allows_correction(3) is False
    with pytest.raises(ArbitrationWorkflowError):
        revision_limit_allows_correction(4)


def test_arbitration_is_a_real_named_graph_node() -> None:
    assert "arbitrate_conflict" in ArbitrationWorkflow().graph.nodes


def test_langgraph_node_uses_persistence_backend_and_replays_same_conflict() -> None:
    facts = CurrencyMismatchFacts(
        client_country="ES",
        expected_currency="EUR",
        proposal_revision_id=UUID(int=30),
        proposal_currency="USD",
        monetary_terms=[],
        mismatched_revision_ids=[UUID(int=30)],
        conflict_demonstrated=True,
    )
    assessment = _assessment(facts)
    targets = [ArbitrationTarget(proposal_revision_id=UUID(int=30))]
    conflict = ArbitrationConflict(
        conflict_id=UUID(int=91),
        approval_run_id=UUID(int=92),
        trigger=facts.trigger,
        facts=facts,
        affected_targets=targets,
        status="demonstrated",
        missing_input_codes=[],
        fingerprint=arbitration_conflict_fingerprint(
            trigger=facts.trigger, facts=facts, affected_targets=targets
        ),
        detected_at=NOW,
    )

    class Backend:
        def __init__(self):
            self.calls = []

        def persist(self, **kwargs):
            self.calls.append(kwargs)
            return ConflictRecord(
                assessment.assessment_id,
                conflict.conflict_id,
                assessment.outcome,
                "open",
                len(self.calls) > 1,
            )

    backend = Backend()
    workflow = ArbitrationWorkflow(backend=backend)
    arguments = dict(
        approval_run_id=conflict.approval_run_id,
        ticket_id=UUID(int=93),
        assessment=assessment,
        assessed_content_hashes={"proposal": "a" * 64},
        conflict=conflict,
        targets=targets,
        execution_id=UUID(int=94),
        thread_id="rfp-ticket:arbitration:currency-mismatch",
    )

    first = workflow.invoke_persisted(**arguments)
    second = workflow.invoke_persisted(**arguments)

    assert first["conflict_record"]["replayed"] is False
    assert second["conflict_record"]["replayed"] is True
    assert len(backend.calls) == 2


def test_persisted_mixed_returns_breach_is_split_by_fixed_authority() -> None:
    reverse_id, warehouse_id = UUID(int=101), UUID(int=102)
    facts = ReturnsSLAFacts(
        observed=[
            ReturnSLAViolation(
                department_revision_id=reverse_id, sla_hours=Decimal("24")
            ),
            ReturnSLAViolation(
                department_revision_id=warehouse_id, sla_hours=Decimal("36")
            ),
        ],
        violating_revision_ids=[reverse_id, warehouse_id],
        conflict_demonstrated=True,
    )
    assessment = _assessment(facts)
    targets = [
        ArbitrationTarget(
            department=DepartmentId.reverse,
            department_revision_id=reverse_id,
        ),
        ArbitrationTarget(
            department=DepartmentId.warehouse,
            department_revision_id=warehouse_id,
        ),
    ]
    conflict = ArbitrationConflict(
        conflict_id=UUID(int=103),
        approval_run_id=UUID(int=104),
        trigger=facts.trigger,
        facts=facts,
        affected_targets=targets,
        status="demonstrated",
        missing_input_codes=[],
        fingerprint=arbitration_conflict_fingerprint(
            trigger=facts.trigger, facts=facts, affected_targets=targets
        ),
        detected_at=NOW,
    )

    class Backend:
        def __init__(self):
            self.calls = []

        def persist(self, **kwargs):
            self.calls.append(kwargs)
            split = kwargs["conflict"]
            return ConflictRecord(
                assessment.assessment_id,
                split.conflict_id,
                assessment.outcome,
                "open",
                False,
            )

    backend = Backend()
    result = ArbitrationWorkflow(backend=backend).invoke_persisted(
        approval_run_id=conflict.approval_run_id,
        ticket_id=UUID(int=105),
        assessment=assessment,
        assessed_content_hashes={"reverse": "a" * 64, "warehouse": "b" * 64},
        conflict=conflict,
        targets=targets,
        execution_id=UUID(int=106),
        thread_id="rfp-ticket:arbitration:returns-sla-breach",
    )

    assert len(backend.calls) == 2
    assert [
        [target.department for target in call["conflict"].affected_targets]
        for call in backend.calls
    ] == [[DepartmentId.reverse], [DepartmentId.warehouse]]
    assert len({call["conflict"].conflict_id for call in backend.calls}) == 2
    assert len(result["conflict_records"]) == 2
