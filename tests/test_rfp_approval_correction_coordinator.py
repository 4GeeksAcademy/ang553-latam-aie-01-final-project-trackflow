from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import (
    DepartmentDraft,
    RequestedAspect,
    RFPResponseStart,
)
from data.pipelines.rfp_response.evaluators import (
    evaluate_department,
    inspect_department_safety,
)
from data.pipelines.rfp_response.generators import generate_department_draft
from data.pipelines.rfp_response.hashing import canonical_hash, canonical_value
from pipelines.rfp_approval.contracts import (
    ClientPriceQuoteInput,
    VolumeCapacityFacts,
    conflict_assessment_hash,
)
from services.rfp_approval.correction_coordinator import (
    _apply_price_inputs,
    _authorized_correction_context,
    _load_selected_inputs,
    _rehydrate_client_price_quote,
)
from services.rfp_approval.store import ApprovalStoreError


H64 = "a" * 64


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        return _Rows(self.rows)


def _persisted_quote_row(*, principal_id=None) -> tuple[dict[str, object], object]:
    quote = ClientPriceQuoteInput(
        department=DepartmentId.lastmile,
        service_code="last_mile_rate",
        amount=Decimal("12.5000"),
        currency="USD",
        unit="shipment",
        effective_from=date(2026, 1, 1),
    )
    identity = SimpleNamespace(
        environment="qa", principal_id=principal_id or uuid4()
    )
    row = {
        "input_id": uuid4(),
        "approval_run_id": uuid4(),
        "ticket_id": uuid4(),
        "environment": "qa",
        "department_scope": "lastmile",
        "actor_principal_id": identity.principal_id,
        "input_kind": "client_price_quote",
        "payload": canonical_value(quote),
        "payload_hash": canonical_hash(
            {"payload": quote, "provenance": {"evidence_id": str(uuid4())}}
        ),
        "provenance": {
            "evidence_type": "approved_quote",
            "evidence_id": str(uuid4()),
            "source_payload_hash": "b" * 64,
        },
    }
    return row, identity


def test_persisted_quote_round_trip_preserves_canonical_value_and_scale() -> None:
    row, identity = _persisted_quote_row()
    original = deepcopy(row)

    restored = _rehydrate_client_price_quote(row["payload"])
    selected = _load_selected_inputs(
        _Session([row]),
        approval_run_id=row["approval_run_id"],
        ticket_id=row["ticket_id"],
        department="lastmile",
        input_ids=(row["input_id"],),
        identity=identity,
        requested_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
    )

    assert restored.amount == Decimal("12.5000")
    assert restored.amount.as_tuple().exponent == -4
    assert restored.effective_from == date(2026, 1, 1)
    assert restored.department is DepartmentId.lastmile
    assert canonical_value(restored) == row["payload"]
    assert selected == [(row["input_id"], row["payload_hash"], restored)]
    assert row == original


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("amount", 12.5),
        ("amount", True),
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", "not-a-decimal"),
        ("effective_from", "2026-02-30"),
        ("department", "customer_success"),
    ],
)
def test_persisted_quote_rejects_noncanonical_or_malformed_values(
    field_name: str, value: object
) -> None:
    row, identity = _persisted_quote_row()
    row["payload"][field_name] = value

    with pytest.raises(ApprovalStoreError) as raised:
        _load_selected_inputs(
            _Session([row]),
            approval_run_id=row["approval_run_id"],
            ticket_id=row["ticket_id"],
            department="lastmile",
            input_ids=(row["input_id"],),
            identity=identity,
            requested_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        )

    assert raised.value.code == "authorized_input_payload_invalid"


def test_persisted_quote_rejects_missing_or_additional_fields() -> None:
    row, identity = _persisted_quote_row()
    del row["payload"]["unit"]
    row["payload"]["unexpected"] = "not-authorized"

    with pytest.raises(ApprovalStoreError) as raised:
        _load_selected_inputs(
            _Session([row]),
            approval_run_id=row["approval_run_id"],
            ticket_id=row["ticket_id"],
            department="lastmile",
            input_ids=(row["input_id"],),
            identity=identity,
            requested_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        )

    assert raised.value.code == "authorized_input_payload_invalid"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("actor", "authorized_input_not_applicable_to_correction"),
        ("department", "authorized_input_not_applicable_to_correction"),
        ("missing", "authorized_input_not_found_or_scope_mismatch"),
    ],
)
def test_selected_quote_remains_bound_to_actor_and_scope(
    mutation: str, expected_code: str
) -> None:
    row, identity = _persisted_quote_row()
    rows = [row]
    if mutation == "actor":
        row["actor_principal_id"] = uuid4()
    elif mutation == "department":
        row["department_scope"] = "reverse"
    else:
        rows = []

    with pytest.raises(ApprovalStoreError) as raised:
        _load_selected_inputs(
            _Session(rows),
            approval_run_id=row["approval_run_id"],
            ticket_id=row["ticket_id"],
            department="lastmile",
            input_ids=(row["input_id"],),
            identity=identity,
            requested_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        )

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "requested_at",
    [
        datetime(2025, 12, 31, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    ],
)
def test_selected_quote_must_be_effective_when_correction_is_requested(
    requested_at: datetime,
) -> None:
    row, identity = _persisted_quote_row()
    row["payload"]["effective_until"] = "2026-12-31"

    with pytest.raises(ApprovalStoreError) as raised:
        _load_selected_inputs(
            _Session([row]),
            approval_run_id=row["approval_run_id"],
            ticket_id=row["ticket_id"],
            department="lastmile",
            input_ids=(row["input_id"],),
            identity=identity,
            requested_at=requested_at,
        )

    assert raised.value.code == "authorized_input_not_effective"


