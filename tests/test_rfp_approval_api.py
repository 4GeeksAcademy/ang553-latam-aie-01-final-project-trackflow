from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from data.pipelines.rfp_intake.contracts import DepartmentId
from services.api.auth_models import Role, UserInDB
from services.api.rfp_approval_auth import (
    RFPApprovalAuthentication,
    _verified_local,
    _verified_oidc,
)
from services.api.routes import rfp as routes
from services.rfp_approval.http_contracts import (
    ApprovalDecisionRequest,
    ApprovalRunInitializeRequest,
    ApprovalWorkflowStartRequest,
    AuthorizedBusinessInputRequest,
    DepartmentCorrectionRequest,
)
from pipelines.rfp_approval.contracts import (
    BusinessInputProvenance,
    ClientPriceQuoteInput,
    WarehouseCapacityInput,
)
from services.rfp_approval.authorization import AuthorizationDenied, EffectiveAuthorization
from services.rfp_approval.commercial_store import CommercialInputRecord
from services.rfp_approval.commercial_store import DepartmentRevisionRecord
from services.rfp_approval.correction_coordinator import CorrectedDepartmentResult
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_approval.identity import VerifiedAuthenticationContext
from services.rfp_approval.runtime import ApprovalRuntimeError, checkpoint_path
from services.rfp_approval.proposal_coordinator import (
    ProposalMaterializationResult,
    materialize_initial_proposal,
)
from services.rfp_approval.assessment_coordinator import InitialAssessmentResult
from services.rfp_approval.store import ApprovalStoreError
from services.rfp_intake.contracts import RFPDetailResponse


def _user() -> UserInDB:
    return UserInDB(
        id="trackflow-user-1",
        email="ana@example.test",
        hashed_password="not-used",
        role=Role.manager,
    )


def _authentication() -> RFPApprovalAuthentication:
    return RFPApprovalAuthentication(
        user=_user(),
        context=VerifiedAuthenticationContext._from_verified_authenticator(
            environment="qa",
            auth_source="oidc",
            issuer="urn:trackflow:qa:oidc:test",
            subject="qa-ana",
            trackflow_user_id="trackflow-user-1",
            general_role="manager",
        ),
    )


def _business_input_json() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "input_id": str(uuid4()),
        "input_kind": "client_price_quote",
        "payload": {
            "input_kind": "client_price_quote",
            "department": "lastmile",
            "service_code": "standard_delivery",
            "amount": "12.5000",
            "currency": "USD",
            "unit": "shipment",
            "effective_from": now.date().isoformat(),
            "effective_until": None,
        },
        "provenance": {
            "evidence_type": "approved_quote",
            "evidence_id": str(uuid4()),
            "source_name": "Authorized QA quote",
            "issued_at": now.isoformat(),
            "valid_until": None,
            "source_payload_hash": "a" * 64,
            "notes": None,
        },
        "idempotency_key": str(uuid4()),
        "recorded_at": now.isoformat(),
    }


def _monthly_storage_volume_json() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "input_id": str(uuid4()),
        "input_kind": "monthly_storage_volume",
        "payload": {
            "input_kind": "monthly_storage_volume",
            "value": "42.1250",
            "dimension": "volume",
            "unit": "m3",
            "period": "month",
            "operational_scope": {
                "location_code": None,
                "service_code": "warehouse-storage",
                "scope_code": "monthly-storage-volume",
            },
            "effective_from": now.date().isoformat(),
            "effective_until": None,
        },
        "provenance": {
            "evidence_type": "responsible_attestation",
            "evidence_id": str(uuid4()),
            "source_name": "Authorized QA storage forecast",
            "issued_at": now.isoformat(),
            "valid_until": None,
            "source_payload_hash": "b" * 64,
            "notes": None,
        },
        "idempotency_key": str(uuid4()),
        "recorded_at": now.isoformat(),
    }


def test_monthly_storage_volume_http_boundary_preserves_decimal_and_rejects_json_types() -> None:
    request = AuthorizedBusinessInputRequest.model_validate(
        _monthly_storage_volume_json()
    )
    assert request.payload.value == Decimal("42.1250")
    assert request.payload.operational_scope.scope_code == "monthly-storage-volume"

    numeric = _monthly_storage_volume_json()
    numeric["payload"]["value"] = 42.125
    with pytest.raises(ValueError):
        AuthorizedBusinessInputRequest.model_validate(numeric)

    unknown_dimension = _monthly_storage_volume_json()
    unknown_dimension["payload"]["dimension"] = "pallet_count"
    with pytest.raises(ValueError):
        AuthorizedBusinessInputRequest.model_validate(unknown_dimension)


def _approval_run_initialize_json() -> dict[str, object]:
    return {
        "idempotency_key": str(uuid4()),
        "automate_proposal_materialization": False,
    }


def _workflow_start_json() -> dict[str, object]:
    return {"revision_id": str(uuid4()), "department": "lastmile"}


