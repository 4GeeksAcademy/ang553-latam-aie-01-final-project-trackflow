import json
from datetime import datetime, timezone
from uuid import UUID

import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.hashing import canonical_hash
from pipelines.rfp_approval.contracts import PersistedFinalDocument
from pipelines.rfp_approval.fan_in import (
    ApprovedDepartmentRevision,
    FanInReadiness,
)
from services.rfp_approval.finalization import (
    FinalizationCommand,
    FinalizationError,
    finalize_approved_proposal,
    get_final_document_locator,
)


NOW = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
RUN_ID, TICKET_ID, RESPONSE_ID = UUID(int=1), UUID(int=2), UUID(int=3)
PROPOSAL_ID, DOCUMENT_ID = UUID(int=4), UUID(int=5)
WAREHOUSE_REVISION, LASTMILE_REVISION = UUID(int=10), UUID(int=11)
WAREHOUSE_DECISION, LASTMILE_DECISION = UUID(int=20), UUID(int=21)
H64 = "a" * 64
EVIDENCE = {"document_id": "rfp", "section_id": "commercial", "evidence_hash": H64}


class _Result:
    def __init__(self, *, one=None, many=None, scalar=None, rowcount=None):
        self.one = one
        self.many = many or []
        self.scalar = scalar
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.one

    def all(self):
        return self.many

    def scalars(self):
        return self

    def scalar_one(self):
        return self.scalar


class _Session:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def in_transaction(self):
        return True

    def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def _readiness():
    return FanInReadiness(
        ready_for_synthesis=True,
        blockers=(),
        approved_revisions=(
            ApprovedDepartmentRevision(
                DepartmentId.warehouse, WAREHOUSE_REVISION, WAREHOUSE_DECISION
            ),
            ApprovedDepartmentRevision(
                DepartmentId.lastmile, LASTMILE_REVISION, LASTMILE_DECISION
            ),
        ),
        proposal_revision_id=PROPOSAL_ID,
    )


def test_final_document_locator_is_ticket_and_environment_scoped() -> None:
    session = _Session(
        [
            _Result(
                one={
                    "ticket_id": TICKET_ID,
                    "approval_run_id": RUN_ID,
                    "final_document_id": DOCUMENT_ID,
                }
            )
        ]
    )

    locator = get_final_document_locator(
        session,
        ticket_id=TICKET_ID,
        environment="qa",
    )

    assert locator is not None
    assert locator.ticket_id == TICKET_ID
    assert locator.approval_run_id == RUN_ID
    assert locator.final_document_id == DOCUMENT_ID
    compiled = str(session.statements[0])
    assert "rfp_approval_runs.environment" in compiled
    assert "rfp_approval_runs.status" in compiled
    assert "rfp_final_documents.ticket_id" in compiled


def test_final_document_locator_preserves_absence() -> None:
    session = _Session([_Result(one=None)])

    assert get_final_document_locator(
        session,
        ticket_id=TICKET_ID,
        environment="qa",
    ) is None


def _rows():
    run = {
        "approval_run_id": RUN_ID,
        "ticket_id": TICKET_ID,
        "response_run_id": RESPONSE_ID,
        "active_departments": ["warehouse", "lastmile"],
        "status": "active",
    }
    ticket = {"ticket_id": TICKET_ID, "status": "esperando_aprobación"}
    revisions = [
        {
            "revision_id": WAREHOUSE_REVISION,
            "department_id": "warehouse",
            "content_hash": H64,
            "content_payload": {"content": "Approved warehouse service."},
            "applied_decision_id": WAREHOUSE_DECISION,
        },
        {
            "revision_id": LASTMILE_REVISION,
            "department_id": "lastmile",
            "content_hash": H64,
            "content_payload": {"content": "Approved last-mile service."},
            "applied_decision_id": LASTMILE_DECISION,
        },
    ]
    proposal = {
        "proposal_revision_id": PROPOSAL_ID,
        "content_hash": H64,
        "content_payload": {
            "currency": {
                "value": "USD",
                "status": "documented",
                "evidence": [EVIDENCE],
            },
            "sla": {
                "applicability": "applicable",
                "percentage": 95.0,
                "measurement_period": "monthly",
                "scope": ["lastmile"],
                "exclusions": [],
                "evidence": [EVIDENCE],
            },
            "volume_discounts": [
                {
                    "volume_from": 1000.0,
                    "volume_to": None,
                    "discount_percentage": 5.0,
                    "condition": "Documented monthly order volume",
                    "status": "documented",
                    "evidence": [EVIDENCE],
                }
            ],
            "authorized_input_ids": [],
        },
    }
    return run, ticket, revisions, proposal


