"""Celery wrappers around existing TrackFlow business operations.

Block A intentionally contains no durable task registry, claims, outbox, DLQ,
or business retry policy. Those guarantees belong to DEV-55 Block B.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pipelines.pipeline import (
    PipelineRuntime,
    build_cli_runtime,
    parse_closed_week,
    run_weekly_pipeline,
)
from services.task_queue.celery_app import celery_app

RUN_WEEKLY_PIPELINE_TASK = "trackflow.run_weekly_pipeline"

logger = logging.getLogger(__name__)

_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_REDACTED_DIAGNOSTIC = "redacted_diagnostic"


class TaskExecutionError(RuntimeError):
    """Safe public failure whose message cannot contain connection details."""


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


@celery_app.task(name=RUN_WEEKLY_PIPELINE_TASK, bind=True)
def run_weekly_pipeline_task(
    self: Any,
    *,
    run_id: str,
    week_start: str,
) -> dict[str, Any]:
    """Execute the existing weekly pipeline from lightweight references only."""
    task_id = str(self.request.id or "unassigned")
    started = time.perf_counter()
    safe_run_id = "invalid"
    try:
        parsed_run_id = UUID(run_id)
        safe_run_id = str(parsed_run_id)
        parsed_week = parse_closed_week(week_start, now=datetime.now(timezone.utc))
        logger.info(
            "celery_task_started task_id=%s run_id=%s week_start=%s",
            task_id,
            parsed_run_id,
            parsed_week,
        )
        runtime = build_worker_runtime(parsed_run_id)
        result = run_weekly_pipeline(runtime, parsed_week)
        payload = _result_payload(task_id, parsed_run_id, result)
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "celery_task_finished task_id=%s run_id=%s status=%s duration_ms=%.1f",
            task_id,
            parsed_run_id,
            result.status,
            duration_ms,
        )
        return payload
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