def _predecessor(*, additional_pending: bool = False) -> dict[str, object]:
    pending = [
        {
            "code": "LAST_MILE_RATE_REQUIRED",
            "description": "Approved price required.",
            "owner": "Carlos Vega",
            "blocks_commercial_completeness": True,
        }
    ]
    if additional_pending:
        pending.append(
            {
                "code": "DELIVERY_COUNTRY_REQUIRED",
                "description": "Country required.",
                "owner": "Carlos Vega",
                "blocks_commercial_completeness": True,
            }
        )
    return {
        "department_id": "lastmile",
        "status": "blocked_input",
        "content_payload": {
            "content": "Persisted Part 2 content.",
            "commercial_terms": [
                {
                    "term_id": "last_mile_rate",
                    "status": "pending",
                    "value": None,
                    "currency": None,
                    "evidence": [],
                }
            ],
            "pending_commercial_items": pending,
            "evidence": [],
        },
    }


def _selected(*, service_code: str = "last_mile_rate"):
    return [
        (
            uuid4(),
            H64,
            ClientPriceQuoteInput(
                department=DepartmentId.lastmile,
                service_code=service_code,
                amount=Decimal("12.50"),
                currency="USD",
                unit="shipment",
                effective_from=date(2026, 1, 1),
            ),
        )
    ]


def test_price_correction_replaces_only_the_authorized_pending_term() -> None:
    content, terms, pending, evidence = _apply_price_inputs(
        predecessor=_predecessor(), selected=_selected()
    )

    assert terms[0].status == "documented"
    assert terms[0].value == "12.5 USD per shipment"
    assert terms[0].currency == "USD"
    assert pending == []
    assert evidence == [terms[0].evidence[0].model_dump(mode="json")]
    assert "Authorized commercial correction" in content
    assert "12.5 USD per shipment" in content


def test_correction_preserves_unresolved_inputs_as_explicit_blockers() -> None:
    _content, _terms, pending, _evidence = _apply_price_inputs(
        predecessor=_predecessor(additional_pending=True), selected=_selected()
    )

    assert [item["code"] for item in pending] == ["DELIVERY_COUNTRY_REQUIRED"]


def test_correction_rejects_an_input_for_a_different_service() -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        _apply_price_inputs(
            predecessor=_predecessor(), selected=_selected(service_code="other_rate")
        )

    assert raised.value.code == "authorized_input_service_code_mismatch"


def _evaluated_correction(department: DepartmentId):
    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    ticket_id = uuid4()
    run_id = uuid4()
    aspect = {
        DepartmentId.lastmile: (
            "Client requests last-mile carrier coverage in the United States.",
            "Describe the available carrier coverage.",
        ),
        DepartmentId.reverse: (
            "Client requests a documented return inspection process.",
            "Describe return inspection and classification.",
        ),
    }[department]
    request = RFPResponseStart(
        ticket_id=ticket_id,
        run_id=run_id,
        source_handoff_hash=H64,
        summary_for_sales="Synthetic QA correction request.",
        key_aspects=[RequestedAspect(
            aspect_id=f"{department.value}:01:1-2",
            department=department,
            fact=aspect[0],
            requested_action=aspect[1],
            line_start=1,
            line_end=2,
        )],
        selected_departments=[department],
        client_country_code="US",
        operation_countries=["US"],
        proposal_currency="USD",
        average_monthly_volume_m3=20.0,
        customer_managed_carrier=False,
        language="en",
    )
    source = generate_department_draft(
        request,
        department=department,
        iteration_count=1,
        generated_at=now,
    )
    term_id = (
        "last_mile_rate"
        if department == DepartmentId.lastmile
        else "return_processing_rate"
    )
    quote = ClientPriceQuoteInput(
        department=department,
        service_code=term_id,
        amount=Decimal("12.5000"),
        currency="USD",
        unit="shipment",
        effective_from=date(2026, 1, 1),
    )
    selected = [(uuid4(), "b" * 64, quote)]
    predecessor = {
        "department_id": department.value,
        "status": "blocked_input",
        "content_payload": {
            "content": source.generated_content,
            "commercial_terms": [
                item.model_dump(mode="python") for item in source.commercial_terms
            ],
            "pending_commercial_items": [
                item.model_dump(mode="python")
                for item in source.pending_commercial_items
            ],
            "evidence": [],
        },
    }
    content, terms, pending, _evidence = _apply_price_inputs(
        predecessor=predecessor,
        selected=selected,
    )
    draft = DepartmentDraft.model_validate({
        **source.model_dump(mode="python"),
        "draft_id": uuid4(),
        "generated_content": content,
        "commercial_terms": terms,
        "pending_commercial_items": pending,
        "draft_status": "complete_candidate",
    })
    return request, draft, _authorized_correction_context(selected), now


