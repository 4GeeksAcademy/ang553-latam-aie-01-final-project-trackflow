"""Durable, fenced Celery execution for the RFP intake graph."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import UUID

from celery.exceptions import Retry
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from data.pipelines.rfp_intake.analyzer import (
    FourGeeksRFPAnalyzer,
    ProviderResponseDiagnostic,
    StructuredGenerationContext,
    StructuredOutputError,
    structured_generation_context,
)
from data.pipelines.rfp_intake.pdf_conversion import PDFConversionError, pdf_to_markdown
from data.pipelines.rfp_intake.readability import compute_readability
from data.pipelines.rfp_intake.runtime import process_rfp_ticket
from pipelines.database_identity import sqlalchemy_postgresql_url
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import load_settings
from services.rag.contracts import RAGConfigurationError, RAGProviderError
from services.rfp_intake.artifacts import persist_markdown
from services.rfp_intake.config import RFPSettings, get_rfp_settings
from services.rfp_intake.fourgeeks_budget import (
    BudgetedIntakeFourGeeksGateway,
    IntakeFourGeeksBudgetError,
    IntakeFourGeeksRequestLedger,
    load_intake_fourgeeks_budget_settings,
)
from services.rfp_intake.models import rfp_jobs, rfp_tickets
from services.rfp_intake.sink import PostgresRFPResultSink
from services.rfp_intake.store import claim_job, finish_job, heartbeat_job, schedule_retry
from services.task_queue.celery_app import celery_app


diagnostic_logger = logging.getLogger("trackflow.rfp_intake.structured_output")


def _validation_stage(category: str) -> str:
    return {
        "json_invalid": "json",
        "pydantic_validation": "pydantic",
        "semantic_validation": "semantic",
        "reference_bounds": "graph_reference",
        "validation_error": "provider_envelope",
        "timeout": "provider_http",
    }.get(category, "provider_http")


def _log_content_free_diagnostic(
    job: dict[str, Any],
    *,
    context: StructuredGenerationContext | None,
    category: str,
    error_type: str,
    path: str,
    received_type: str,
    violated_constraint: str,
) -> None:
    provider = context.provider if context is not None else ProviderResponseDiagnostic()
    diagnostic_logger.warning(
        "rfp_intake_structured_failure "
        "job_id=%s attempt=%s operation=%s invocation_id=%s "
        "validation_stage=%s category=%s error_type=%s path=%s "
        "received_type=%s violated_constraint=%s http_status=%s "
        "content_type=%s finish_reason=%s content_value_type=%s content_length=%s "
        "refusal_present=%s tool_calls_present=%s reasoning_present=%s "
        "latency_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s "
        "provider_request_id=%s",
        job["job_id"],
        job["attempt_count"],
        context.operation if context is not None else "unknown",
        context.invocation_id if context is not None else "unknown",
        _validation_stage(category),
        category,
        error_type,
        path,
        received_type,
        violated_constraint,
        provider.http_status,
        provider.content_type,
        provider.finish_reason,
        provider.content_value_type,
        provider.content_length,
        provider.refusal_present,
        provider.tool_calls_present,
        provider.reasoning_present,
        provider.latency_ms,
        provider.prompt_tokens,
        provider.completion_tokens,
        provider.total_tokens,
        provider.request_id,
    )


def _log_structured_output_failure(
    job: dict[str, Any], error: StructuredOutputError
) -> None:
    diagnostic = error.diagnostic
    _log_content_free_diagnostic(
        job,
        context=StructuredGenerationContext(
            operation=diagnostic.operation,
            invocation_id=diagnostic.invocation_id,
            provider=diagnostic.provider,
        ),
        category=diagnostic.category,
        error_type=diagnostic.error_type,
        path=diagnostic.path,
        received_type=diagnostic.received_type,
        violated_constraint=diagnostic.violated_constraint,
    )


def _log_provider_failure(job: dict[str, Any], error: RAGProviderError) -> None:
    category = error.category if error.category in {
        "timeout", "network_error", "http_error", "validation_error",
        "deadline_exhausted", "retry_exhausted",
    } else "provider_error"
    _log_content_free_diagnostic(
        job,
        context=structured_generation_context(error),
        category=category,
        error_type="provider_error",
        path="root",
        received_type="unknown",
        violated_constraint="provider_request",
    )


def _engine() -> Any:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise RuntimeError("RFP processing requires PostgreSQL DATABASE_URL")
    return create_engine(sqlalchemy_postgresql_url(database_url), pool_pre_ping=True)


def _source_path(root: Path, ticket_id: UUID) -> Path:
    root_resolved = root.resolve()
    source = (root / str(ticket_id) / "source.pdf").resolve()
    if root_resolved not in source.parents:
        raise PDFConversionError("RFP source path is invalid")
    return source


class _LeaseHeartbeat:
    """Renew a claim from an independent session while provider calls run."""

    def __init__(self, engine: Any, job: dict[str, Any], settings: RFPSettings) -> None:
        self._engine, self._job, self._settings = engine, job, settings
        self._stop = Event()
        self.lost = Event()
        self._thread = Thread(target=self._run, name="rfp-lease-heartbeat", daemon=True)

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=min(5, self._settings.heartbeat_seconds))

    def _run(self) -> None:
        while not self._stop.wait(self._settings.heartbeat_seconds):
            try:
                with Session(self._engine) as session:
                    alive = heartbeat_job(
                        session, job_id=self._job["job_id"], token=self._job["claim_token"],
                        epoch=self._job["execution_epoch"], lease_seconds=self._settings.lease_seconds,
                    )
                    session.commit()
                if not alive:
                    self.lost.set()
                    return
            except SQLAlchemyError:
                # A transient heartbeat outage does not override the DB-enforced lease/fence.
                continue


def _terminal_state(engine: Any, job_id: UUID) -> str | None:
    try:
        with Session(engine) as session:
            return session.execute(
                select(rfp_jobs.c.status).where(rfp_jobs.c.job_id == job_id)
            ).scalar_one_or_none()
    except SQLAlchemyError:
        return None


def _record_failure(engine: Any, job: dict[str, Any], *, retryable: bool,
                    code: str, summary: str) -> None:
    with Session(engine) as session:
        if retryable:
            schedule_retry(session, job=job, error_code=code, error_summary=summary)
        else:
            finish_job(session, job=job, status="failed", error_code=code, error_summary=summary)
        session.commit()


def _record_failure_or_redeliver(task: Any, engine: Any, job: dict[str, Any],
                                 settings: RFPSettings, *, retryable: bool,
                                 code: str, summary: str) -> None:
    try:
        _record_failure(engine, job, retryable=retryable, code=code, summary=summary)
    except SQLAlchemyError as exc:
        # Reconcile an acknowledgement-lost COMMIT before requesting redelivery.
        state = _terminal_state(engine, UUID(str(job["job_id"])))
        if state in {"retry_wait", "succeeded", "failed", "dead_letter"}:
            return
        raise task.retry(exc=exc, countdown=settings.lease_seconds)


def execute_claimed_job(engine: Any, job: dict[str, Any], settings: RFPSettings) -> None:
    """Execute one already-claimed job; terminal business state and job commit together."""
    ticket_id = UUID(str(job["ticket_id"]))
    markdown_reference = f"rfp_intake/{ticket_id}/source.md"
    conversion = pdf_to_markdown(
        _source_path(settings.artifact_root, ticket_id),
        markdown_reference=markdown_reference,
        max_pages=settings.max_pdf_pages,
        timeout_seconds=settings.pdf_conversion_timeout_seconds,
    )
    markdown_path = persist_markdown(root=settings.artifact_root, ticket_id=ticket_id,
                                     markdown=conversion.markdown)
    if markdown_path != markdown_reference:
        raise RuntimeError("Persisted Markdown reference does not match conversion metrics")
    with Session(engine) as session:
        changed = session.execute(update(rfp_tickets).where(
            rfp_tickets.c.ticket_id == ticket_id,
            rfp_tickets.c.status == "analizando",
        ).values(markdown_path=markdown_path)).rowcount
        if changed != 1:
            session.rollback()
            raise RuntimeError("RFP ticket is no longer processable")
        session.commit()

    gateway = FourGeeksHTTPClient(load_settings())
    try:
        budget_settings = load_intake_fourgeeks_budget_settings(
            artifact_root=settings.artifact_root
        )
        analyzer_gateway: Any = gateway
        if budget_settings is not None:
            analyzer_gateway = BudgetedIntakeFourGeeksGateway(
                gateway,
                IntakeFourGeeksRequestLedger(
                    budget_settings,
                    job_id=UUID(str(job["job_id"])),
                    ticket_id=ticket_id,
                    actual_pdf_sha256=conversion.metrics.sha256,
                ),
            )
        analyzer = FourGeeksRFPAnalyzer(
            analyzer_gateway,
            timeout_seconds=gateway.settings.generation_timeout_seconds,
        )
        readability = compute_readability(conversion.markdown)
        with Session(engine) as session:
            sink = PostgresRFPResultSink(session, job)
            process_rfp_ticket(
                ticket_id=ticket_id,
                markdown=conversion.markdown,
                conversion_metrics=conversion.metrics,
                readability_metrics=readability,
                analyzer=analyzer,
                sink=sink,
            )
    finally:
        gateway.close()


@celery_app.task(bind=True, name="trackflow.process_rfp", max_retries=None)
def process_rfp(self: Any, job_id: str) -> None:
    """Claim, fence and process a durable job identified only by its UUID."""
    try:
        parsed_job_id = UUID(job_id)
    except (TypeError, ValueError):
        return
    settings = get_rfp_settings()
    try:
        engine = _engine()
        with Session(engine) as session:
            job = claim_job(session, parsed_job_id, lease_seconds=settings.lease_seconds)
            session.commit()
    except SQLAlchemyError as exc:
        raise self.retry(exc=exc, countdown=settings.retry_backoff_seconds[0])
    if job is None:
        return

    try:
        with _LeaseHeartbeat(engine, job, settings):
            execute_claimed_job(engine, job, settings)
    except PDFConversionError:
        _record_failure_or_redeliver(self, engine, job, settings, retryable=False,
                                     code="pdf_conversion_failed", summary="PDF conversion failed")
    except RAGConfigurationError:
        _record_failure_or_redeliver(
            self, engine, job, settings, retryable=False,
            code="provider_configuration_invalid", summary="RFP provider configuration is invalid",
        )
    except IntakeFourGeeksBudgetError:
        _record_failure_or_redeliver(
            self, engine, job, settings, retryable=False,
            code="fourgeeks_request_budget_blocked",
            summary="The isolated QA FourGeeks request budget blocked intake",
        )
    except StructuredOutputError as exc:
        _log_structured_output_failure(job, exc)
        _record_failure_or_redeliver(
            self, engine, job, settings, retryable=True, code="structured_output_invalid",
            summary="Structured provider output did not satisfy the contract",
        )
    except RAGProviderError as exc:
        _log_provider_failure(job, exc)
        _record_failure_or_redeliver(self, engine, job, settings, retryable=True,
                                     code=exc.category, summary="RFP provider request failed")
    except SQLAlchemyError as exc:
        # The previous COMMIT may have succeeded even when its acknowledgement failed.
        if _terminal_state(engine, parsed_job_id) == "succeeded":
            return
        raise self.retry(exc=exc, countdown=settings.lease_seconds)
    except Retry:
        raise
    except Exception:
        _record_failure_or_redeliver(self, engine, job, settings, retryable=False,
                                     code="rfp_processing_failed", summary="RFP processing failed")
