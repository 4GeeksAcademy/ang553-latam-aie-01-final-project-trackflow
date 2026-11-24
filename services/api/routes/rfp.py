"""Authenticated RFP intake endpoints on the existing FastAPI service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RESPONSIBLE_BY_DEPARTMENT
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
from services.api.rfp_approval_auth import (
    RFPApprovalAuthentication,
    get_rfp_approval_authentication,
)
from pipelines.rfp_approval.contracts import (
    AuthenticatedPrincipal,
    AuthorizedBusinessInput,
    AuthorizedHumanActor,
    PublicFinalDocument,
    authorized_business_input_hash,
    indicative_approval_reason,
)
from pipelines.rfp_approval.hitl import (
    ApprovalWorkflowError,
    DepartmentApprovalTarget,
    new_human_resume,
)
from services.rfp_approval.authorization import (
    AuthorizationDenied,
    AuthorizationTarget,
    resolve_effective_authorization,
)
from services.rfp_approval.finalization import (
    get_final_document_locator,
    get_public_final_document,
)
from services.rfp_approval.http_contracts import (
    ApprovalDecisionRequest,
    ApprovalRunInitializationPublic,
    ApprovalRunInitializeRequest,
    ApprovalWorkflowPublic,
    ApprovalWorkflowStartRequest,
    AuthorizedBusinessInputPublic,
    AuthorizedBusinessInputRequest,
    DepartmentCorrectionPublic,
    DepartmentCorrectionRequest,
    DepartmentReviewInboxPublic,
    DepartmentReviewPublic,
    FinalDocumentLocatorPublic,
    InitialAssessmentPublic,
    ProposalMaterializationPublic,
)
from services.rfp_approval.assessment_coordinator import (
    assert_current_conflict_clearance,
    assess_and_release_initial_revisions,
)
from services.rfp_approval.identity import (
    IdentityResolutionDenied,
    resolve_authenticated_identity,
)
from services.rfp_approval.commercial_store import record_authorized_business_input
from services.rfp_approval.correction_coordinator import (
    create_corrected_department_from_inputs,
)
from services.rfp_approval.authorization import AuthorizationReference
from services.rfp_approval.runtime import (
    ApprovalRuntimeError,
    open_approval_workflow,
    public_workflow_state,
)
from services.rfp_approval.proposal_coordinator import materialize_initial_proposal
from services.rfp_approval.read_model import (
    get_department_review,
    list_department_review_inbox,
)
from services.rfp_approval.run_coordinator import initialize_approval_run_from_handoff
from services.rfp_approval.store import ApprovalStoreError
from services.rfp_intake.artifacts import InvalidRFPArtifact, cleanup_artifact, persist_pdf
from services.rfp_intake.config import get_rfp_settings
from services.rfp_intake.contracts import (
    DepartmentSectionPublic, RFPDetailResponse, RFPHandoffPublic, RFPJobPublic,
    RFPListResponse, RFPMetadataPublic, RFPSummary, RFPTicketAccepted,
)
from services.rfp_intake.store import accept_rfp, get_rfp, list_rfps, ticket_exists
from services.rfp_response.http_contracts import (
    ResponseGenerationAccepted, ResponseGenerationStart, ResponseRunPublic,
)
from services.rfp_response.store import (
    ResponsePersistenceConflict, accept_response_generation,
    get_response_generation,
)

router = APIRouter(prefix="/rfps", tags=["RFP intake"])

_RESPONSIBLE_NAMES = {
    "ana_whitfield": "Ana Whitfield",
    "carlos_vega": "Carlos Vega",
    "sofia_ramos": "Sofía Ramos",
    "miguel_torres": "Miguel Torres",
}


def require_rfp_admin(user: Annotated[UserInDB, Depends(get_current_user)]) -> UserInDB:
    if user.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


def _approval_identity(session: Session, authentication: RFPApprovalAuthentication):
    try:
        return resolve_authenticated_identity(session, context=authentication.context)
    except IdentityResolutionDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="RFP approval identity is not authorized",
        ) from None


def _authorize_department(
    session: Session,
    authentication: RFPApprovalAuthentication,
    department: DepartmentId,
) -> None:
    identity = _approval_identity(session, authentication)
    try:
        resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="approve_department",
                department_scope=department.value,
                target_policy="department_exact",
                affected_departments=frozenset({department.value}),
            ),
        )
    except AuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Department approval permission is required",
        ) from None


def _authorize_run_management(
    session: Session, authentication: RFPApprovalAuthentication
) -> tuple[object, object]:
    identity = _approval_identity(session, authentication)
    try:
        authorization = resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="manage_approval_run", target_policy="run"
            ),
        )
    except AuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval-run management permission is required",
        ) from None
    return identity, authorization


def _authorize_approval_start(
    session: Session, authentication: RFPApprovalAuthentication
) -> tuple[object, object]:
    identity = _approval_identity(session, authentication)
    try:
        authorization = resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="start_approval_run", target_policy="run"
            ),
        )
    except AuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval-run start permission is required",
        ) from None
    return identity, authorization


def _approval_http_error(error: Exception) -> HTTPException:
    code = getattr(error, "code", "approval_operation_rejected")
    if code == "workflow_thread_not_found":
        status_code = status.HTTP_404_NOT_FOUND
    elif code in {
        "checkpoint_path_not_configured",
        "checkpoint_path_invalid",
        "checkpoint_path_outside_runtime_directory",
        "runtime_directory_not_configured",
        "runtime_directory_invalid",
        "strict_msgpack_required",
    }:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail=code)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=RFPTicketAccepted)
async def upload_rfp(
    file: Annotated[UploadFile, File(...)],
    user: Annotated[UserInDB, Depends(require_rfp_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> RFPTicketAccepted:
    settings, ticket_id = get_rfp_settings(), uuid4()
    try:
        raw_path, _sha256, _size = await persist_pdf(file, root=settings.artifact_root,
            ticket_id=ticket_id, max_bytes=settings.max_upload_bytes)
    except InvalidRFPArtifact as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    try:
        accept_rfp(session, ticket_id=ticket_id, raw_pdf_path=raw_path, uploaded_by_subject=user.id)
        session.commit()
    except Exception:
        session.rollback()
        confirmed_absent = False
        try:
            with Session(bind=session.get_bind()) as reconciliation_session:
                if ticket_exists(reconciliation_session, ticket_id):
                    return RFPTicketAccepted(ticket_id=ticket_id)
                confirmed_absent = True
        except Exception:
            pass
        if confirmed_absent:
            cleanup_artifact(settings.artifact_root, ticket_id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RFP intake unavailable") from None
    return RFPTicketAccepted(ticket_id=ticket_id)


@router.get("", response_model=RFPListResponse)
def list_rfp_tickets(
    _user: Annotated[UserInDB, Depends(require_rfp_admin)],
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RFPListResponse:
    rows, total = list_rfps(session, limit=limit, offset=offset)
    return RFPListResponse(items=[
        RFPSummary(**{**row, "departments_needed": row.get("departments_needed") or []})
        for row in rows
    ], total=total)


@router.post("/{ticket_id}/response", status_code=status.HTTP_202_ACCEPTED,
             response_model=ResponseGenerationAccepted)
def start_rfp_response(
    ticket_id: UUID,
    user: Annotated[UserInDB, Depends(require_rfp_admin)],
    session: Annotated[Session, Depends(get_db)],
    payload: ResponseGenerationStart = Body(default=ResponseGenerationStart()),
) -> ResponseGenerationAccepted:
    """Enqueue one durable response run; no generation executes in HTTP."""
    if not ticket_exists(session, ticket_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="RFP ticket not found")
    try:
        accepted = accept_response_generation(
            session, ticket_id=ticket_id,
            carrier_model=payload.carrier_model,
            confirmation_actor=user.id,
            confirmation_source="admin_response_start",
        )
        session.commit()
    except ResponsePersistenceConflict as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=str(exc)) from None
    except Exception:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="RFP response generation unavailable") from None
    return ResponseGenerationAccepted(ticket_id=ticket_id, **accepted)


def _response_run_or_error(session: Session, *, ticket_id: UUID,
                           run_id: UUID | None = None) -> ResponseRunPublic:
    try:
        result = get_response_generation(session, ticket_id=ticket_id, run_id=run_id)
    except ResponsePersistenceConflict:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="RFP response state is unavailable") from None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="RFP response run not found")
    return ResponseRunPublic(**result)


@router.get("/{ticket_id}/response", response_model=ResponseRunPublic)
def get_current_rfp_response(
    ticket_id: UUID,
    _user: Annotated[UserInDB, Depends(require_rfp_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> ResponseRunPublic:
    return _response_run_or_error(session, ticket_id=ticket_id)


@router.get("/{ticket_id}/response/{run_id}", response_model=ResponseRunPublic)
def get_rfp_response_run(
    ticket_id: UUID,
    run_id: UUID,
    _user: Annotated[UserInDB, Depends(require_rfp_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> ResponseRunPublic:
    return _response_run_or_error(session, ticket_id=ticket_id, run_id=run_id)


def _target(
    *, ticket_id: UUID, approval_run_id: UUID, payload: ApprovalWorkflowStartRequest
) -> DepartmentApprovalTarget:
    return DepartmentApprovalTarget(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        revision_id=payload.revision_id,
        department=payload.department,
    )


@router.post(
    "/{ticket_id}/approval",
    response_model=ApprovalRunInitializationPublic,
)
def initialize_approval_run(
    ticket_id: UUID,
    payload: ApprovalRunInitializeRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ApprovalRunInitializationPublic:
    """Atomically derive Parte 3 from the ticket's persisted Parte 2 handoff."""

    if payload.automate_proposal_materialization:
        identity, authorization = _authorize_run_management(session, authentication)
    else:
        identity, authorization = _authorize_approval_start(session, authentication)
    try:
        initialized = initialize_approval_run_from_handoff(
            session,
            ticket_id=ticket_id,
            response_run_id=None,
            identity=identity,
            authorization=authorization,
            idempotency_key=payload.idempotency_key,
            requested_at=datetime.now(timezone.utc),
            automate_proposal_materialization=(
                payload.automate_proposal_materialization
            ),
        )
        session.commit()
    except AuthorizationDenied:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval-run authorization is no longer effective",
        ) from None
    except ApprovalStoreError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    try:
        with open_approval_workflow() as workflow:
            for item in initialized.revisions:
                if not item.ready_for_hitl:
                    continue
                target = DepartmentApprovalTarget(
                    approval_run_id=initialized.approval_run_id,
                    ticket_id=initialized.ticket_id,
                    revision_id=item.revision_id,
                    department=DepartmentId(item.department),
                )
                if not workflow.state(target.thread_id).values:
                    workflow.start(target)
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        # The PostgreSQL transaction is already durable. Replaying this exact
        # initialization is safe and will recover only missing checkpoint threads.
        raise _approval_http_error(error) from None
    return ApprovalRunInitializationPublic(
        approval_run_id=initialized.approval_run_id,
        ticket_id=initialized.ticket_id,
        response_run_id=initialized.response_run_id,
        revisions=[
            {
                "department": DepartmentId(item.department),
                "revision_id": item.revision_id,
                "status": item.status,
                "validation_bundle_id": item.validation_bundle_id,
                "ready_for_hitl": item.ready_for_hitl,
            }
            for item in initialized.revisions
        ],
        proposal_automation_delegated=(
            payload.automate_proposal_materialization
        ),
        replayed=initialized.replayed,
    )


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/assess-and-start",
    response_model=InitialAssessmentPublic,
)
def assess_and_start_department_approvals(
    ticket_id: UUID,
    approval_run_id: UUID,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> InitialAssessmentPublic:
    """Derive official conflict facts and start only fully cleared branches."""

    identity, authorization = _authorize_run_management(session, authentication)
    try:
        assessed = assess_and_release_initial_revisions(
            session,
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            identity=identity,
            authorization=authorization,
            execution_id=uuid4(),
        )
        session.commit()
    except AuthorizationDenied:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval-run authorization is no longer effective",
        ) from None
    except ApprovalStoreError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None

    ready = []
    try:
        with open_approval_workflow() as workflow:
            for department, revision_id, bundle_id in assessed.ready_revisions:
                target = DepartmentApprovalTarget(
                    approval_run_id=approval_run_id,
                    ticket_id=ticket_id,
                    revision_id=revision_id,
                    department=DepartmentId(department),
                )
                if not workflow.state(target.thread_id).values:
                    workflow.start(target)
                ready.append(
                    {
                        "department": target.department,
                        "revision_id": target.revision_id,
                        "status": "pending_approval",
                        "validation_bundle_id": bundle_id,
                        "ready_for_hitl": True,
                    }
                )
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        raise _approval_http_error(error) from None
    return InitialAssessmentPublic(
        approval_run_id=assessed.approval_run_id,
        outcomes=[
            {"trigger": trigger, "outcome": outcome}
            for trigger, outcome in assessed.outcomes
        ],
        ready_revisions=ready,
        missing_input_codes=list(assessed.missing_input_codes),
    )


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/business-inputs",
    response_model=AuthorizedBusinessInputPublic,
)
def register_approval_business_input(
    ticket_id: UUID,
    approval_run_id: UUID,
    payload: AuthorizedBusinessInputRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> AuthorizedBusinessInputPublic:
    """Persist a human commercial fact under the official responsible's grant."""

    identity = _approval_identity(session, authentication)
    department = getattr(payload.payload, "department", None)
    department_value = department.value if department is not None else None
    target_policy = "proposal_wide" if department is None else "department_exact"
    try:
        authorization = resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="provide_business_input",
                department_scope=department_value,
                target_policy=target_policy,
                affected_departments=(
                    frozenset({department_value})
                    if department_value is not None
                    else frozenset()
                ),
            ),
        )
    except AuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business-input permission is required",
        ) from None
    responsible = _RESPONSIBLE_NAMES.get(identity.responsible_code)
    if responsible is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Official responsible assignment is required",
        )
    principal = AuthenticatedPrincipal(
        issuer=identity.issuer,
        subject=identity.subject,
        trackflow_user_id=identity.trackflow_user_id,
        auth_source=identity.auth_source,
        general_role=identity.general_role,
        synthetic=identity.synthetic,
    )
    actor = AuthorizedHumanActor(
        principal=principal,
        authorization_id=authorization.authorization_id,
        official_responsible=responsible,
        permission="provide_business_input",
        department_scope=department,
    )
    value = AuthorizedBusinessInput(
        input_id=payload.input_id,
        ticket_id=ticket_id,
        approval_run_id=approval_run_id,
        input_kind=payload.input_kind,
        payload=payload.payload,
        provenance=payload.provenance,
        actor=actor,
        payload_hash=authorized_business_input_hash(
            input_kind=payload.input_kind,
            payload=payload.payload,
            provenance=payload.provenance,
            actor=actor,
        ),
        recorded_at=payload.recorded_at,
    )
    try:
        record = record_authorized_business_input(
            session,
            business_input=value,
            identity=identity,
            authorization_reference=AuthorizationReference(
                authorization.authorization_id,
                authorization.authorization_version,
            ),
            idempotency_key=payload.idempotency_key,
            execution_id=uuid4(),
        )
        session.commit()
    except AuthorizationDenied:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business-input authorization is no longer effective",
        ) from None
    except ApprovalStoreError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    return AuthorizedBusinessInputPublic(
        input_id=record.input_id,
        approval_run_id=record.approval_run_id,
        payload_hash=record.payload_hash,
        replayed=record.replayed,
    )


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/departments/{department}/revisions/"
    "{revision_id}/correct",
    response_model=DepartmentCorrectionPublic,
)
def correct_department_revision(
    ticket_id: UUID,
    approval_run_id: UUID,
    department: DepartmentId,
    revision_id: UUID,
    payload: DepartmentCorrectionRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> DepartmentCorrectionPublic:
    """Create one corrected revision from persisted, authorized business inputs."""

    identity = _approval_identity(session, authentication)
    try:
        authorization = resolve_effective_authorization(
            session,
            identity=identity,
            target=AuthorizationTarget(
                permission="provide_business_input",
                department_scope=department.value,
                target_policy="department_exact",
                affected_departments=frozenset({department.value}),
            ),
        )
    except AuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Department business-input permission is required",
        ) from None
    responsible = _RESPONSIBLE_NAMES.get(identity.responsible_code)
    if responsible is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Official responsible assignment is required",
        )
    actor = AuthorizedHumanActor(
        principal=AuthenticatedPrincipal(
            issuer=identity.issuer,
            subject=identity.subject,
            trackflow_user_id=identity.trackflow_user_id,
            auth_source=identity.auth_source,
            general_role=identity.general_role,
            synthetic=identity.synthetic,
        ),
        authorization_id=authorization.authorization_id,
        official_responsible=responsible,
        permission="provide_business_input",
        department_scope=department,
    )
    try:
        corrected = create_corrected_department_from_inputs(
            session,
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            department=department.value,
            predecessor_revision_id=revision_id,
            expected_predecessor_status=payload.expected_predecessor_status,
            expected_predecessor_content_hash=(
                payload.expected_predecessor_content_hash
            ),
            expected_predecessor_state_version=(
                payload.expected_predecessor_state_version
            ),
            authorized_input_ids=tuple(payload.authorized_input_ids),
            identity=identity,
            authorization_reference=AuthorizationReference(
                authorization.authorization_id,
                authorization.authorization_version,
            ),
            actor=actor,
            idempotency_key=payload.idempotency_key,
            requested_at=payload.requested_at,
        )
        session.commit()
    except AuthorizationDenied:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business-input authorization is no longer effective",
        ) from None
    except ApprovalStoreError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    revision = corrected.revision
    return DepartmentCorrectionPublic(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        department=department,
        predecessor_revision_id=revision_id,
        revision_id=corrected.record.revision_id,
        review_revision_no=corrected.record.review_revision_no,
        status=corrected.record.status,
        content_hash=revision.content_hash,
        validation_bundle_id=revision.validation_bundle_id,
        remaining_input_codes=list(corrected.remaining_input_codes),
        ready_for_assessment=corrected.record.status == "pending_approval",
        ready_for_hitl=False,
        replayed=corrected.record.replayed,
    )


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/departments/start",
    response_model=ApprovalWorkflowPublic,
)
def start_department_approval(
    ticket_id: UUID,
    approval_run_id: UUID,
    payload: ApprovalWorkflowStartRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ApprovalWorkflowPublic:
    """Start one persisted departmental branch and return its safe interrupt."""

    _authorize_run_management(session, authentication)
    try:
        assert_current_conflict_clearance(
            session, approval_run_id=approval_run_id, ticket_id=ticket_id
        )
    except ApprovalStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    target = _target(
        ticket_id=ticket_id, approval_run_id=approval_run_id, payload=payload
    )
    try:
        with open_approval_workflow() as workflow:
            workflow.start(target)
            return ApprovalWorkflowPublic.model_validate(
                public_workflow_state(workflow, target.thread_id)
            )
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        raise _approval_http_error(error) from None


@router.get(
    "/approval/inbox",
    response_model=DepartmentReviewInboxPublic,
)
def get_department_approval_inbox(
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> DepartmentReviewInboxPublic:
    """Return only revisions actionable by the authenticated department owner."""

    identity = _approval_identity(session, authentication)
    items = []
    for department in DepartmentId:
        try:
            resolve_effective_authorization(
                session,
                identity=identity,
                target=AuthorizationTarget(
                    permission="approve_department",
                    department_scope=department.value,
                    target_policy="department_exact",
                    affected_departments=frozenset({department.value}),
                ),
            )
        except AuthorizationDenied:
            continue
        candidates = list_department_review_inbox(
            session,
            department=department.value,
            environment=identity.environment,
        )
        for item in candidates:
            try:
                assert_current_conflict_clearance(
                    session,
                    approval_run_id=item["approval_run_id"],
                    ticket_id=item["ticket_id"],
                )
            except ApprovalStoreError:
                continue
            items.append(item)
    return DepartmentReviewInboxPublic(items=items)


@router.get(
    "/{ticket_id}/approval/{approval_run_id}/departments/{department}/"
    "revisions/{revision_id}",
    response_model=ApprovalWorkflowPublic,
)
def get_department_approval(
    ticket_id: UUID,
    approval_run_id: UUID,
    department: DepartmentId,
    revision_id: UUID,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ApprovalWorkflowPublic:
    _authorize_department(session, authentication, department)
    target = DepartmentApprovalTarget(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        revision_id=revision_id,
        department=department,
    )
    try:
        with open_approval_workflow() as workflow:
            return ApprovalWorkflowPublic.model_validate(
                public_workflow_state(workflow, target.thread_id)
            )
    except ApprovalRuntimeError as error:
        raise _approval_http_error(error) from None


@router.get(
    "/{ticket_id}/approval/{approval_run_id}/departments/{department}/"
    "revisions/{revision_id}/review",
    response_model=DepartmentReviewPublic,
)
def get_department_approval_review(
    ticket_id: UUID,
    approval_run_id: UUID,
    department: DepartmentId,
    revision_id: UUID,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> DepartmentReviewPublic:
    """Return the safe preview and immutable evidence required for a decision."""

    _authorize_department(session, authentication, department)
    try:
        review = DepartmentReviewPublic.model_validate(
            get_department_review(
                session,
                approval_run_id=approval_run_id,
                ticket_id=ticket_id,
                revision_id=revision_id,
                department=department.value,
            )
        )
    except ApprovalStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.code
        ) from None
    if review.revision_status != "pending_approval":
        return review.model_copy(update={"allowed_actions": []})
    try:
        assert_current_conflict_clearance(
            session, approval_run_id=approval_run_id, ticket_id=ticket_id
        )
    except ApprovalStoreError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    return review


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/departments/{department}/"
    "revisions/{revision_id}/decision",
    response_model=ApprovalWorkflowPublic,
)
def resume_department_approval(
    ticket_id: UUID,
    approval_run_id: UUID,
    department: DepartmentId,
    revision_id: UUID,
    payload: ApprovalDecisionRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ApprovalWorkflowPublic:
    """Resume exactly one interrupted branch with authenticated human input."""

    _authorize_department(session, authentication, department)
    review = get_department_review(
        session,
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        revision_id=revision_id,
        department=department.value,
    )
    pending_codes = [
        item["code"]
        for item in review["pending_commercial_items"]
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    ]
    if (
        payload.action == "approve"
        and pending_codes
        and not payload.acknowledge_pending_commercial_items
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pending_commercial_items_acknowledgement_required",
        )
    decision_reason = payload.reason
    if payload.action == "approve" and pending_codes:
        decision_reason = indicative_approval_reason(pending_codes, payload.reason)
    target = DepartmentApprovalTarget(
        approval_run_id=approval_run_id,
        ticket_id=ticket_id,
        revision_id=revision_id,
        department=department,
    )
    try:
        with open_approval_workflow() as workflow:
            workflow.resume(
                thread_id=target.thread_id,
                command=new_human_resume(action=payload.action, reason=decision_reason),
                authentication=authentication.context,
            )
            return ApprovalWorkflowPublic.model_validate(
                public_workflow_state(workflow, target.thread_id)
            )
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        raise _approval_http_error(error) from None


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/reconcile",
    response_model=ApprovalWorkflowPublic,
)
def reconcile_approval_completion(
    ticket_id: UUID,
    approval_run_id: UUID,
    payload: ApprovalWorkflowStartRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ApprovalWorkflowPublic:
    """Recover fan-in/finalization after a committed decision or process crash."""

    _authorize_run_management(session, authentication)
    target = _target(
        ticket_id=ticket_id, approval_run_id=approval_run_id, payload=payload
    )
    try:
        with open_approval_workflow() as workflow:
            workflow.reconcile(target.thread_id)
            return ApprovalWorkflowPublic.model_validate(
                public_workflow_state(workflow, target.thread_id)
            )
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        raise _approval_http_error(error) from None


@router.post(
    "/{ticket_id}/approval/{approval_run_id}/proposal/materialize-and-reconcile",
    response_model=ProposalMaterializationPublic,
)
def materialize_and_reconcile_approval(
    ticket_id: UUID,
    approval_run_id: UUID,
    payload: ApprovalWorkflowStartRequest,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> ProposalMaterializationPublic:
    """Import unchanged Part 2 terms under a live run-management grant.

    This is an operational coordination action, not another business approval.
    It fails closed if Parte 2 global terms need a Miguel-authorized correction.
    """

    identity, authorization = _authorize_run_management(session, authentication)
    target = _target(
        ticket_id=ticket_id, approval_run_id=approval_run_id, payload=payload
    )
    try:
        materialized = materialize_initial_proposal(
            session,
            approval_run_id=approval_run_id,
            ticket_id=ticket_id,
            identity=identity,
            authorization=authorization,
        )
        session.commit()
    except AuthorizationDenied:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval-run authorization is no longer effective",
        ) from None
    except ApprovalStoreError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from None
    try:
        with open_approval_workflow() as workflow:
            workflow.reconcile(target.thread_id)
            coordination = ApprovalWorkflowPublic.model_validate(
                public_workflow_state(workflow, target.thread_id)
            )
    except (ApprovalWorkflowError, ApprovalRuntimeError) as error:
        raise _approval_http_error(error) from None
    return ProposalMaterializationPublic(
        proposal_revision_id=materialized.proposal_revision_id,
        approval_run_id=materialized.approval_run_id,
        eligible_for_synthesis=materialized.eligible_for_synthesis,
        replayed=materialized.replayed,
        coordination=coordination,
    )


@router.get(
    "/{ticket_id}/approval/document-locator",
    response_model=FinalDocumentLocatorPublic,
)
def get_approval_final_document_locator(
    ticket_id: UUID,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> FinalDocumentLocatorPublic:
    identity, _authorization = _authorize_run_management(session, authentication)
    locator = get_final_document_locator(
        session,
        ticket_id=ticket_id,
        environment=identity.environment,
    )
    if locator is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Final document not found",
        )
    return FinalDocumentLocatorPublic(
        ticket_id=locator.ticket_id,
        approval_run_id=locator.approval_run_id,
        final_document_id=locator.final_document_id,
    )


@router.get(
    "/{ticket_id}/approval/{approval_run_id}/document",
    response_model=PublicFinalDocument,
)
def get_approval_final_document(
    ticket_id: UUID,
    approval_run_id: UUID,
    authentication: Annotated[
        RFPApprovalAuthentication, Depends(get_rfp_approval_authentication)
    ],
    session: Annotated[Session, Depends(get_db)],
) -> PublicFinalDocument:
    _authorize_run_management(session, authentication)
    document = get_public_final_document(
        session, approval_run_id=approval_run_id, ticket_id=ticket_id
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Final document not found",
        )
    return document


@router.get("/{ticket_id}", response_model=RFPDetailResponse)
def get_rfp_ticket(ticket_id: UUID, _user: Annotated[UserInDB, Depends(require_rfp_admin)],
                   session: Annotated[Session, Depends(get_db)]) -> RFPDetailResponse:
    result = get_rfp(session, ticket_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFP ticket not found")
    ticket, metadata, job, handoff = result["ticket"], result["metadata"], result["job"], result["handoff"]
    return RFPDetailResponse(
        **{key: ticket[key] for key in ("ticket_id", "rfp_id", "status", "discard_reason_code", "discard_summary", "created_at", "updated_at")},
        job=None if job is None else RFPJobPublic(**{key: job[key] for key in (
            "status", "attempt_count", "max_attempts", "last_error_code", "last_error_summary"
        )}),
        metadata=None if metadata is None else RFPMetadataPublic(**metadata),
        department_sections=[DepartmentSectionPublic(
            responsible=RESPONSIBLE_BY_DEPARTMENT[DepartmentId(row["department_id"])], **row
        ) for row in result["sections"]],
        handoff=None if handoff is None else RFPHandoffPublic(
            **{**handoff["payload"], "summary_for_sales": handoff["summary_for_sales"]}
        ),
    )
