"""Authenticated operational incident query and mutation API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from scripts.incidents import INCIDENT_ID_PATTERN
from services.api.access_control import build_access_context
from services.api.auth_models import UserInDB
from services.api.auth_security import get_current_user
from services.api.database import get_db
from services.incidents.contracts import IncidentCreate, IncidentResponse, IncidentUpdate
from services.incidents.service import (
    IncidentConflict,
    IncidentForbidden,
    IncidentNotFound,
    IncidentValidationError,
    change_incident_status,
    create_incident,
    read_incident,
)

router = APIRouter(prefix="/api/incidents", tags=["Operational incidents"])


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, IncidentNotFound):
        return HTTPException(status_code=404, detail="Incident not found.")
    if isinstance(exc, IncidentForbidden):
        return HTTPException(status_code=403, detail="Incident operation not authorized.")
    if isinstance(exc, IncidentConflict):
        return HTTPException(status_code=409, detail="Incident version conflict.")
    if isinstance(exc, IncidentValidationError):
        return HTTPException(status_code=422, detail="Incident state change is invalid.")
    return HTTPException(status_code=503, detail="Incident service unavailable.")


def _validate_id(incident_id: str) -> None:
    if not INCIDENT_ID_PATTERN.fullmatch(incident_id):
        raise HTTPException(status_code=404, detail="Incident not found.")


@router.post("", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def post_incident(
    payload: IncidentCreate,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> IncidentResponse:
    try:
        return create_incident(session, payload, build_access_context(session, user))
    except IncidentValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail="Incident data is invalid.") from exc
    except (IncidentForbidden, SQLAlchemyError) as exc:
        session.rollback()
        raise _translate(exc) from exc


@router.get("/{incident_id}", response_model=IncidentResponse)
def get_incident_by_id(
    incident_id: str,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> IncidentResponse:
    _validate_id(incident_id)
    try:
        return read_incident(session, incident_id, build_access_context(session, user))
    except (IncidentNotFound, SQLAlchemyError) as exc:
        raise _translate(exc) from exc


def _patch_incident_status(
    incident_id: str,
    payload: IncidentUpdate,
    session: Session,
    user: UserInDB,
) -> IncidentResponse:
    _validate_id(incident_id)
    try:
        return change_incident_status(session, incident_id, payload, build_access_context(session, user))
    except (IncidentNotFound, IncidentForbidden, IncidentConflict, IncidentValidationError, SQLAlchemyError) as exc:
        session.rollback()
        raise _translate(exc) from exc


@router.patch("/{incident_id}", response_model=IncidentResponse)
def patch_incident(
    incident_id: str,
    payload: IncidentUpdate,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> IncidentResponse:
    return _patch_incident_status(incident_id, payload, session, user)


@router.patch("/{incident_id}/status", response_model=IncidentResponse)
def patch_incident_status(
    incident_id: str,
    payload: IncidentUpdate,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[UserInDB, Depends(get_current_user)],
) -> IncidentResponse:
    return _patch_incident_status(incident_id, payload, session, user)
