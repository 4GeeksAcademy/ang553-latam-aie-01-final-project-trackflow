"""Durably claimed Celery wrapper around the existing weekly pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from celery.exceptions import Retry

from pipelines.database_identity import sqlalchemy_postgresql_url

from pipelines.pipeline import (
    PipelineRuntime,
    build_cli_runtime,
    parse_closed_week,
    run_weekly_pipeline,
)
from services.task_queue.celery_app import celery_app
from services.task_queue.reconciliation import reconcile_task
from services.task_queue.sanitization import public_exception
from services.task_queue.store import (
    CONTRACT_VERSION,
    MAX_FAILED_EXECUTIONS,
    claim_execution,
    fail_claim,
    finish_execution,
    get_async_task,
    heartbeat_execution,
    insert_dead_letter,
    schedule_retry,
    start_prefect,
)

RUN_WEEKLY_PIPELINE_TASK = "trackflow.run_weekly_pipeline"

logger = logging.getLogger(__name__)

_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_REDACTED_DIAGNOSTIC = "redacted_diagnostic"


class TaskExecutionError(RuntimeError):
    """Safe public failure whose message cannot contain connection details."""


_task_engine: Any | None = None


def get_task_engine() -> Any:
    global _task_engine
    if _task_engine is None:
        url = os.environ.get("TRACKFLOW_REPORTING_DATABASE_URL", "")
        if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
            raise TaskExecutionError("task registry PostgreSQL is unavailable")
        _task_engine = create_engine(sqlalchemy_postgresql_url(url), pool_pre_ping=True)
    return _task_engine


def _try_dead_letter(connection: Any, **values: Any) -> bool:
    """Isolate DLQ persistence so its failure can be represented durably."""
    try:
        with connection.begin_nested():
            return insert_dead_letter(connection, **values)
    except Exception:
        return False


@contextmanager
def _execution_heartbeat(
    engine: Any,
    *,
    task_id: UUID,
    claim_token: UUID,
    epoch: int,
    lease_seconds: int,
    interval_seconds: int,
):
    """Renew a fenced running claim; failure never authorizes reexecution."""
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(interval_seconds):
            try:
                with engine.begin() as connection:
                    current = heartbeat_execution(
                        connection,
                        task_id=task_id,
                        claim_token=claim_token,
                        epoch=epoch,
                        lease_seconds=lease_seconds,
                    )
                if not current:
                    logger.warning(
                        "task_heartbeat_fenced task_id=%s epoch=%s",
                        task_id,
                        epoch,
                    )
                    return
            except Exception as exc:
                code, _message = public_exception(exc)
                logger.warning(
                    "task_heartbeat_failed task_id=%s epoch=%s error_code=%s",
                    task_id,
                    epoch,
                    code,
                )

    thread = threading.Thread(target=renew, name=f"task-heartbeat-{task_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(1, interval_seconds))


def build_worker_runtime(run_id: UUID) -> PipelineRuntime:
    """Reconstruct pipeline dependencies inside the worker process."""
    runtime = build_cli_runtime()
    return replace(runtime, accepted_run_id=run_id, trigger_type="celery")


def _safe_result_errors(errors: Any) -> list[str]:
    """Expose stable diagnostic codes, never arbitrary exception text."""
    return [
        value
        if isinstance(value, str) and _SAFE_DIAGNOSTIC_CODE.fullmatch(value)
        else _REDACTED_DIAGNOSTIC
        for value in errors
    ]


def _result_payload(task_id: str, requested_run_id: UUID, result: Any) -> dict[str, Any]:
    result_run_id = getattr(result, "run_id", None)
    if result_run_id != requested_run_id:
        raise RuntimeError("pipeline result run_id does not match the requested run_id")
    return {
        "task_id": task_id,
        "run_id": str(requested_run_id),
        "week_start": result.week_start.isoformat(),
        "status": result.status,
        "counts": {
            "inserted": result.inserted,
            "updated": result.updated,
            "unchanged": result.unchanged,
        },
        "commit_result": result.commit_result,
        "publication_recorded": result.publication_recorded,
        "recovered": result.recovered,
        "evidence_inconclusive": result.evidence_inconclusive,
        "coverage_source": result.coverage_source,
        "errors": _safe_result_errors(result.errors),
        "production_certified": result.production_certified,
    }


def _legacy_eager_execution(task_id: str, run_id: str, week_start: str) -> dict[str, Any]:
    """Retain isolated Block-A unit-test execution; never used by a worker."""
    parsed_run_id = UUID(run_id)
    parsed_week = parse_closed_week(week_start, now=datetime.now(timezone.utc))
    return _result_payload(task_id, parsed_run_id,
                           run_weekly_pipeline(build_worker_runtime(parsed_run_id), parsed_week))


@celery_app.task(name=RUN_WEEKLY_PIPELINE_TASK, bind=True, max_retries=3)
def run_weekly_pipeline_task(
    self: Any,
    *,
    run_id: str,
    week_start: str,
    contract_version: int = CONTRACT_VERSION,
) -> dict[str, Any]:
    """Execute at most one claimed pipeline; three failed executions are terminal."""
    task_id = str(self.request.id or "unassigned")
    started = time.perf_counter()
    safe_run_id = "invalid"
    try:
        if self.request.is_eager and os.environ.get("TRACKFLOW_DURABLE_EAGER_TEST") != "1":
            return _legacy_eager_execution(task_id, run_id, week_start)
        if contract_version != CONTRACT_VERSION:
            raise TaskExecutionError("unsupported task contract")
        parsed_task_id = UUID(task_id)
        parsed_run_id = UUID(run_id)
        safe_run_id = str(parsed_run_id)
        parsed_week = parse_closed_week(week_start, now=datetime.now(timezone.utc))
        logger.info(
            "celery_task_started task_id=%s run_id=%s week_start=%s",
            task_id,
            parsed_run_id,
            parsed_week,
        )
        engine = get_task_engine()
        lease_seconds = int(os.environ["TRACKFLOW_TASK_CLAIM_LEASE_SECONDS"])
        heartbeat_seconds = int(os.environ["TRACKFLOW_TASK_HEARTBEAT_SECONDS"])
        retry_max_backoff = int(os.environ["TRACKFLOW_TASK_RETRY_MAX_BACKOFF_SECONDS"])
        if (
            lease_seconds <= 0
            or heartbeat_seconds <= 0
            or heartbeat_seconds >= lease_seconds
            or retry_max_backoff <= 0
        ):
            raise TaskExecutionError("task timing configuration is invalid")
        with engine.begin() as connection:
            claim = claim_execution(connection, parsed_task_id, lease_seconds=lease_seconds)
        if claim is None:
            with engine.begin() as connection:
                reconcile_task(connection, parsed_task_id)
                existing = get_async_task(connection, parsed_task_id)
            if existing and existing["execution_state"] == "succeeded":
                value = existing.get("result_summary") or {}
                return dict(value) if not isinstance(value, str) else json.loads(value)
            raise TaskExecutionError("task is already owned or terminal")
        if UUID(str(claim["run_id"])) != parsed_run_id or claim["week_start"] != parsed_week:
            raise TaskExecutionError("task payload identity mismatch")
        token, epoch, attempt = UUID(str(claim["claim_token"])), int(claim["execution_epoch"]), int(claim["attempt_count"])
        try:
            runtime = build_worker_runtime(parsed_run_id)
            with runtime.engine.connect() as probe:
                probe.execute(text("SELECT 1"))
        except OperationalError as exc:
            code, message = public_exception(exc)
            if attempt < MAX_FAILED_EXECUTIONS:
                delay = min(
                    retry_max_backoff,
                    2 ** (attempt - 1),
                )
                retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                with engine.begin() as connection:
                    schedule_retry(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                                   next_attempt_at=retry_at, error_code=code, error_message=message)
                raise self.retry(exc=TaskExecutionError(message), countdown=delay, max_retries=3)
            with engine.begin() as connection:
                inserted = _try_dead_letter(connection, task_id=parsed_task_id, run_id=parsed_run_id,
                    attempt_number=attempt, reason="retries_exhausted", error_code=code, error_message=message)
                fail_claim(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                           state="dead_lettered" if inserted else "failure_pending_dlq",
                           error_code=code, error_message=message)
            raise TaskExecutionError(message)
        except Exception as exc:
            code, message = public_exception(exc)
            with engine.begin() as connection:
                inserted = _try_dead_letter(connection, task_id=parsed_task_id, run_id=parsed_run_id,
                    attempt_number=attempt, reason="non_retryable", error_code=code, error_message=message)
                fail_claim(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                    state="dead_lettered" if inserted else "failure_pending_dlq", error_code=code,error_message=message)
            raise TaskExecutionError(message)
        with engine.begin() as connection:
            if not start_prefect(
                connection,
                task_id=parsed_task_id,
                claim_token=token,
                epoch=epoch,
                lease_seconds=lease_seconds,
            ):
                raise TaskExecutionError("execution claim was fenced before Prefect")
        try:
            with _execution_heartbeat(
                engine,
                task_id=parsed_task_id,
                claim_token=token,
                epoch=epoch,
                lease_seconds=lease_seconds,
                interval_seconds=heartbeat_seconds,
            ):
                result = run_weekly_pipeline(runtime, parsed_week)
        except Exception as exc:
            code, message = public_exception(exc)
            with engine.begin() as connection:
                finish_execution(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                    state="reconciliation_required", pipeline_status="execution_uncertain",
                    published=None, result="{}", error_code=code, error_message=message)
            raise TaskExecutionError(message) from None
        payload = _result_payload(task_id, parsed_run_id, result)
        encoded = json.dumps(payload, separators=(",", ":"))
        if result.status == "commit_uncertain" or result.commit_result == "uncertain":
            state, published = "reconciliation_required", None
        elif result.status == "blocked_quality":
            state, published = "succeeded", False
        elif result.status in {"succeeded", "succeeded_noop"}:
            state, published = "succeeded", bool(result.publication_recorded)
        elif result.status in {"superseded", "skipped_in_progress"}:
            state, published = "failed", False
        else:
            state, published = "failed", False
        if state == "failed":
            with engine.begin() as connection:
                inserted = _try_dead_letter(connection, task_id=parsed_task_id, run_id=parsed_run_id,
                    attempt_number=attempt, reason="pipeline_terminal", error_code=result.status,
                    error_message="pipeline returned a terminal failure")
                finish_execution(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                    state="dead_lettered" if inserted else "failure_pending_dlq", pipeline_status=result.status,
                    published=published, result=encoded, error_code=result.status,
                    error_message="pipeline returned a terminal failure")
            raise TaskExecutionError("pipeline returned a terminal failure")
        with engine.begin() as connection:
            if not finish_execution(connection, task_id=parsed_task_id, claim_token=token, epoch=epoch,
                state=state, pipeline_status=result.status, published=published, result=encoded):
                raise TaskExecutionError("terminal update was fenced")
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "celery_task_finished task_id=%s run_id=%s status=%s duration_ms=%.1f",
            task_id,
            parsed_run_id,
            result.status,
            duration_ms,
        )
        return payload
    except Retry:
        raise
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.error(
            "celery_task_failed task_id=%s run_id=%s error_type=%s duration_ms=%.1f",
            task_id,
            safe_run_id,
            type(exc).__name__,
            duration_ms,
        )
        raise TaskExecutionError(f"task execution failed ({type(exc).__name__})") from None
