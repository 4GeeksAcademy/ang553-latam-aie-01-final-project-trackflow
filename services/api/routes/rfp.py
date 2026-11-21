"""Authenticated RFP intake endpoints on the existing FastAPI service."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RESPONSIBLE_BY_DEPARTMENT
from services.api.auth_models import Role, UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
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


def require_rfp_admin(user: Annotated[UserInDB, Depends(get_current_user)]) -> UserInDB:
    if user.role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


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
