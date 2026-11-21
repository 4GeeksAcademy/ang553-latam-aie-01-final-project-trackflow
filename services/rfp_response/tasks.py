"""Durable Celery entrypoint for recoverable RFP response generation."""

from __future__ import annotations

import os
from threading import Event, Thread
from typing import Any
from uuid import UUID

from celery.exceptions import Retry
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from data.pipelines.rfp_response.graph import ResponseGraphError
from pipelines.database_identity import sqlalchemy_postgresql_url
from services.rfp_intake.config import RFPSettings, get_rfp_settings
from services.rfp_intake.models import rfp_jobs
from services.rfp_intake.store import (
    claim_job,
    finish_job,
    heartbeat_job,
    schedule_retry,
)
from services.rfp_response.models import rfp_response_handoffs, rfp_response_runs
from services.rfp_response.runtime import process_response_run
from services.rfp_response.store import claim_response_run, heartbeat_response_run
from services.task_queue.celery_app import celery_app


def _engine() -> Any:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise RuntimeError("RFP response processing requires PostgreSQL DATABASE_URL")
    return create_engine(sqlalchemy_postgresql_url(database_url), pool_pre_ping=True)


class _ResponseHeartbeat:
    def __init__(self, engine: Any, job: dict[str, Any], run: dict[str, Any],
                 settings: RFPSettings) -> None:
        self.engine, self.job, self.run, self.settings = engine, job, run, settings
        self.stop = Event()
        self.lost = Event()
        self.thread = Thread(target=self._renew, name="rfp-response-heartbeat", daemon=True)

    def __enter__(self) -> "_ResponseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=min(5, self.settings.heartbeat_seconds))

    def _renew(self) -> None:
        while not self.stop.wait(self.settings.heartbeat_seconds):
            try:
                with Session(self.engine) as session:
                    job_alive = heartbeat_job(
                        session, job_id=self.job["job_id"], token=self.job["claim_token"],
                        epoch=self.job["execution_epoch"],
                        lease_seconds=self.settings.lease_seconds)
                    run_alive = heartbeat_response_run(
                        session, run_id=self.run["run_id"], token=self.run["claim_token"],
                        epoch=self.run["execution_epoch"],
                        lease_seconds=self.settings.lease_seconds)
                    session.commit()
                if not job_alive or not run_alive:
                    self.lost.set()
                    return
            except SQLAlchemyError:
                continue


def _job_kind(session: Session, job_id: UUID) -> str | None:
    return session.execute(select(rfp_jobs.c.job_kind).where(
        rfp_jobs.c.job_id == job_id)).scalar_one_or_none()


def _run_for_ticket(session: Session, ticket_id: UUID) -> dict[str, Any] | None:
    row = session.execute(select(rfp_response_runs).where(
        rfp_response_runs.c.ticket_id == ticket_id
    ).order_by(rfp_response_runs.c.created_at.desc()).limit(1)).mappings().one_or_none()
    return None if row is None else dict(row)


def _finish(engine: Any, job: dict[str, Any], *, status: str,
            code: str | None = None, summary: str | None = None) -> None:
    with Session(engine) as session:
        finish_job(session, job=job, status=status,
                   error_code=code, error_summary=summary)
        session.commit()


def execute_claimed_response_job(engine: Any, job: dict[str, Any],
                                 settings: RFPSettings) -> None:
    ticket_id = UUID(str(job["ticket_id"]))
    with Session(engine) as session:
        current = _run_for_ticket(session, ticket_id)
        if current is None:
            raise ResponseGraphError("response job has no durable response run")
        claimed_run = claim_response_run(
            session, UUID(str(current["run_id"])), lease_seconds=settings.lease_seconds)
        session.commit()
    if claimed_run is None:
        with Session(engine) as session:
            current = _run_for_ticket(session, ticket_id)
            has_handoff = session.execute(select(rfp_response_handoffs.c.run_id).where(
                rfp_response_handoffs.c.run_id == current["run_id"])).scalar_one_or_none()
        if current["status"] in {"ready_for_part3", "needs_human_review"} and has_handoff:
            _finish(engine, job, status="succeeded")
            return
        if current["status"] == "technical_failure":
            _finish(engine, job, status="failed", code="response_technical_failure",
                    summary="RFP response generation ended with a technical failure")
            return
        raise SQLAlchemyError("response run lease is currently unavailable")

    with _ResponseHeartbeat(engine, job, claimed_run, settings) as heartbeat:
        result = process_response_run(engine, claimed_run=claimed_run)
    if heartbeat.lost.is_set():
        raise SQLAlchemyError("response generation lease was lost")
    if result.technical_failure:
        _finish(engine, job, status="failed", code="response_technical_failure",
                summary="RFP response generation ended with a technical failure")
    else:
        _finish(engine, job, status="succeeded")


@celery_app.task(bind=True, name="trackflow.process_rfp_response", max_retries=None)
def process_rfp_response(self: Any, job_id: str) -> None:
    """Run one durable response job; Celery attempts never change business iterations."""
    try:
        parsed_job_id = UUID(job_id)
    except (TypeError, ValueError):
        return
    settings = get_rfp_settings()
    try:
        engine = _engine()
        with Session(engine) as session:
            if _job_kind(session, parsed_job_id) != "response_generation":
                return
            job = claim_job(session, parsed_job_id, lease_seconds=settings.lease_seconds)
            session.commit()
    except SQLAlchemyError as exc:
        raise self.retry(exc=exc, countdown=settings.retry_backoff_seconds[0])
    if job is None:
        return
    try:
        execute_claimed_response_job(engine, job, settings)
    except SQLAlchemyError:
        with Session(engine) as session:
            schedule_retry(session, job=job, error_code="response_infrastructure_retry",
                           error_summary="RFP response infrastructure operation failed")
            session.commit()
    except ResponseGraphError:
        _finish(engine, job, status="failed", code="response_invariant_failed",
                summary="RFP response generation violated a durable invariant")
    except Retry:
        raise
    except Exception:
        _finish(engine, job, status="failed", code="response_processing_failed",
                summary="RFP response generation failed")
