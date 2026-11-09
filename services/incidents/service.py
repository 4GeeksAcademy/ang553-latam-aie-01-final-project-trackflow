"""Tenant-aware incident domain service."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlmodel import Session

from services.api.access_control import AccessContext
from services.api.auth_models import Role
from services.incidents.contracts import (
    IncidentCreate,
    IncidentHistoryResponse,
    IncidentResponse,
    IncidentUpdate,
)
from services.incidents.models import clients
from services.incidents.repository import (
    get_history,
    get_incident,
    insert_history,
    insert_incident,
    update_incident_status,
)


class IncidentNotFound(RuntimeError):
    pass


class IncidentForbidden(RuntimeError):
    pass


class IncidentConflict(RuntimeError):
    pass


class IncidentValidationError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS = {
    ("OPEN", "CLOSED"),
    ("OPEN", "DISCARDED"),
    ("CLOSED", "OPEN"),
}


def _ensure_write_access(access: AccessContext, client_id: str) -> None:
    if access.role is Role.user or not access.can_access_client(client_id):
        raise IncidentForbidden("incident write not authorized")


def _client_exists(session: Session, client_id: str) -> bool:
    return session.execute(
        select(clients.c.client_id).where(
            clients.c.client_id == client_id,
            clients.c.is_active.is_(True),
        )
    ).first() is not None


def _to_response(
    row: dict[str, object], history: list[dict[str, object]], access: AccessContext
) -> IncidentResponse:
    client_id = str(row["client_id"])
    return IncidentResponse(
        incident_id=str(row["incident_id"]),
        incident_date=row["incident_date"],
        country=str(row["country"]),
        customer_type=str(row["customer_type"]),
        tracking_number=str(row["tracking_number"]),
        carrier=str(row["carrier"]),
        category=str(row["category"]),
        description=str(row["description"]),
        status=str(row["status"]),
        customer_email=(
            None
            if not access.can_view_sensitive(client_id)
            else row.get("customer_email")
        ),
        source_channel=str(row["source_channel"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=int(row["version"]),
        history=[IncidentHistoryResponse(**item) for item in history],
    )


def create_incident(
    session: Session, payload: IncidentCreate, access: AccessContext
) -> IncidentResponse:
    _ensure_write_access(access, payload.client_id)
    if not _client_exists(session, payload.client_id):
        raise IncidentForbidden("incident write not authorized")

    now = datetime.now(timezone.utc)
    try:
        row = insert_incident(
            session,
            {
                "client_id": payload.client_id,
                "incident_date": payload.incident_date,
                "country": payload.country,
                "customer_type": payload.customer_type,
                "tracking_number": payload.tracking_number,
                "carrier": payload.carrier,
                "category": payload.category,
                "description": payload.description,
                "status": "OPEN",
                "customer_email": payload.customer_email,
                "satisfaction_score": None,
                "source_channel": payload.source_channel.value,
                "source_reference": payload.source_reference,
                "created_by_user_id": access.user_id,
                "updated_by_user_id": access.user_id,
                "created_at": now,
                "updated_at": now,
                "version": 1,
            },
        )
        insert_history(
            session,
            {
                "incident_pk": row["id"],
                "version": 1,
                "event_type": "CREATED",
                "previous_status": None,
                "new_status": "OPEN",
                "reason": None,
                "actor_user_id": access.user_id,
                "occurred_at": now,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _to_response(row, get_history(session, int(row["id"])), access)


def read_incident(
    session: Session, incident_id: str, access: AccessContext
) -> IncidentResponse:
    row = get_incident(session, incident_id, access)
    if row is None:
        raise IncidentNotFound("incident not found")
    return _to_response(row, get_history(session, int(row["id"])), access)


def change_incident_status(
    session: Session,
    incident_id: str,
    payload: IncidentUpdate,
    access: AccessContext,
) -> IncidentResponse:
    if access.role is Role.user:
        raise IncidentForbidden("incident write not authorized")
    row = get_incident(session, incident_id, access, for_update=True)
    if row is None:
        raise IncidentNotFound("incident not found")
    if int(row["version"]) != payload.expected_version:
        session.rollback()
        raise IncidentConflict("incident version conflict")
    previous = str(row["status"])
    transition = (previous, payload.status)
    if transition not in _ALLOWED_TRANSITIONS:
        session.rollback()
        raise IncidentValidationError("status transition not allowed")
    if transition == ("CLOSED", "OPEN") and not payload.reason:
        session.rollback()
        raise IncidentValidationError("reopening requires a reason")
    changed = update_incident_status(
        session,
        incident_pk=int(row["id"]),
        expected_version=payload.expected_version,
        status=payload.status,
        satisfaction_score=payload.satisfaction_score,
        actor_user_id=access.user_id,
    )
    if not changed:
        session.rollback()
        raise IncidentConflict("incident version conflict")
    insert_history(
        session,
        {
            "incident_pk": row["id"],
            "version": payload.expected_version + 1,
            "event_type": "STATUS_CHANGED",
            "previous_status": previous,
            "new_status": payload.status,
            "reason": payload.reason,
            "actor_user_id": access.user_id,
            "occurred_at": datetime.now(timezone.utc),
        },
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return read_incident(session, incident_id, access)
