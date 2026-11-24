"""The commercial read projection and retained cursor for the RFP feed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Column, DateTime, ForeignKeyConstraint, String, Table,
    and_, delete, func, or_, select, update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId
from services.rfp_approval.models import rfp_principals
from services.rfp_approval.identity import ResolvedIdentity
from services.rfp_intake.contracts import JobStatus, TicketStatus
from services.rfp_intake.intake_events import (
    EVENT_TYPE, RFPTicketCreated, rfp_intake_decisions, rfp_sse_cursor, rfp_sse_events,
    rfp_sse_metadata_registry,
)
from services.rfp_intake.models import rfp_jobs, rfp_metadata, rfp_tickets


rfp_sse_commercial_grants = Table(
    "rfp_sse_commercial_grants", rfp_sse_metadata_registry,
    Column("principal_id", PGUUID(as_uuid=True), primary_key=True),
    Column("environment", String, primary_key=True),
    Column("granted_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ("principal_id", "environment"),
        (rfp_principals.c.principal_id, rfp_principals.c.environment),
    ),
)


class CommercialRFPTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: UUID
    rfp_id: str | None
    client_name: str | None
    client_country: Literal["ES", "US"] | None
    services_requested: list[DepartmentId]
    status: TicketStatus
    job_status: JobStatus | None
    created_at: datetime
    updated_at: datetime


class CommercialRFPSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CommercialRFPTicket]
    total: int
    cursor: str


class CursorResetRequired(RuntimeError):
    """The client must obtain a new snapshot before opening the stream."""


def commercial_grant_active(session: Session, identity: ResolvedIdentity) -> bool:
    grant = rfp_sse_commercial_grants.c
    now = func.statement_timestamp()
    return session.execute(select(grant.principal_id).where(
        grant.principal_id == identity.principal_id,
        grant.environment == identity.environment,
        grant.granted_at <= now,
        or_(grant.expires_at.is_(None), grant.expires_at > now),
        grant.revoked_at.is_(None),
    )).scalar_one_or_none() is not None


_visible_tickets = rfp_tickets.outerjoin(
    rfp_intake_decisions, rfp_intake_decisions.c.ticket_id == rfp_tickets.c.ticket_id,
).outerjoin(rfp_metadata, rfp_metadata.c.ticket_id == rfp_tickets.c.ticket_id)
_is_visible = and_(
    rfp_tickets.c.status != "descartado",
    or_(
        rfp_intake_decisions.c.ticket_id.is_not(None),
        and_(rfp_tickets.c.status != "analizando", rfp_metadata.c.ticket_id.is_not(None)),
    ),
)
_early_metadata = rfp_intake_decisions.c.decision["metadata"]
_intake_job_status = (
    select(rfp_jobs.c.status)
    .where(rfp_jobs.c.ticket_id == rfp_tickets.c.ticket_id, rfp_jobs.c.job_kind == "intake")
    .order_by(rfp_jobs.c.created_at.desc(), rfp_jobs.c.job_id.desc())
    .limit(1).scalar_subquery()
)


def _ticket_projection():
    return select(
        rfp_tickets.c.ticket_id, rfp_tickets.c.created_at, rfp_tickets.c.updated_at,
        rfp_tickets.c.status, _intake_job_status.label("job_status"),
        func.coalesce(rfp_tickets.c.rfp_id, _early_metadata["rfp_id"].astext).label("rfp_id"),
        func.coalesce(rfp_metadata.c.client_name, _early_metadata["client_name"].astext).label("client_name"),
        func.coalesce(rfp_metadata.c.client_country_code, _early_metadata["client_country_code"].astext).label("client_country"),
        func.coalesce(rfp_metadata.c.departments_needed, _early_metadata["departments_needed"]).label("services_requested"),
    ).select_from(_visible_tickets).where(_is_visible)


def _position(session: Session) -> tuple[int, int]:
    cursor = rfp_sse_cursor.c
    last, purged = session.execute(
        select(cursor.last_cursor, cursor.purged_through).where(cursor.singleton.is_(True))
    ).one()
    return last, purged


def commercial_snapshot(session: Session, *, limit: int, offset: int) -> CommercialRFPSnapshot:
    """Read the page and watermark within the caller's repeatable-read view."""
    last, _ = _position(session)
    rows = session.execute(_ticket_projection()
        .order_by(rfp_tickets.c.created_at.desc(), rfp_tickets.c.ticket_id.desc())
        .limit(limit).offset(offset)).mappings()
    total = session.execute(
        select(func.count()).select_from(_visible_tickets).where(_is_visible)
    ).scalar_one()
    return CommercialRFPSnapshot(
        items=[CommercialRFPTicket.model_validate(row) for row in rows], total=total, cursor=str(last),
    )


def commercial_ticket(session: Session, ticket_id: UUID) -> CommercialRFPTicket | None:
    row = session.execute(
        _ticket_projection().where(rfp_tickets.c.ticket_id == ticket_id)
    ).mappings().one_or_none()
    return None if row is None else CommercialRFPTicket.model_validate(row)


def read_event_batch(session: Session, *, after: int, limit: int) -> list[tuple[int, RFPTicketCreated]]:
    """Check retention and read rows from the same repeatable-read view."""
    last, purged = _position(session)
    if not purged <= after <= last:
        raise CursorResetRequired
    rows = session.execute(
        select(rfp_sse_events.c.cursor, rfp_sse_events.c.payload)
        .where(rfp_sse_events.c.cursor > after, rfp_sse_events.c.event_type == EVENT_TYPE)
        .order_by(rfp_sse_events.c.cursor).limit(limit)
    )
    return [(cursor, RFPTicketCreated.model_validate_json(json.dumps(payload))) for cursor, payload in rows]


def purge_event_prefix(session: Session, *, days: int, limit: int) -> int:
    """Purge one expired, contiguous prefix and advance its floor atomically."""
    if not 1 <= days <= 365 or not 1 <= limit <= 10_000:
        raise ValueError("RFP retention configuration is outside the supported range")
    cursor = rfp_sse_cursor.c
    purged = session.execute(select(cursor.purged_through)
        .where(cursor.singleton.is_(True)).with_for_update()).scalar_one()
    events = rfp_sse_events.c
    cutoff = session.execute(select(func.clock_timestamp())).scalar_one() - timedelta(days=days)
    rows = session.execute(select(events.cursor, events.recorded_at)
        .where(events.cursor > purged).order_by(events.cursor).limit(limit))
    through = purged
    for event_cursor, recorded_at in rows:
        if event_cursor != through + 1 or recorded_at >= cutoff:
            break
        through = event_cursor
    if through == purged:
        return 0
    session.execute(delete(rfp_sse_events).where(events.cursor > purged, events.cursor <= through))
    session.execute(update(rfp_sse_cursor).where(cursor.singleton.is_(True)).values(purged_through=through))
    return through - purged