def _department_correction_json() -> dict[str, object]:
    return {
        "expected_predecessor_status": "blocked_input",
        "expected_predecessor_content_hash": "a" * 64,
        "expected_predecessor_state_version": 1,
        "authorized_input_ids": [str(uuid4())],
        "idempotency_key": str(uuid4()),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def _approval_request_boundary_client() -> TestClient:
    app = FastAPI()

    @app.post("/initialize")
    def validate_initialize(
        payload: ApprovalRunInitializeRequest,
    ) -> dict[str, object]:
        return {"idempotency_key": str(payload.idempotency_key)}

    @app.post("/workflow")
    def validate_workflow(
        payload: ApprovalWorkflowStartRequest,
    ) -> dict[str, object]:
        return {
            "revision_id": str(payload.revision_id),
            "department": payload.department.value,
        }

    @app.post("/correct")
    def validate_correction(
        payload: DepartmentCorrectionRequest,
    ) -> dict[str, object]:
        return {
            "authorized_input_ids": [
                str(value) for value in payload.authorized_input_ids
            ],
            "idempotency_key": str(payload.idempotency_key),
            "requested_at": payload.requested_at.isoformat(),
        }

    return TestClient(app)


def _business_input_boundary_client() -> TestClient:
    app = FastAPI()

    @app.post("/business-inputs")
    def validate_business_input(
        payload: AuthorizedBusinessInputRequest,
    ) -> dict[str, object]:
        return {
            "input_id": str(payload.input_id),
            "department": payload.payload.department.value,
            "amount": str(payload.payload.amount),
            "effective_from": payload.payload.effective_from.isoformat(),
            "recorded_at": payload.recorded_at.isoformat(),
        }

    return TestClient(app)


def _public_state(target) -> dict[str, object]:
    return {
        "thread_id": target.thread_id,
        "status": "awaiting_human_decision",
        "target": target.model_dump(mode="json"),
        "interrupted": True,
        "interrupt_payload": {"allowed_actions": ["approve", "reject", "request_changes"]},
        "decision": None,
        "application": None,
        "coordination": None,
    }


@pytest.mark.parametrize("ticket_status", ["esperando_aprobación", "terminado"])
def test_public_ticket_contract_accepts_part3_states(ticket_status: str) -> None:
    now = datetime.now(timezone.utc)

    projected = RFPDetailResponse(
        ticket_id=uuid4(),
        status=ticket_status,
        created_at=now,
        updated_at=now,
    )

    assert projected.status == ticket_status


def test_local_adapter_uses_verified_user_and_server_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRACKFLOW_ENVIRONMENT", "qa")
    monkeypatch.setattr(
        "services.api.rfp_approval_auth.get_current_user", lambda token: _user()
    )
    authentication = _verified_local("verified-local-token")
    assert authentication.context.environment == "qa"
    assert authentication.context.auth_source == "local"
    assert authentication.context.issuer == "urn:trackflow:qa:local"
    assert authentication.context.subject == _user().id
    assert authentication.context.trackflow_user_id == _user().id


def test_oidc_adapter_preserves_verified_issuer_and_subject(monkeypatch) -> None:
    monkeypatch.setenv("TRACKFLOW_ENVIRONMENT", "qa")
    settings = SimpleNamespace(issuer="https://issuer.example.test")
    monkeypatch.setattr(
        "services.api.rfp_approval_auth.OperationalOIDCSettings.from_env",
        lambda: settings,
    )
    monkeypatch.setattr(
        "services.api.rfp_approval_auth._decode_external_token",
        lambda token, configured: {
            "sub": "verified-subject",
            "trackflow_user_id": _user().id,
        },
    )
    monkeypatch.setattr(
        "services.api.auth_security._find_user_by_id", lambda user_id: _user()
    )
    authentication = _verified_oidc("verified-oidc-token")
    assert authentication.context.auth_source == "oidc"
    assert authentication.context.issuer == settings.issuer
    assert authentication.context.subject == "verified-subject"


def test_checkpoint_path_is_server_configured_and_strict(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH", raising=False)
    with pytest.raises(ApprovalRuntimeError) as raised:
        checkpoint_path()
    assert raised.value.code == "checkpoint_path_not_configured"
    path = tmp_path / "approval.sqlite3"
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH", str(path))
    assert checkpoint_path() == path

    outside = tmp_path.parent / "outside.sqlite3"
    monkeypatch.setenv("TRACKFLOW_RFP_APPROVAL_CHECKPOINT_PATH", str(outside))
    with pytest.raises(ApprovalRuntimeError) as outside_error:
        checkpoint_path()
    assert outside_error.value.code == "checkpoint_path_outside_runtime_directory"


def test_authentication_environment_is_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("TRACKFLOW_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        "services.api.rfp_approval_auth.get_current_user", lambda token: _user()
    )
    with pytest.raises(HTTPException) as raised:
        _verified_local("verified-local-token")
    assert raised.value.status_code == 503


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/initialize", _approval_run_initialize_json()),
        ("/workflow", _workflow_start_json()),
        ("/correct", _department_correction_json()),
    ],
)
def test_remaining_approval_requests_accept_valid_http_json(
    path: str, body: dict[str, object]
) -> None:
    response = _approval_request_boundary_client().post(path, json=body)

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("path", "body", "field_name"),
    [
        ("/initialize", _approval_run_initialize_json(), "idempotency_key"),
        ("/workflow", _workflow_start_json(), "revision_id"),
        ("/correct", _department_correction_json(), "idempotency_key"),
    ],
)
@pytest.mark.parametrize("invalid_uuid", ["not-a-uuid", 42, True, {}])
def test_remaining_approval_requests_reject_invalid_uuid_json(
    path: str,
    body: dict[str, object],
    field_name: str,
    invalid_uuid: object,
) -> None:
    request = deepcopy(body)
    request[field_name] = invalid_uuid

    response = _approval_request_boundary_client().post(path, json=request)

    assert response.status_code == 422


