from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from data.pipelines.rfp_intake.contracts import DepartmentId
from pipelines.rfp_approval.fan_in import (
    determine_fan_in_readiness,
    evaluate_fan_in_rows,
)
from pipelines.rfp_approval.contracts import indicative_approval_reason


RUN_ID = UUID(int=1)
TICKET_ID = UUID(int=2)
PROPOSAL_ID = UUID(int=3)
H64 = "a" * 64


def _ready_rows() -> list[dict[str, object]]:
    revisions = {
        DepartmentId.warehouse: (UUID(int=101), UUID(int=201)),
        DepartmentId.lastmile: (UUID(int=102), UUID(int=202)),
        DepartmentId.reverse: (UUID(int=103), UUID(int=203)),
    }
    proposal_sources = {
        "warehouse_revision_id": revisions[DepartmentId.warehouse][0],
        "lastmile_revision_id": revisions[DepartmentId.lastmile][0],
        "reverse_revision_id": revisions[DepartmentId.reverse][0],
    }
    rows = []
    for department, (revision_id, decision_id) in revisions.items():
        rows.append(
            {
                "run_id": RUN_ID,
                "run_ticket_id": TICKET_ID,
                "run_status": "active",
                "active_departments": ["warehouse", "lastmile", "reverse"],
                "revision_id": revision_id,
                "department_id": department.value,
                "revision_status": "approved",
                "is_current": True,
                "superseded_by_revision_id": None,
                "revision_content_hash": H64,
                "revision_content_payload": {"pending_commercial_items": []},
                "revision_validation_payload": {
                    "eligible_for_approval": True,
                    "mandatory_compliance_passed": True,
                    "unresolved_input_codes": [],
                    "blocks_commercial_completeness": False,
                    "blocking_rule_ids": [],
                    "open_conflict_ids": [],
                },
                "applied_decision_id": decision_id,
                "decision_id": decision_id,
                "decision_revision_id": revision_id,
                "decision_department_id": department.value,
                "decision_content_hash": H64,
                "decision_action": "approve",
                "decision_processing_status": "applied",
                "proposal_revision_id": PROPOSAL_ID,
                "proposal_is_current": True,
                "proposal_superseded_by_revision_id": None,
                **proposal_sources,
                "proposal_content_payload": {
                    "currency": {"status": "documented", "value": "USD"},
                    "sla": {"applicability": "applicable"},
                    "volume_discounts": [{
                        "volume_from": 50,
                        "volume_to": None,
                        "discount_percentage": 5,
                        "condition": "Authorized volume discount",
                        "status": "documented",
                        "evidence": [{"document_id": "commercial-approval"}],
                    }],
                    "source_department_revision_ids": [
                        str(item[0]) for item in revisions.values()
                    ]
                },
                "proposal_validation_payload": {
                    "contract_version": "rfp-proposal-revision-validation-v2",
                    "source_departments_approved": True,
                    "eligible_for_synthesis": True,
                    "mandatory_compliance_passed": True,
                    "unresolved_input_codes": [],
                    "blocking_rule_ids": [],
                    "open_conflict_ids": [],
                },
                "eligible_for_synthesis": True,
                "conflict_id": None,
                "conflict_status": None,
            }
        )
    return rows


def test_fan_in_is_ready_only_for_exact_approved_persisted_sources() -> None:
    readiness = evaluate_fan_in_rows(_ready_rows())

    assert readiness.ready_for_synthesis is True
    assert readiness.blockers == ()
    assert [item.department for item in readiness.approved_revisions] == [
        DepartmentId.warehouse,
        DepartmentId.lastmile,
        DepartmentId.reverse,
    ]
    assert readiness.proposal_revision_id == PROPOSAL_ID
    assert readiness.required_ticket_status == "esperando_aprobación"


def test_fan_in_blocks_a_pending_department() -> None:
    rows = _ready_rows()
    rows[2]["revision_status"] = "pending_approval"
    rows[2]["applied_decision_id"] = None
    rows[2]["decision_id"] = None

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is False
    assert "department_not_approved:reverse:pending_approval" in readiness.blockers
    assert "applied_approval_invalid:reverse" in readiness.blockers