def _command():
    return FinalizationCommand(
        approval_run_id=RUN_ID,
        ticket_id=TICKET_ID,
        final_document_id=DOCUMENT_ID,
        execution_id=UUID(int=6),
        generated_at=NOW,
    )


def _successful_session():
    run, ticket, revisions, proposal = _rows()
    return _Session(
        [
            _Result(one=run),
            _Result(one=ticket),
            _Result(one={"client_country_code": "US"}),
            _Result(one=None),
            _Result(many=revisions),
            _Result(one=proposal),
            _Result(many=[]),
            _Result(many=[]),
            _Result(many=[]),
            _Result(),
            _Result(rowcount=1),
            _Result(rowcount=1),
            _Result(scalar=1),
            _Result(),
            _Result(),
            _Result(),
        ]
    )


def test_complete_document_is_built_from_exact_approved_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()

    result = finalize_approved_proposal(session, command=_command())

    assert result.replayed is False
    assert result.document.payload.manifest.department_revision_ids == [
        WAREHOUSE_REVISION,
        LASTMILE_REVISION,
    ]
    assert result.document.payload.manifest.approval_decision_ids == [
        WAREHOUSE_DECISION,
        LASTMILE_DECISION,
    ]
    assert result.document.payload.global_terms.currency.value == "USD"
    assert "Approved warehouse service" in result.document.markdown
    assert "Approved last-mile service" in result.document.markdown
    assert "| Volume range (m³/month) | Status | Discount | Condition |" in result.document.markdown
    assert "| 1000.0 and above | Documented | 5.0% |" in result.document.markdown
    assert "token" not in result.document.markdown.lower()
    assert "oidc" not in result.document.markdown.lower()


def test_final_document_manifest_preserves_authorized_input_references(monkeypatch) -> None:
    authorized_input_id = UUID(int=30)
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    session.results[5].one["content_payload"]["authorized_input_ids"] = [
        str(authorized_input_id)
    ]

    result = finalize_approved_proposal(session, command=_command())

    assert result.document.payload.manifest.authorized_input_ids == [
        authorized_input_id
    ]