@pytest.mark.parametrize("department", ["customer_success", 7, True, {}])
def test_workflow_start_rejects_unknown_or_non_text_department(
    department: object,
) -> None:
    body = _workflow_start_json()
    body["department"] = department

    response = _approval_request_boundary_client().post("/workflow", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "authorized_input_ids",
    ["not-an-array", ["not-a-uuid"], [7], [True], [{}]],
)
def test_department_correction_rejects_invalid_uuid_collections(
    authorized_input_ids: object,
) -> None:
    body = _department_correction_json()
    body["authorized_input_ids"] = authorized_input_ids

    response = _approval_request_boundary_client().post("/correct", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "requested_at",
    ["not-a-datetime", "2026-11-22T18:00:00", 42, True, {}],
)
def test_department_correction_rejects_invalid_or_naive_datetime(
    requested_at: object,
) -> None:
    body = _department_correction_json()
    body["requested_at"] = requested_at

    response = _approval_request_boundary_client().post("/correct", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/initialize", _approval_run_initialize_json()),
        ("/workflow", _workflow_start_json()),
        ("/correct", _department_correction_json()),
    ],
)
def test_remaining_approval_requests_reject_additional_fields(
    path: str, body: dict[str, object]
) -> None:
    request = deepcopy(body)
    request["unexpected"] = "not-authorized"

    response = _approval_request_boundary_client().post(path, json=request)

    assert response.status_code == 422


@pytest.mark.parametrize("value", [0, 1, "true", [], {}])
def test_approval_run_initialize_preserves_strict_boolean(value: object) -> None:
    body = _approval_run_initialize_json()
    body["automate_proposal_materialization"] = value

    response = _approval_request_boundary_client().post("/initialize", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("value", [True, 1.0, "1", [], {}])
def test_department_correction_preserves_strict_state_version(value: object) -> None:
    body = _department_correction_json()
    body["expected_predecessor_state_version"] = value

    response = _approval_request_boundary_client().post("/correct", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/rfps/{uuid4()}/approval", _approval_run_initialize_json()),
        (
            f"/rfps/{uuid4()}/approval/{uuid4()}/departments/start",
            _workflow_start_json(),
        ),
    ],
)
def test_valid_initialize_and_workflow_json_reach_business_authorization(
    monkeypatch, path: str, body: dict[str, object]
) -> None:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_rfp_approval_authentication] = _authentication
    app.dependency_overrides[routes.get_db] = lambda: SimpleNamespace()

    def deny_authorization(*args, **kwargs):
        raise HTTPException(status_code=403, detail="authorization denied for test")

    monkeypatch.setattr(routes, "_authorize_approval_start", deny_authorization)
    monkeypatch.setattr(routes, "_authorize_run_management", deny_authorization)
    with TestClient(app) as client:
        response = client.post(path, json=body)

    assert response.status_code == 403
    assert response.json() == {"detail": "authorization denied for test"}


def test_valid_correction_json_reaches_persisted_authorization(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_rfp_approval_authentication] = _authentication
    app.dependency_overrides[routes.get_db] = lambda: SimpleNamespace()
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: SimpleNamespace())

    def deny_authorization(*args, **kwargs):
        raise AuthorizationDenied("authorization_not_found")

    monkeypatch.setattr(routes, "resolve_effective_authorization", deny_authorization)
    with TestClient(app) as client:
        response = client.post(
            f"/rfps/{uuid4()}/approval/{uuid4()}/departments/lastmile/"
            f"revisions/{uuid4()}/correct",
            json=_department_correction_json(),
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Department business-input permission is required"
    }


def test_business_input_http_boundary_accepts_valid_json_without_losing_precision(
) -> None:
    response = _business_input_boundary_client().post(
        "/business-inputs", json=_business_input_json()
    )

    assert response.status_code == 200, response.text
    assert response.json()["department"] == "lastmile"
    assert response.json()["amount"] == "12.5000"