def test_fan_in_blocks_an_obsolete_revision_and_stale_proposal_reference() -> None:
    rows = _ready_rows()
    rows[0]["is_current"] = False
    rows[0]["superseded_by_revision_id"] = UUID(int=999)
    for row in rows:
        row["warehouse_revision_id"] = UUID(int=998)

    readiness = evaluate_fan_in_rows(rows)

    assert "department_revision_obsolete:warehouse" in readiness.blockers
    assert "proposal_department_sources_mismatch" in readiness.blockers


def test_fan_in_blocks_an_open_conflict() -> None:
    rows = _ready_rows()
    for row in rows:
        row["conflict_id"] = UUID(int=500)
        row["conflict_status"] = "open"

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is False
    assert "open_arbitration_conflict" in readiness.blockers


def test_fan_in_blocks_a_decision_not_applied_to_exact_content() -> None:
    rows = _ready_rows()
    rows[1]["decision_processing_status"] = "queued"
    rows[1]["decision_content_hash"] = "b" * 64

    readiness = evaluate_fan_in_rows(rows)

    assert "applied_approval_invalid:lastmile" in readiness.blockers
    assert readiness.required_ticket_status == "esperando_aprobación"


@pytest.mark.parametrize(
    "status",
    [
        "rejected",
        "changes_requested",
        "blocked_conflict",
        "blocked_compliance",
        "blocked_input",
        "limit_reached",
    ],
)
def test_every_non_approved_business_status_blocks_fan_in(status: str) -> None:
    rows = _ready_rows()
    rows[0]["revision_status"] = status

    readiness = evaluate_fan_in_rows(rows)

    assert f"department_not_approved:warehouse:{status}" in readiness.blockers
    assert readiness.ready_for_synthesis is False


def test_pending_commercial_input_and_global_ineligibility_block_fan_in() -> None:
    rows = _ready_rows()
    rows[0]["revision_content_payload"] = {
        "pending_commercial_items": [{"code": "WAREHOUSE_CAPACITY_REQUIRED"}]
    }
    for row in rows:
        row["eligible_for_synthesis"] = False
        row["proposal_validation_payload"] = {
            **row["proposal_validation_payload"],
            "eligible_for_synthesis": False,
            "unresolved_input_codes": ["WAREHOUSE_CAPACITY_REQUIRED"],
        }

    readiness = evaluate_fan_in_rows(rows)

    assert "commercial_input_pending:warehouse" in readiness.blockers
    assert "global_validation_ineligible" in readiness.blockers


def test_indicative_approval_requires_exact_durable_acknowledgement() -> None:
    rows = _ready_rows()
    pending_code = "RETURN_PROCESSING_RATE_REQUIRED"
    rows[2]["revision_content_payload"] = {
        "pending_commercial_items": [{"code": pending_code}]
    }
    rows[2]["revision_validation_payload"] = {
        **rows[2]["revision_validation_payload"],
        "unresolved_input_codes": [pending_code],
        "blocks_commercial_completeness": True,
    }

    missing_ack = evaluate_fan_in_rows(rows)
    assert (
        "indicative_commercial_items_not_acknowledged:reverse"
        in missing_ack.blockers
    )

    rows[2]["decision_reason"] = indicative_approval_reason([pending_code])
    acknowledged = evaluate_fan_in_rows(rows)
    assert acknowledged.ready_for_synthesis is True
    assert acknowledged.blockers == ()


def _pending_discount_rows() -> list[dict[str, object]]:
    rows = _ready_rows()
    for row in rows:
        row["proposal_content_payload"]["volume_discounts"] = [{
            "volume_from": 50,
            "volume_to": None,
            "discount_percentage": None,
            "condition": (
                "Volumes above 50 m³ are eligible for negotiation; "
                "no percentage is documented."
            ),
            "status": "pending",
            "evidence": [{"document_id": "official-volume-policy"}],
        }]
    return rows