@pytest.mark.parametrize("department", [DepartmentId.lastmile, DepartmentId.reverse])
def test_authorized_commercial_delta_passes_real_safety_and_compliance(
    department: DepartmentId,
) -> None:
    request, draft, context, now = _evaluated_correction(department)

    inspection = inspect_department_safety(
        request,
        draft,
        inspected_at=now,
        authorized_corrections=context,
    )
    evaluation, _feedback = evaluate_department(
        request,
        draft,
        evaluated_at=now,
        authorized_corrections=context,
    )

    assert inspection.status == "verified_safe"
    assert inspection.reason_codes == []
    assert evaluation.relevance.passed is True
    assert evaluation.compliance.passed is True
    assert evaluation.blocking_rule_ids == []
    assert evaluation.blocks_commercial_completeness is False


@pytest.mark.parametrize("mutation", ["hash", "amount", "extra-content", "department"])
def test_authorized_commercial_delta_rejects_any_divergence(mutation: str) -> None:
    request, draft, context, now = _evaluated_correction(DepartmentId.lastmile)
    if mutation == "hash":
        context = (replace(context[0], payload_hash="c" * 64),)
    elif mutation == "department":
        context = (replace(context[0], department=DepartmentId.reverse),)
    elif mutation == "extra-content":
        draft = DepartmentDraft.model_validate({
            **draft.model_dump(mode="python"),
            "generated_content": f"{draft.generated_content}\nUnverified extra promise.",
        })
    else:
        term = draft.commercial_terms[-1].model_copy(update={"value": "13 USD per shipment"})
        draft = DepartmentDraft.model_validate({
            **draft.model_dump(mode="python"),
            "commercial_terms": [*draft.commercial_terms[:-1], term],
        })

    inspection = inspect_department_safety(
        request,
        draft,
        inspected_at=now,
        authorized_corrections=context,
    )
    evaluation, _feedback = evaluate_department(
        request,
        draft,
        evaluated_at=now,
        authorized_corrections=context,
    )

    assert inspection.status == "blocked"
    assert evaluation.compliance.passed is False
    assert "TF-CONFIDENTIAL-001" in evaluation.blocking_rule_ids


def test_independent_compliance_rule_still_blocks_authorized_correction() -> None:
    request, draft, context, now = _evaluated_correction(DepartmentId.reverse)
    draft = DepartmentDraft.model_validate({
        **draft.model_dump(mode="python"),
        "generated_content": (
            f"{draft.generated_content}\nReturns will be processed in 24 hours."
        ),
    })

    evaluation, _feedback = evaluate_department(
        request,
        draft,
        evaluated_at=now,
        authorized_corrections=context,
    )

    assert evaluation.compliance.passed is False
    assert "TF-RETURN-048" in evaluation.blocking_rule_ids
    assert "TF-CONFIDENTIAL-001" in evaluation.blocking_rule_ids


def test_ordinary_part2_evaluation_preserves_declared_pending_input_without_correction_context() -> None:
    request, _draft, _context, now = _evaluated_correction(DepartmentId.lastmile)
    source = generate_department_draft(
        request,
        department=DepartmentId.lastmile,
        iteration_count=1,
        generated_at=now,
    )

    inspection = inspect_department_safety(request, source, inspected_at=now)
    evaluation, _feedback = evaluate_department(request, source, evaluated_at=now)

    assert inspection.status == "verified_safe"
    assert evaluation.compliance.passed is True
    assert evaluation.overall_pass is False
    assert evaluation.terminal_status == "needs_human_input"
    assert evaluation.blocking_rule_ids == []
    assert {item.code for item in source.pending_commercial_items} == {
        "LAST_MILE_RATE_REQUIRED"
    }
    commercial_warning = next(
        item
        for item in evaluation.compliance.findings
        if item.rule_id == "TF-COMMERCIAL-001"
    )
    assert commercial_warning.blocking is False
    assert commercial_warning.requires_human_input is True


def test_assessment_hash_is_bound_to_the_content_actually_rechecked() -> None:
    facts = VolumeCapacityFacts(
        applicable=False,
        comparison_status="not_applicable",
        exceeds_capacity=None,
    )
    first = conflict_assessment_hash(
        trigger="volume-vs-capacity",
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
        assessed_content_hashes={"lastmile": "a" * 64, "proposal": "b" * 64},
    )
    corrected = conflict_assessment_hash(
        trigger="volume-vs-capacity",
        facts=facts,
        outcome="no_conflict",
        missing_input_codes=[],
        assessed_content_hashes={"lastmile": "c" * 64, "proposal": "b" * 64},
    )

    assert first != corrected