def test_business_input_http_boundary_rejects_invalid_uuid() -> None:
    body = _business_input_json()
    body["input_id"] = "not-a-uuid"

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


def test_business_input_http_boundary_rejects_unknown_department() -> None:
    body = _business_input_json()
    body["payload"]["department"] = "customer_success"

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("amount", ["not-a-decimal", "NaN", "Infinity", 12.5, True])
def test_business_input_http_boundary_rejects_unsafe_decimal(amount) -> None:
    body = _business_input_json()
    body["payload"]["amount"] = amount

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


def test_business_input_http_boundary_rejects_invalid_date() -> None:
    body = _business_input_json()
    body["payload"]["effective_from"] = "2026-02-30"

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


def test_business_input_http_boundary_rejects_naive_datetime() -> None:
    body = _business_input_json()
    body["recorded_at"] = "2026-11-22T18:00:00"

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("extra", "not-authorized"),
        ("input_id", True),
        ("provenance", []),
        ("payload", "not-an-object"),
    ],
)
def test_business_input_http_boundary_rejects_incompatible_json(
    mutation: str, value: object
) -> None:
    body = deepcopy(_business_input_json())
    body[mutation] = value

    response = _business_input_boundary_client().post("/business-inputs", json=body)

    assert response.status_code == 422


def test_business_input_domain_contract_remains_strict() -> None:
    with pytest.raises(ValueError):
        ClientPriceQuoteInput(
            department="lastmile",
            service_code="standard_delivery",
            amount="12.5000",
            currency="USD",
            unit="shipment",
            effective_from="2026-11-22",
        )


def test_business_input_http_validation_precedes_persisted_authorization(
    monkeypatch,
) -> None:
    body = _business_input_json()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_rfp_approval_authentication] = _authentication
    app.dependency_overrides[routes.get_db] = lambda: SimpleNamespace()
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: SimpleNamespace())

    def deny_authorization(*args, **kwargs):
        raise AuthorizationDenied("authorization_not_found")

    monkeypatch.setattr(routes, "resolve_effective_authorization", deny_authorization)
    with TestClient(app) as client:
        response = client.post(
            f"/rfps/{uuid4()}/approval/{uuid4()}/business-inputs",
            json=body,
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Business-input permission is required"}


def test_decision_contract_rejects_unexplained_changes() -> None:
    with pytest.raises(ValueError):
        ApprovalDecisionRequest(action="request_changes")


def test_department_correction_uses_persisted_identity_and_never_starts_hitl(
    monkeypatch,
) -> None:
    ticket_id, run_id, predecessor_id, revision_id, bundle_id, authorization_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    identity = SimpleNamespace(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc:test",
        subject="qa-carlos",
        trackflow_user_id="qa-carlos-user",
        general_role="user",
        principal_id=uuid4(),
        binding_id=uuid4(),
        responsible_code="carlos_vega",
        synthetic=True,
    )
    authorization = EffectiveAuthorization(
        authorization_id=authorization_id,
        authorization_version=3,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="provide_business_input",
        department_scope="lastmile",
        trigger_scope=None,
        target_policy="department_exact",
        evaluated_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: identity)
    targets = []
    monkeypatch.setattr(
        routes,
        "resolve_effective_authorization",
        lambda session, *, identity, target: targets.append(target) or authorization,
    )
    captured = {}

    def correct(session, **kwargs):
        captured.update(kwargs)
        return CorrectedDepartmentResult(
            record=DepartmentRevisionRecord(
                revision_id, run_id, "lastmile", 2, "pending_approval", 0, False
            ),
            revision=SimpleNamespace(
                content_hash="a" * 64, validation_bundle_id=bundle_id
            ),
            remaining_input_codes=(),
        )

    monkeypatch.setattr(routes, "create_corrected_department_from_inputs", correct)
    session = SimpleNamespace(
        committed=False,
        rolled_back=False,
        commit=lambda: setattr(session, "committed", True),
        rollback=lambda: setattr(session, "rolled_back", True),
    )
    payload = DepartmentCorrectionRequest(
        expected_predecessor_status="blocked_input",
        expected_predecessor_content_hash="b" * 64,
        expected_predecessor_state_version=0,
        authorized_input_ids=[uuid4()],
        idempotency_key=uuid4(),
        requested_at=datetime.now(timezone.utc),
    )

    result = routes.correct_department_revision(
        ticket_id,
        run_id,
        DepartmentId.lastmile,
        predecessor_id,
        payload,
        _authentication(),
        session,
    )

    assert targets[0].permission == "provide_business_input"
    assert targets[0].department_scope == "lastmile"
    assert captured["identity"] is identity
    assert captured["actor"].official_responsible == "Carlos Vega"
    assert captured["actor"].principal.subject == identity.subject
    assert session.committed and not session.rolled_back
    assert result.status == "pending_approval"
    assert result.ready_for_assessment is True
    assert result.ready_for_hitl is False


def test_department_correction_rejects_cross_department_actor_before_store(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(responsible_code="carlos_vega")
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: identity)
    monkeypatch.setattr(
        routes,
        "resolve_effective_authorization",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AuthorizationDenied("official_department_owner_required")
        ),
    )
    monkeypatch.setattr(
        routes,
        "create_corrected_department_from_inputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("store must not be called")
        ),
    )
    payload = DepartmentCorrectionRequest(
        expected_predecessor_status="blocked_input",
        expected_predecessor_content_hash="b" * 64,
        expected_predecessor_state_version=0,
        authorized_input_ids=[uuid4()],
        idempotency_key=uuid4(),
        requested_at=datetime.now(timezone.utc),
    )

    with pytest.raises(HTTPException) as raised:
        routes.correct_department_revision(
            uuid4(),
            uuid4(),
            DepartmentId.warehouse,
            uuid4(),
            payload,
            _authentication(),
            SimpleNamespace(),
        )

    assert raised.value.status_code == 403