def test_non_required_pending_discount_preserves_table_without_blocking_fan_in() -> None:
    readiness = evaluate_fan_in_rows(_pending_discount_rows())

    assert readiness.ready_for_synthesis is True
    assert readiness.blockers == ()


@pytest.mark.parametrize(
    "pending_code",
    ["MONTHLY_STORAGE_VOLUME_REQUIRED", "VOLUME_DISCOUNT_APPROVAL_REQUIRED"],
)
def test_declared_pending_discount_can_be_synthesized_indicatively(pending_code: str) -> None:
    rows = _pending_discount_rows()
    for row in rows:
        row["proposal_validation_payload"] = {
            **row["proposal_validation_payload"],
            "eligible_for_synthesis": True,
            "unresolved_input_codes": [pending_code],
        }
        row["eligible_for_synthesis"] = True

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is True
    assert readiness.blockers == ()


def test_discount_table_is_always_required() -> None:
    rows = _ready_rows()
    for row in rows:
        row["proposal_content_payload"]["volume_discounts"] = []

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is False
    assert "global_commercial_terms_incomplete" in readiness.blockers


@pytest.mark.parametrize(
    "discount",
    [
        {
            "discount_percentage": 5,
            "condition": "Unresolved negotiation",
            "status": "pending",
            "evidence": [{"document_id": "policy"}],
        },
        {
            "discount_percentage": None,
            "condition": "Missing authorized percentage",
            "status": "documented",
            "evidence": [{"document_id": "policy"}],
        },
        {
            "discount_percentage": 5,
            "condition": "Missing commercial provenance",
            "status": "documented",
            "evidence": [],
        },
    ],
)
def test_contradictory_or_untraceable_discount_rows_block_fan_in(discount) -> None:
    rows = _ready_rows()
    for row in rows:
        row["proposal_content_payload"]["volume_discounts"] = [discount]

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is False
    assert "global_commercial_terms_incomplete" in readiness.blockers


def test_other_global_pending_conditions_still_block_fan_in() -> None:
    rows = _pending_discount_rows()
    for row in rows:
        row["proposal_validation_payload"] = {
            **row["proposal_validation_payload"],
            "eligible_for_synthesis": False,
            "unresolved_input_codes": ["PROPOSAL_CURRENCY_REQUIRED"],
        }
        row["eligible_for_synthesis"] = False

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is False
    assert "global_validation_ineligible" in readiness.blockers


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)


def test_postgres_coordinator_reconstructs_the_gate_with_one_statement() -> None:
    session = _Session(deepcopy(_ready_rows()))

    readiness = determine_fan_in_readiness(
        session, approval_run_id=RUN_ID, ticket_id=TICKET_ID
    )

    assert readiness.ready_for_synthesis is True
    assert len(session.statements) == 1
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "rfp_approval_runs" in sql
    assert "rfp_approval_department_revisions" in sql
    assert "rfp_human_decisions" in sql
    assert "rfp_approval_proposal_revisions" in sql
    assert "rfp_arbitration_conflicts" in sql
    assert "is_current IS true" in sql


@pytest.mark.parametrize(
    "active,currency",
    [
        (["warehouse", "reverse"], "EUR"),
        (["warehouse", "lastmile"], "USD"),
    ],
)
def test_official_two_department_scopes_do_not_require_a_third_approval(
    active, currency
) -> None:
    rows = [row for row in _ready_rows() if row["department_id"] in active]
    expected_ids = [row["revision_id"] for row in rows]
    for row in rows:
        row["active_departments"] = active
        row["proposal_content_payload"]["currency"]["value"] = currency
        row["proposal_content_payload"]["source_department_revision_ids"] = [
            str(item) for item in expected_ids
        ]
        for department in {"warehouse", "lastmile", "reverse"} - set(active):
            row[f"{department}_revision_id"] = None

    readiness = evaluate_fan_in_rows(rows)

    assert readiness.ready_for_synthesis is True
    assert len(readiness.approved_revisions) == 2
