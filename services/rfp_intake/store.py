"""PostgreSQL repository for RFP acceptance, querying and fenced execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, select, text, update
from sqlmodel import Session

from services.rfp_intake.models import (
    rfp_department_sections,
    rfp_handoffs,
    rfp_jobs,
    rfp_metadata,
    rfp_outbox,
    rfp_tickets,
)
from services.task_queue.sanitization import safe_code, sanitize_message


def accept_rfp(session: Session, *, ticket_id: UUID, raw_pdf_path: str, uploaded_by_subject: str) -> UUID:
    """Create ticket, job and first outbox row in the caller's transaction."""
    job_id, outbox_id = uuid4(), uuid4()
    session.execute(insert(rfp_tickets).values(
        ticket_id=ticket_id, status="analizando", raw_pdf_path=raw_pdf_path,
        uploaded_by_subject=uploaded_by_subject,
    ))
    session.execute(insert(rfp_jobs).values(job_id=job_id, ticket_id=ticket_id, status="queued"))
    session.execute(insert(rfp_outbox).values(
        outbox_id=outbox_id, job_id=job_id, attempt_no=1, status="pending",
    ))
    return job_id


def list_rfps(session: Session, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    latest_job = (
        select(rfp_jobs.c.ticket_id, rfp_jobs.c.status.label("job_status"),
               func.row_number().over(partition_by=rfp_jobs.c.ticket_id, order_by=rfp_jobs.c.created_at.desc()).label("rn"))
        .subquery()
    )
    statement = (
        select(
            rfp_tickets.c.ticket_id, rfp_tickets.c.rfp_id, rfp_tickets.c.status,
            rfp_tickets.c.created_at, rfp_tickets.c.updated_at,
            rfp_metadata.c.client_name, rfp_metadata.c.currency,
            rfp_metadata.c.departments_needed, latest_job.c.job_status,
        )
        .select_from(rfp_tickets.outerjoin(rfp_metadata).outerjoin(latest_job, and_(latest_job.c.ticket_id == rfp_tickets.c.ticket_id, latest_job.c.rn == 1)))
        .order_by(rfp_tickets.c.created_at.desc()).limit(limit).offset(offset)
    )
    rows = [dict(row) for row in session.execute(statement).mappings().all()]
    total = int(session.execute(select(func.count()).select_from(rfp_tickets)).scalar_one())
    return rows, total


def get_rfp(session: Session, ticket_id: UUID) -> dict[str, Any] | None:
    ticket = session.execute(select(rfp_tickets).where(rfp_tickets.c.ticket_id == ticket_id)).mappings().first()
    if ticket is None:
        return None
    metadata = session.execute(select(rfp_metadata).where(rfp_metadata.c.ticket_id == ticket_id)).mappings().first()
    job = session.execute(select(rfp_jobs).where(rfp_jobs.c.ticket_id == ticket_id).order_by(rfp_jobs.c.created_at.desc()).limit(1)).mappings().first()
    sections = session.execute(select(rfp_department_sections).where(rfp_department_sections.c.ticket_id == ticket_id).order_by(rfp_department_sections.c.department_id)).mappings().all()
    handoff = session.execute(select(rfp_handoffs).where(rfp_handoffs.c.ticket_id == ticket_id)).mappings().first()
    return {"ticket": dict(ticket), "metadata": None if metadata is None else dict(metadata),
            "job": None if job is None else dict(job), "sections": [dict(row) for row in sections],
            "handoff": None if handoff is None else dict(handoff)}


def ticket_exists(session: Session, ticket_id: UUID) -> bool:
    """Reconcile an uncertain acceptance COMMIT from a fresh DB session."""
    return session.execute(
        select(rfp_tickets.c.ticket_id).where(rfp_tickets.c.ticket_id == ticket_id)
    ).scalar_one_or_none() is not None


def claim_job(session: Session, job_id: UUID, *, lease_seconds: int) -> dict[str, Any] | None:
    token = uuid4()
    row = session.execute(text("""
      UPDATE rfp_jobs SET status='running',attempt_count=attempt_count+1,
        claim_token=:token,execution_epoch=execution_epoch+1,
        lease_until=clock_timestamp()+(:lease*interval '1 second'),heartbeat_at=clock_timestamp(),
        last_error_code=NULL,last_error_summary=NULL,updated_at=clock_timestamp()
      WHERE job_id=:job_id AND attempt_count<max_attempts AND (
        status='queued' OR (status='retry_wait' AND next_attempt_at<=clock_timestamp()) OR
        (status='running' AND lease_until<=clock_timestamp()))
      RETURNING *
    """), {"job_id": job_id, "token": token, "lease": lease_seconds}).mappings().first()
    return None if row is None else dict(row)


def heartbeat_job(session: Session, *, job_id: UUID, token: UUID, epoch: int, lease_seconds: int) -> bool:
    row = session.execute(text("""
      UPDATE rfp_jobs SET lease_until=clock_timestamp()+(:lease*interval '1 second'),
        heartbeat_at=clock_timestamp(),updated_at=clock_timestamp()
      WHERE job_id=:job_id AND status='running' AND claim_token=:token AND execution_epoch=:epoch
      RETURNING job_id
    """), {"job_id": job_id, "token": token, "epoch": epoch, "lease": lease_seconds}).first()
    return row is not None


def schedule_retry(session: Session, *, job: dict[str, Any], error_code: str, error_summary: str) -> bool:
    attempt = int(job["attempt_count"])
    if attempt >= int(job["max_attempts"]):
        return finish_job(session, job=job, status="dead_letter", error_code=error_code, error_summary=error_summary)
    delay = (30, 120)[attempt - 1]
    next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    changed = session.execute(update(rfp_jobs).where(
        rfp_jobs.c.job_id == job["job_id"], rfp_jobs.c.status == "running",
        rfp_jobs.c.claim_token == job["claim_token"], rfp_jobs.c.execution_epoch == job["execution_epoch"],
    ).values(status="retry_wait", next_attempt_at=next_at, claim_token=None, lease_until=None,
             last_error_code=safe_code(error_code), last_error_summary=sanitize_message(error_summary), updated_at=func.now())).rowcount == 1
    if changed:
        session.execute(insert(rfp_outbox).values(outbox_id=uuid4(), job_id=job["job_id"],
            attempt_no=attempt + 1, status="pending", next_publish_at=next_at))
    return changed


def finish_job(session: Session, *, job: dict[str, Any], status: str,
               error_code: str | None = None, error_summary: str | None = None) -> bool:
    return session.execute(update(rfp_jobs).where(
        rfp_jobs.c.job_id == job["job_id"], rfp_jobs.c.status == "running",
        rfp_jobs.c.claim_token == job["claim_token"], rfp_jobs.c.execution_epoch == job["execution_epoch"],
    ).values(status=status, claim_token=None, lease_until=None, next_attempt_at=None,
             last_error_code=None if error_code is None else safe_code(error_code),
             last_error_summary=None if error_summary is None else sanitize_message(error_summary),
             updated_at=func.now())).rowcount == 1


def claim_outbox(session: Session, *, lease_seconds: int) -> dict[str, Any] | None:
    token = uuid4()
    row = session.execute(text("""
      SELECT outbox_id FROM rfp_outbox
      WHERE status IN ('pending','publish_failed') AND next_publish_at<=clock_timestamp()
        AND publish_attempts<3 AND (lease_until IS NULL OR lease_until<=clock_timestamp())
      ORDER BY next_publish_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1
    """)).first()
    if row is None:
        return None
    claimed = session.execute(text("""
      UPDATE rfp_outbox SET lease_token=:token,
        lease_until=clock_timestamp()+(:lease*interval '1 second'),publish_attempts=publish_attempts+1,
        updated_at=clock_timestamp() WHERE outbox_id=:outbox_id RETURNING *
    """), {"token": token, "lease": lease_seconds, "outbox_id": row[0]}).mappings().one()
    result = dict(claimed)
    result["job_kind"] = session.execute(select(rfp_jobs.c.job_kind).where(
        rfp_jobs.c.job_id == result["job_id"])).scalar_one()
    return result


def finish_outbox(session: Session, *, claimed: dict[str, Any], published: bool, error_code: str | None = None) -> None:
    attempts = int(claimed["publish_attempts"])
    if published:
        session.execute(update(rfp_outbox).where(rfp_outbox.c.outbox_id == claimed["outbox_id"], rfp_outbox.c.lease_token == claimed["lease_token"]).values(
            status="published", lease_token=None, lease_until=None, last_error_code=None, updated_at=func.now()))
        return
    delay = 30 if attempts == 1 else 120
    session.execute(update(rfp_outbox).where(rfp_outbox.c.outbox_id == claimed["outbox_id"], rfp_outbox.c.lease_token == claimed["lease_token"]).values(
        status="publish_failed", lease_token=None, lease_until=None, last_error_code=safe_code(error_code),
        next_publish_at=datetime.now(timezone.utc) + timedelta(seconds=delay), updated_at=func.now()))
    if attempts >= 3:
        session.execute(update(rfp_jobs).where(rfp_jobs.c.job_id == claimed["job_id"], rfp_jobs.c.status.in_(["queued", "retry_wait"])).values(
            status="dead_letter", last_error_code="publish_attempts_exhausted", last_error_summary="task publication failed", updated_at=func.now()))