def test_initialize_run_derives_from_server_handoff_and_commits(monkeypatch) -> None:
    ticket_id, run_id, response_run_id, revision_id, bundle_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    calls = {}
    identity, authorization = object(), object()
    monkeypatch.setattr(
        routes,
        "_authorize_approval_start",
        lambda *args: (identity, authorization),
    )

    def initialize(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            approval_run_id=run_id,
            ticket_id=ticket_id,
            response_run_id=response_run_id,
            revisions=(
                SimpleNamespace(
                    department="warehouse",
                    revision_id=revision_id,
                    status="pending_approval",
                    validation_bundle_id=bundle_id,
                    ready_for_hitl=True,
                ),
            ),
            replayed=False,
        )

    monkeypatch.setattr(routes, "initialize_approval_run_from_handoff", initialize)
    started = []

    class Workflow:
        def state(self, thread_id):
            return SimpleNamespace(values={})

        def start(self, target):
            started.append(target)

    @contextmanager
    def opened():
        yield Workflow()

    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    session = SimpleNamespace(
        committed=False,
        commit=lambda: setattr(session, "committed", True),
        rollback=lambda: None,
    )
    result = routes.initialize_approval_run(
        ticket_id,
        ApprovalRunInitializeRequest(idempotency_key=uuid4()),
        _authentication(),
        session,
    )
    assert calls["ticket_id"] == ticket_id
    assert calls["response_run_id"] is None
    assert calls["identity"] is identity
    assert calls["authorization"] is authorization
    assert calls["automate_proposal_materialization"] is False
    assert session.committed is True
    assert result.approval_run_id == run_id
    assert result.revisions[0].department is DepartmentId.warehouse
    assert result.revisions[0].ready_for_hitl is True
    assert result.proposal_automation_delegated is False
    assert started[0].revision_id == revision_id


def test_initialize_run_automation_requires_management_grant(monkeypatch) -> None:
    ticket_id, run_id, response_run_id = uuid4(), uuid4(), uuid4()
    identity, authorization = object(), object()
    monkeypatch.setattr(
        routes,
        "_authorize_approval_start",
        lambda *args: (_ for _ in ()).throw(AssertionError("start grant used")),
    )
    monkeypatch.setattr(
        routes,
        "_authorize_run_management",
        lambda *args: (identity, authorization),
    )
    captured = {}

    def initialize(session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            approval_run_id=run_id,
            ticket_id=ticket_id,
            response_run_id=response_run_id,
            revisions=(),
            replayed=False,
        )

    monkeypatch.setattr(routes, "initialize_approval_run_from_handoff", initialize)

    @contextmanager
    def opened():
        yield SimpleNamespace()

    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    result = routes.initialize_approval_run(
        ticket_id,
        ApprovalRunInitializeRequest(
            idempotency_key=uuid4(), automate_proposal_materialization=True
        ),
        _authentication(),
        session,
    )

    assert captured["identity"] is identity
    assert captured["authorization"] is authorization
    assert captured["automate_proposal_materialization"] is True
    assert result.proposal_automation_delegated is True


