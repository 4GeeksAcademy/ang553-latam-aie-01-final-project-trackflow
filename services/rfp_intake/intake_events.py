"""Durable acceptance and notification ledger for valid RFPs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_serializer, field_validator
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Session

from data.pipelines.rfp_intake.contracts import DepartmentId, RFPIntakeDecision, StrictContract
from services.rfp_intake.models import rfp_jobs, rfp_tickets

EVENT_TYPE = "rfp_ticket_created"
rfp_sse_metadata_registry = MetaData()

rfp_intake_decisions = Table(
    "rfp_intake_decisions", rfp_sse_metadata_registry,
    Column("ticket_id", PGUUID(as_uuid=True), ForeignKey(rfp_tickets.c.ticket_id), primary_key=True),
    Column("decision", JSONB, nullable=False),
)
rfp_sse_cursor = Table(
    "rfp_sse_cursor", rfp_sse_metadata_registry,
    Column("singleton", Boolean, primary_key=True),
    Column("last_cursor", BigInteger, nullable=False),
    Column("purged_through", BigInteger, nullable=False),
)
rfp_sse_events = Table(
    "rfp_sse_events", rfp_sse_metadata_registry,
    Column("cursor", BigInteger, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("ticket_id", PGUUID(as_uuid=True), ForeignKey("rfp_intake_decisions.ticket_id"), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()),
)


class RFPTicketCreated(StrictContract):
    ticket_id: UUID
    rfp_id: Annotated[str, Field(max_length=160)] | None
    client_name: Annotated[str, Field(max_length=200)] | None
    client_country: Literal["ES", "US"] | None
    services_requested: list[DepartmentId] = Field(min_length=1, max_length=3)
    status: Literal["analyzing"] = "analyzing"
    created_at: AwareDatetime

    @field_validator("services_requested")
    @classmethod
    def unique_services(cls, services: list[DepartmentId]) -> list[DepartmentId]:
        if len(set(services)) != len(services):
            raise ValueError("duplicate service identifier")
        return services

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class IntakeClaimLost(RuntimeError):
    """No further work may be committed on the supplied claim."""


def require_intake_claim(session: Session, job: dict[str, Any]) -> None:
    """Lock the exact claim and check its lease using the database clock."""
    valid_claim = (
        rfp_jobs.c.job_id == job["job_id"],
        rfp_jobs.c.ticket_id == job["ticket_id"],
        rfp_jobs.c.job_kind == "intake",
        rfp_jobs.c.status == "running",
        rfp_jobs.c.claim_token == job["claim_token"],
        rfp_jobs.c.execution_epoch == job["execution_epoch"],
    )
    locked = session.execute(
        select(rfp_jobs.c.job_id).where(*valid_claim).with_for_update()
    ).scalar_one_or_none()
    if locked is None or session.execute(
        select(rfp_jobs.c.job_id).where(*valid_claim, rfp_jobs.c.lease_until > func.clock_timestamp())
    ).scalar_one_or_none() is None:
        raise IntakeClaimLost("RFP intake claim is no longer current")


def get_intake_decision(session: Session, ticket_id: UUID) -> RFPIntakeDecision | None:
    stored = session.execute(
        select(rfp_intake_decisions.c.decision).where(rfp_intake_decisions.c.ticket_id == ticket_id)
    ).scalar_one_or_none()
    if stored is None:
        return None
    decision = RFPIntakeDecision.model_validate_json(json.dumps(stored))
    if decision.ticket_id != ticket_id:
        raise ValueError("Stored RFP decision does not match its ticket")
    return decision


def record_intake_decision(
    session: Session, job: dict[str, Any], decision: RFPIntakeDecision,
) -> None:
    """Add the decision and event to the caller's transaction exactly once."""
    if decision.ticket_id != job["ticket_id"]:
        raise ValueError("RFP decision does not match the claimed ticket")
    require_intake_claim(session, job)
    existing = get_intake_decision(session, decision.ticket_id)
    if existing is not None:
        if existing != decision:
            raise ValueError("RFP intake decision conflicts with committed decision")
        return
    created_at = session.execute(select(rfp_tickets.c.created_at).where(
        rfp_tickets.c.ticket_id == decision.ticket_id,
        rfp_tickets.c.status == "analizando",
    )).scalar_one_or_none()
    if created_at is None:
        raise IntakeClaimLost("RFP ticket is no longer in intake")
    metadata = decision.metadata
    payload = RFPTicketCreated(
        ticket_id=decision.ticket_id,
        rfp_id=metadata.rfp_id,
        client_name=metadata.client_name,
        client_country=metadata.client_country_code,
        services_requested=metadata.departments_needed,
        created_at=created_at,
    )
    session.execute(insert(rfp_intake_decisions).values(
        ticket_id=decision.ticket_id,
        decision=decision.model_dump(mode="json"),
    ))
    cursor = session.execute(update(rfp_sse_cursor).where(rfp_sse_cursor.c.singleton.is_(True))
        .values(last_cursor=rfp_sse_cursor.c.last_cursor + 1)
        .returning(rfp_sse_cursor.c.last_cursor)).scalar_one()
    session.execute(insert(rfp_sse_events).values(
        cursor=cursor,
        event_type=EVENT_TYPE,
        ticket_id=decision.ticket_id,
        payload=payload.model_dump(mode="json"),
    ))
    # The counter update can block behind another publisher. Check again
    # immediately before returning to the sink for COMMIT.
    require_intake_claim(session, job)