def test_optional_pending_discount_is_not_rendered_as_an_approved_percentage(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    proposal = session.results[5].one
    proposal["content_payload"]["volume_discounts"] = [
        {
            "volume_from": 50.0,
            "volume_to": None,
            "discount_percentage": None,
            "condition": (
                "Volumes above 50 m³ are eligible for negotiation; "
                "no percentage is documented."
            ),
            "status": "pending",
            "evidence": [EVIDENCE],
        }
    ]

    result = finalize_approved_proposal(session, command=_command())

    assert "Pending — no percentage authorized" in result.document.markdown
    assert "Not documented" in result.document.markdown
    assert "None%" not in result.document.markdown
    assert "| 0% |" not in result.document.markdown


def test_declared_pending_terms_produce_an_explicitly_non_binding_document(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    session.results[4].many[1]["content_payload"]["pending_commercial_items"] = [
        {
            "code": "LAST_MILE_RATE_REQUIRED",
            "description": "Client-specific delivery price remains pending.",
            "owner": "Carlos Vega",
        }
    ]
    result = finalize_approved_proposal(session, command=_command())

    assert result.document.payload.title.startswith("TrackFlow indicative")
    assert "Indicative, non-binding proposal" in result.document.markdown
    assert "not negotiated commitments" in result.document.markdown


def test_global_pending_inputs_are_rendered_once_and_remain_separate(
    monkeypatch,
) -> None:
    reverse_readiness = FanInReadiness(
        ready_for_synthesis=True,
        blockers=(),
        approved_revisions=(
            ApprovedDepartmentRevision(
                DepartmentId.warehouse, WAREHOUSE_REVISION, WAREHOUSE_DECISION
            ),
            ApprovedDepartmentRevision(
                DepartmentId.reverse, LASTMILE_REVISION, LASTMILE_DECISION
            ),
        ),
        proposal_revision_id=PROPOSAL_ID,
    )
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: reverse_readiness,
    )
    session = _successful_session()
    session.results[0].one["active_departments"] = ["warehouse", "reverse"]
    session.results[4].many[1]["department_id"] = "reverse"
    session.results[4].many[1]["content_payload"]["pending_commercial_items"] = [
        {
            "code": "RETURN_PROCESSING_RATE_REQUIRED",
            "description": "Client-specific return processing rate remains pending.",
            "owner": "Sofía Ramos",
        }
    ]
    session.results[4].many[1]["content_payload"]["content"] = (
        "Approved reverse logistics service. Pending commercial input: "
        "RETURN_PROCESSING_RATE_REQUIRED."
    )
    session.results[5].one["validation_payload"] = {
        "unresolved_input_codes": [
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
            "MONTHLY_STORAGE_VOLUME_REQUIRED",
        ]
    }

    result = finalize_approved_proposal(session, command=_command())

    assert result.document.payload.global_terms.unresolved_input_codes == [
        "MONTHLY_STORAGE_VOLUME_REQUIRED"
    ]
    assert result.document.markdown.count("MONTHLY_STORAGE_VOLUME_REQUIRED") == 1
    assert "Average monthly storage volume must be confirmed" in (
        result.document.markdown
    )
    assert result.document.markdown.count("RETURN_PROCESSING_RATE_REQUIRED") == 1
    assert "RETURN_PROCESSING_RATE_REQUIRED" not in (
        result.document.payload.global_terms.unresolved_input_codes
    )
    assert "Pending — no percentage authorized" not in result.document.markdown
    serialized = result.document.model_dump(mode="json")
    rehydrated = PersistedFinalDocument.model_validate_json(
        json.dumps(serialized, ensure_ascii=False)
    )
    assert rehydrated.payload_hash == canonical_hash(rehydrated.payload)
    assert rehydrated.payload_hash == result.document.payload_hash


def test_unknown_global_pending_input_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    session.results[5].one["validation_payload"] = {
        "unresolved_input_codes": ["RETURN_PROCESSING_RATE_REQUIRED"]
    }

    with pytest.raises(ValueError, match="unresolved_input_codes"):
        finalize_approved_proposal(session, command=_command())


def test_ticket_cas_failure_aborts_before_run_close_and_events(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    session.results[10] = _Result(rowcount=0)

    with pytest.raises(FinalizationError) as raised:
        finalize_approved_proposal(session, command=_command())

    assert raised.value.code == "ticket_close_cas_failed"
    assert len(session.results) == 5


def test_wrong_country_currency_is_rejected_before_document_insert(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    session = _successful_session()
    session.results[2] = _Result(one={"client_country_code": "ES"})

    with pytest.raises(FinalizationError) as raised:
        finalize_approved_proposal(session, command=_command())

    assert raised.value.code == "proposal_currency_country_mismatch"
    assert len(session.results) == 7


def test_legitimate_replay_returns_the_single_persisted_document(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.rfp_approval.finalization.determine_fan_in_readiness",
        lambda *args, **kwargs: _readiness(),
    )
    first = finalize_approved_proposal(_successful_session(), command=_command())
    document = first.document
    assert "unresolved_input_codes" not in document.payload.model_dump(
        mode="json"
    )["global_terms"]
    existing = {
        "final_document_id": document.final_document_id,
        "payload": document.payload.model_dump(mode="json"),
        "payload_hash": document.payload_hash,
        "markdown": document.markdown,
        "markdown_hash": document.markdown_hash,
        "storage_reference": document.storage_reference,
        "generated_at": document.generated_at,
    }
    run, ticket, _, _ = _rows()
    run["status"] = "completed"
    ticket["status"] = "terminado"
    replay_session = _Session(
        [
            _Result(one=run),
            _Result(one=ticket),
            _Result(one={"client_country_code": "US"}),
            _Result(one=existing),
        ]
    )

    replay = finalize_approved_proposal(replay_session, command=_command())

    assert replay.replayed is True
    assert replay.document == document
    assert replay_session.results == []


def test_explicit_transaction_is_required_before_any_read() -> None:
    session = _Session([])
    session.in_transaction = lambda: False

    with pytest.raises(FinalizationError) as raised:
        finalize_approved_proposal(session, command=_command())

    assert raised.value.code == "explicit_transaction_required"
    assert session.statements == []