def test_start_endpoint_authorizes_and_starts_exact_target(monkeypatch) -> None:
    calls: list[object] = []
    workflow = SimpleNamespace()
    workflow.start = lambda target: calls.append(target)

    @contextmanager
    def opened():
        yield workflow

    monkeypatch.setattr(
        routes, "_authorize_run_management", lambda *args: calls.append("authorized")
    )
    monkeypatch.setattr(
        routes, "assert_current_conflict_clearance", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    monkeypatch.setattr(routes, "public_workflow_state", lambda wf, thread_id: _public_state(calls[-1]))
    ticket_id, run_id, revision_id = uuid4(), uuid4(), uuid4()
    result = routes.start_department_approval(
        ticket_id,
        run_id,
        ApprovalWorkflowStartRequest(
            revision_id=revision_id, department=DepartmentId.warehouse
        ),
        _authentication(),
        SimpleNamespace(),
    )
    assert calls[0] == "authorized"
    target = calls[1]
    assert target.ticket_id == ticket_id
    assert target.approval_run_id == run_id
    assert target.revision_id == revision_id
    assert result.interrupted is True


def test_review_inbox_returns_only_effectively_authorized_department(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(environment="qa")
    ticket_id, run_id, revision_id, bundle_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: identity)

    def authorize(session, *, identity, target, **kwargs):
        if target.department_scope != "warehouse":
            raise AuthorizationDenied("no_effective_authorization")
        return object()

    monkeypatch.setattr(routes, "resolve_effective_authorization", authorize)
    captured = []

    def inbox(session, *, department, environment):
        captured.append((department, environment))
        return [
            {
                "approval_run_id": run_id,
                "ticket_id": ticket_id,
                "revision_id": revision_id,
                "department": DepartmentId.warehouse,
                "review_revision_no": 1,
                "status": "pending_approval",
                "content_hash": "a" * 64,
                "validation_bundle_id": bundle_id,
                "created_at": datetime.now(timezone.utc),
            }
        ]

    monkeypatch.setattr(routes, "list_department_review_inbox", inbox)
    monkeypatch.setattr(
        routes, "assert_current_conflict_clearance", lambda *args, **kwargs: None
    )

    result = routes.get_department_approval_inbox(
        _authentication(), SimpleNamespace()
    )

    assert captured == [("warehouse", "qa")]
    assert len(result.items) == 1
    assert result.items[0].department is DepartmentId.warehouse


def test_assessment_endpoint_commits_before_starting_cleared_threads(monkeypatch) -> None:
    identity, authorization = object(), object()
    ticket_id, run_id, revision_id, bundle_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    calls: list[str] = []

    class Session:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

    class Workflow:
        def state(self, thread_id):
            return SimpleNamespace(values={})

        def start(self, target):
            assert calls == ["assess", "commit"]
            calls.append("start")

    @contextmanager
    def opened():
        yield Workflow()

    monkeypatch.setattr(
        routes, "_authorize_run_management", lambda *args: (identity, authorization)
    )
    monkeypatch.setattr(
        routes,
        "assess_and_release_initial_revisions",
        lambda *args, **kwargs: (
            calls.append("assess")
            or InitialAssessmentResult(
                approval_run_id=run_id,
                outcomes=(
                    ("volume-vs-capacity", "no_conflict"),
                    ("returns-sla-breach", "no_conflict"),
                    ("currency-mismatch", "no_conflict"),
                ),
                ready_revisions=(("warehouse", revision_id, bundle_id),),
                missing_input_codes=(),
            )
        ),
    )
    monkeypatch.setattr(routes, "open_approval_workflow", opened)

    result = routes.assess_and_start_department_approvals(
        ticket_id,
        run_id,
        _authentication(),
        Session(),
    )

    assert calls == ["assess", "commit", "start"]
    assert result.ready_revisions[0].revision_id == revision_id
    assert result.ready_revisions[0].validation_bundle_id == bundle_id
    assert [item.outcome for item in result.outcomes] == ["no_conflict"] * 3


def test_business_input_endpoint_builds_actor_from_persisted_identity(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    ticket_id, run_id, input_id, authorization_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    identity = ResolvedIdentity._from_persisted_binding(
        environment="qa",
        auth_source="oidc",
        issuer="urn:trackflow:qa:oidc:test",
        subject="qa-ana",
        trackflow_user_id="trackflow-user-1",
        general_role="manager",
        principal_id=uuid4(),
        binding_id=uuid4(),
        responsible_code="ana_whitfield",
        synthetic=True,
    )
    authorization = EffectiveAuthorization(
        authorization_id=authorization_id,
        authorization_version=1,
        principal_id=identity.principal_id,
        binding_id=identity.binding_id,
        environment="qa",
        permission="provide_business_input",
        department_scope="warehouse",
        trigger_scope=None,
        target_policy="department_exact",
        evaluated_at=now,
    )
    provenance = BusinessInputProvenance(
        evidence_type="responsible_attestation",
        evidence_id=uuid4(),
        source_name="Ana QA capacity attestation",
        issued_at=now,
        source_payload_hash="a" * 64,
    )
    request = AuthorizedBusinessInputRequest(
        input_id=input_id,
        input_kind="warehouse_capacity",
        payload=WarehouseCapacityInput(
            site_code="WH-QA-01",
            capacity_value=Decimal("5000"),
            capacity_unit="orders",
            period="month",
            available_from=date.today(),
        ),
        provenance=provenance,
        idempotency_key=uuid4(),
        recorded_at=now,
    )
    captured = {}
    monkeypatch.setattr(routes, "_approval_identity", lambda *args: identity)
    monkeypatch.setattr(
        routes, "resolve_effective_authorization", lambda *args, **kwargs: authorization
    )

    def record(session, **kwargs):
        captured.update(kwargs)
        value = kwargs["business_input"]
        return CommercialInputRecord(
            value.input_id,
            value.approval_run_id,
            value.payload_hash,
            False,
        )

    monkeypatch.setattr(routes, "record_authorized_business_input", record)
    session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    result = routes.register_approval_business_input(
        ticket_id,
        run_id,
        request,
        _authentication(),
        session,
    )

    value = captured["business_input"]
    assert value.actor.official_responsible == "Ana Whitfield"
    assert value.actor.principal.subject == identity.subject
    assert value.actor.authorization_id == authorization_id
    assert value.payload.capacity_value == Decimal("5000")
    assert result.input_id == input_id


def test_resume_endpoint_passes_only_sealed_authentication(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class Workflow:
        def resume(self, **kwargs):
            calls.update(kwargs)

    @contextmanager
    def opened():
        yield Workflow()

    monkeypatch.setattr(routes, "_authorize_department", lambda *args: None)
    monkeypatch.setattr(
        routes,
        "get_department_review",
        lambda *args, **kwargs: {"pending_commercial_items": []},
    )
    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    monkeypatch.setattr(
        routes,
        "public_workflow_state",
        lambda workflow, thread_id: {
            **_public_state(
                SimpleNamespace(
                    thread_id=thread_id,
                    model_dump=lambda mode: {},
                )
            ),
            "status": "decision_applied",
            "interrupted": False,
        },
    )
    authentication = _authentication()
    result = routes.resume_department_approval(
        uuid4(),
        uuid4(),
        DepartmentId.reverse,
        uuid4(),
        ApprovalDecisionRequest(action="approve"),
        authentication,
        SimpleNamespace(),
    )
    assert calls["authentication"] is authentication.context
    assert calls["command"].action == "approve"
    assert result.status == "decision_applied"


def test_indicative_approval_requires_and_seals_explicit_acknowledgement(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}
    pending_code = "LAST_MILE_RATE_REQUIRED"

    class Workflow:
        def resume(self, **kwargs):
            calls.update(kwargs)

    @contextmanager
    def opened():
        yield Workflow()

    monkeypatch.setattr(routes, "_authorize_department", lambda *args: None)
    monkeypatch.setattr(
        routes,
        "get_department_review",
        lambda *args, **kwargs: {
            "pending_commercial_items": [{"code": pending_code}]
        },
    )
    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    monkeypatch.setattr(
        routes,
        "public_workflow_state",
        lambda workflow, thread_id: {
            **_public_state(
                SimpleNamespace(
                    thread_id=thread_id,
                    model_dump=lambda mode: {},
                )
            ),
            "status": "decision_applied",
            "interrupted": False,
        },
    )
    args = (
        uuid4(),
        uuid4(),
        DepartmentId.lastmile,
        uuid4(),
    )
    with pytest.raises(HTTPException) as missing:
        routes.resume_department_approval(
            *args,
            ApprovalDecisionRequest(action="approve"),
            _authentication(),
            SimpleNamespace(),
        )
    assert missing.value.status_code == 422
    assert missing.value.detail == "pending_commercial_items_acknowledgement_required"
    assert calls == {}

    routes.resume_department_approval(
        *args,
        ApprovalDecisionRequest(
            action="approve",
            acknowledge_pending_commercial_items=True,
            reason="Approved as an indicative proposal.",
        ),
        _authentication(),
        SimpleNamespace(),
    )
    assert calls["command"].reason == (
        "[indicative-commercial-ack:v1] LAST_MILE_RATE_REQUIRED | "
        "Approved as an indicative proposal."
    )


def test_review_endpoint_returns_safe_preview_after_department_authorization(monkeypatch) -> None:
    ticket_id, run_id, revision_id = uuid4(), uuid4(), uuid4()
    calls = []
    monkeypatch.setattr(
        routes, "_authorize_department", lambda *args: calls.append("authorized")
    )
    monkeypatch.setattr(
        routes,
        "get_department_review",
        lambda session, **kwargs: {
            "approval_run_id": run_id,
            "ticket_id": ticket_id,
            "revision_id": revision_id,
                "department": DepartmentId.warehouse,
            "run_status": "active",
            "revision_status": "pending_approval",
            "is_current": True,
            "state_version": 0,
            "content_hash": "a" * 64,
            "validation_bundle_id": uuid4(),
            "validated_at": datetime.now(timezone.utc),
            "validation": {"eligible_for_approval": True},
                "pending_commercial_items": [],
            "safe_preview_artifact_id": uuid4(),
            "safe_preview_payload_hash": "b" * 64,
            "safe_preview": "Approved safe content",
            "evidence": [],
            "provenance": {"source_kind": "part2_import"},
            "allowed_actions": ["approve", "reject", "request_changes"],
        },
    )
    monkeypatch.setattr(
        routes, "assert_current_conflict_clearance", lambda *args, **kwargs: None
    )
    result = routes.get_department_approval_review(
        ticket_id,
        run_id,
        DepartmentId.warehouse,
        revision_id,
        _authentication(),
        SimpleNamespace(),
    )
    assert calls == ["authorized"]
    assert result.safe_preview == "Approved safe content"
    assert result.allowed_actions == ["approve", "reject", "request_changes"]


def test_reconcile_requires_persisted_run_management(monkeypatch) -> None:
    monkeypatch.setattr(
        routes,
        "_authorize_run_management",
        lambda *args: (_ for _ in ()).throw(
            HTTPException(status_code=403, detail="required")
        ),
    )
    with pytest.raises(HTTPException) as raised:
        routes.reconcile_approval_completion(
            uuid4(),
            uuid4(),
            ApprovalWorkflowStartRequest(
                revision_id=uuid4(), department=DepartmentId.lastmile
            ),
            _authentication(),
            SimpleNamespace(),
        )
    assert raised.value.status_code == 403


def test_final_document_endpoint_does_not_expose_missing_document(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_authorize_run_management", lambda *args: None)
    monkeypatch.setattr(routes, "get_public_final_document", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as raised:
        routes.get_approval_final_document(
            uuid4(), uuid4(), _authentication(), SimpleNamespace()
        )
    assert raised.value.status_code == 404


def test_document_locator_requires_management_and_server_environment(monkeypatch) -> None:
    ticket_id, run_id, document_id = uuid4(), uuid4(), uuid4()
    identity = SimpleNamespace(environment="qa")
    calls = {}
    monkeypatch.setattr(
        routes,
        "_authorize_run_management",
        lambda *args: (identity, object()),
    )

    def locate(session, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            ticket_id=ticket_id,
            approval_run_id=run_id,
            final_document_id=document_id,
        )

    monkeypatch.setattr(routes, "get_final_document_locator", locate)

    result = routes.get_approval_final_document_locator(
        ticket_id=ticket_id,
        authentication=_authentication(),
        session=object(),
    )

    assert calls == {"ticket_id": ticket_id, "environment": "qa"}
    assert result.ticket_id == ticket_id
    assert result.approval_run_id == run_id
    assert result.final_document_id == document_id


def test_document_locator_preserves_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        routes,
        "_authorize_run_management",
        lambda *args: (SimpleNamespace(environment="qa"), object()),
    )
    monkeypatch.setattr(routes, "get_final_document_locator", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as raised:
        routes.get_approval_final_document_locator(
            ticket_id=uuid4(),
            authentication=_authentication(),
            session=object(),
        )

    assert raised.value.status_code == 404


def test_materializer_refuses_non_management_authority_before_database_access() -> None:
    with pytest.raises(ApprovalStoreError) as raised:
        materialize_initial_proposal(
            SimpleNamespace(),
            approval_run_id=uuid4(),
            ticket_id=uuid4(),
            identity=SimpleNamespace(),
            authorization=SimpleNamespace(permission="approve_department"),
        )
    assert raised.value.code == "manage_approval_run_required"


def test_materialize_endpoint_commits_then_reconciles(monkeypatch) -> None:
    identity, authorization = object(), object()
    ticket_id, run_id, revision_id, proposal_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    calls: list[str] = []

    class Session:
        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

    class Workflow:
        def reconcile(self, thread_id):
            assert calls == ["materialize", "commit"]
            calls.append("reconcile")

    @contextmanager
    def opened():
        yield Workflow()

    monkeypatch.setattr(
        routes, "_authorize_run_management", lambda *args: (identity, authorization)
    )
    monkeypatch.setattr(
        routes,
        "materialize_initial_proposal",
        lambda *args, **kwargs: (
            calls.append("materialize")
            or ProposalMaterializationResult(
                proposal_revision_id=proposal_id,
                approval_run_id=run_id,
                eligible_for_synthesis=True,
                replayed=False,
            )
        ),
    )
    monkeypatch.setattr(routes, "open_approval_workflow", opened)
    monkeypatch.setattr(
        routes,
        "public_workflow_state",
        lambda workflow, thread_id: {
            "thread_id": thread_id,
            "status": "finalized",
            "target": {},
            "interrupted": False,
            "interrupt_payload": None,
            "decision": None,
            "application": None,
            "coordination": {"outcome": "finalized"},
        },
    )
    result = routes.materialize_and_reconcile_approval(
        ticket_id,
        run_id,
        ApprovalWorkflowStartRequest(
            revision_id=revision_id, department=DepartmentId.warehouse
        ),
        _authentication(),
        Session(),
    )
    assert calls == ["materialize", "commit", "reconcile"]
    assert result.proposal_revision_id == proposal_id
    assert result.coordination.status == "finalized"
