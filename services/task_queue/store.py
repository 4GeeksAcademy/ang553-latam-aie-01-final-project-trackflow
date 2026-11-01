"""PostgreSQL contracts for durable task acceptance, claims, retries and DLQ.

Every mutating function expects a caller-owned transaction.  The SQL uses
database time and compare-and-set predicates so a stale worker/relay cannot
close work owned by a newer epoch.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from pipelines.run_store import create_run
from services.task_queue.sanitization import safe_code, sanitize_message

TASK_NAME = "trackflow.run_weekly_pipeline"
CONTRACT_VERSION = 1
MAX_FAILED_EXECUTIONS = 3

_ACTIVE_RUN = text("""
SELECT run_id FROM reporting.pipeline_publication_runs
WHERE target_week_start=:week_start AND status IN ('pending','running')
ORDER BY started_at DESC LIMIT 1
""")
_WEEK_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")
_INSERT_TASK = text("""
INSERT INTO reporting.async_tasks
 (task_id,run_id,requested_by_user_id,task_name,week_start,execution_state,
  dispatch_status,attempt_count,execution_epoch,execution_phase,created_at,updated_at)
VALUES (:task_id,:run_id,:user_id,:task_name,:week_start,'accepted','pending',0,0,'none',
        clock_timestamp(),clock_timestamp())
""")
_INSERT_OUTBOX = text("""
INSERT INTO reporting.task_dispatch_outbox
 (task_id,payload,dispatch_status,publish_attempts,next_attempt_at,created_at,updated_at)
VALUES (:task_id,CAST(:payload AS jsonb),'pending',0,clock_timestamp(),clock_timestamp(),clock_timestamp())
""")
_GET_TASK = text("""
SELECT t.*, r.status AS durable_pipeline_status, r.commit_result,
       r.rows_inserted,r.rows_updated,r.rows_unchanged,r.error_code AS run_error_code,
       r.error_message AS run_error_message,r.block_reason
FROM reporting.async_tasks t
JOIN reporting.pipeline_publication_runs r ON r.run_id=t.run_id
WHERE t.task_id=:task_id
""")


def accept_async_task(
    connection: Any,
    week_start: date,
    requested_by_user_id: UUID | str,
) -> tuple[UUID, UUID, bool]:
    """Atomically create the pipeline run, durable task and dispatch outbox."""
    connection.execute(_WEEK_LOCK, {"key": f"trackflow-reporting:{week_start.isoformat()}"})
    existing = connection.execute(_ACTIVE_RUN, {"week_start": week_start}).scalar_one_or_none()
    if existing is not None:
        row = connection.execute(
            text("SELECT task_id FROM reporting.async_tasks WHERE run_id=:run_id"),
            {"run_id": existing},
        ).first()
        return UUID(str(existing)), UUID(str(row[0])) if row else uuid4(), False
    run = create_run(connection, target_week_start=week_start, trigger_type="http", stage_reached="accepted")
    task_id = uuid4()
    connection.execute(_INSERT_TASK, {"task_id": task_id, "run_id": run.run_id,
        "user_id": str(requested_by_user_id), "task_name": TASK_NAME, "week_start": week_start})
    import json
    payload = json.dumps({"contract_version": CONTRACT_VERSION, "task_id": str(task_id),
                          "run_id": str(run.run_id), "week_start": week_start.isoformat()}, separators=(",", ":"))
    connection.execute(_INSERT_OUTBOX, {"task_id": task_id, "payload": payload})
    return run.run_id, task_id, True


def get_async_task(connection: Any, task_id: UUID) -> Mapping[str, Any] | None:
    row = connection.execute(_GET_TASK, {"task_id": task_id}).mappings().first()
    return None if row is None else dict(row)


_CLAIM = text("""
UPDATE reporting.async_tasks SET
 execution_state='claimed', attempt_count=attempt_count+1, attempt_number=attempt_count+1,
 attempt_id=:attempt_id,
 celery_state='STARTED',
 claim_token=:claim_token, execution_epoch=execution_epoch+1, execution_phase='preflight',
 claim_expires_at=clock_timestamp()+(:lease_seconds*INTERVAL '1 second'),
 heartbeat_at=clock_timestamp(), updated_at=clock_timestamp()
WHERE task_id=:task_id AND attempt_count<:max_attempts AND (
 execution_state IN ('accepted','queued') OR
 (execution_state='retry_wait' AND next_attempt_at<=clock_timestamp()) OR
 (execution_state='claimed' AND execution_phase='preflight' AND claim_expires_at<=clock_timestamp())
)
RETURNING *
""")


def claim_execution(connection: Any, task_id: UUID, *, lease_seconds: int) -> Mapping[str, Any] | None:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    token, attempt = uuid4(), uuid4()
    row = connection.execute(_CLAIM, {"task_id": task_id, "claim_token": token,
        "attempt_id": attempt, "lease_seconds": lease_seconds,
        "max_attempts": MAX_FAILED_EXECUTIONS}).mappings().first()
    return None if row is None else dict(row)


_START_PREFECT = text("""
UPDATE reporting.async_tasks SET execution_state='running',execution_phase='prefect_started',
 heartbeat_at=clock_timestamp(),
 claim_expires_at=clock_timestamp()+(:lease_seconds*INTERVAL '1 second'),
 updated_at=clock_timestamp()
WHERE task_id=:task_id AND execution_state='claimed' AND execution_phase='preflight'
 AND claim_token=:claim_token AND execution_epoch=:epoch
 AND claim_expires_at>clock_timestamp()
RETURNING task_id
""")


def start_prefect(
    connection: Any,
    *,
    task_id: UUID,
    claim_token: UUID,
    epoch: int,
    lease_seconds: int,
) -> bool:
    return connection.execute(_START_PREFECT, {"task_id": task_id, "claim_token": claim_token,
                                               "epoch": epoch,
                                               "lease_seconds": lease_seconds}).first() is not None


_HEARTBEAT = text("""
UPDATE reporting.async_tasks SET
 heartbeat_at=clock_timestamp(),
 claim_expires_at=clock_timestamp()+(:lease_seconds*INTERVAL '1 second'),
 updated_at=clock_timestamp()
WHERE task_id=:task_id AND execution_state='running'
 AND execution_phase='prefect_started' AND claim_token=:claim_token
 AND execution_epoch=:epoch
RETURNING task_id
""")


def heartbeat_execution(
    connection: Any,
    *,
    task_id: UUID,
    claim_token: UUID,
    epoch: int,
    lease_seconds: int,
) -> bool:
    return connection.execute(
        _HEARTBEAT,
        {
            "task_id": task_id,
            "claim_token": claim_token,
            "epoch": epoch,
            "lease_seconds": lease_seconds,
        },
    ).first() is not None


_FINISH = text("""
UPDATE reporting.async_tasks SET execution_state=:state,execution_phase='terminal',
 celery_state=:celery_state,
 pipeline_status=:pipeline_status,published=:published,result_summary=CAST(:result AS jsonb),
 error_code=:error_code,error_message=:error_message,finished_at=clock_timestamp(),
 heartbeat_at=clock_timestamp(),claim_expires_at=NULL,updated_at=clock_timestamp()
WHERE task_id=:task_id AND execution_state='running' AND execution_phase='prefect_started'
 AND claim_token=:claim_token AND execution_epoch=:epoch RETURNING task_id
""")


def finish_execution(connection: Any, *, task_id: UUID, claim_token: UUID, epoch: int,
                     state: str, pipeline_status: str, published: bool | None,
                     result: str, error_code: str | None = None,
                     error_message: str | None = None) -> bool:
    return connection.execute(_FINISH, {"task_id": task_id, "claim_token": claim_token,
        "epoch": epoch, "state": state,
        "celery_state": (
            "SUCCESS"
            if state == "succeeded"
            or (state == "reconciliation_required" and pipeline_status == "commit_uncertain")
            else "FAILURE"
        ),
        "pipeline_status": pipeline_status,
        "published": published, "result": result, "error_code": None if error_code is None else safe_code(error_code),
        "error_message": None if error_message is None else sanitize_message(error_message)}).first() is not None


_RETRY = text("""
UPDATE reporting.async_tasks SET execution_state='retry_wait',execution_phase='none',
 celery_state='RETRY',
 error_code=:error_code,error_message=:error_message,next_attempt_at=:next_attempt_at,
 claim_token=NULL,claim_expires_at=NULL,updated_at=clock_timestamp()
WHERE task_id=:task_id AND execution_state='claimed' AND execution_phase='preflight'
 AND claim_token=:claim_token AND execution_epoch=:epoch RETURNING task_id
""")


def schedule_retry(connection: Any, *, task_id: UUID, claim_token: UUID, epoch: int,
                   next_attempt_at: Any, error_code: str, error_message: str) -> bool:
    return connection.execute(_RETRY, {"task_id": task_id, "claim_token": claim_token,
        "epoch": epoch, "next_attempt_at": next_attempt_at, "error_code": safe_code(error_code),
        "error_message": sanitize_message(error_message)}).first() is not None


def insert_dead_letter(connection: Any, *, task_id: UUID, run_id: UUID, attempt_number: int,
                       reason: str, error_code: str, error_message: str) -> bool:
    row = connection.execute(text("""
    INSERT INTO reporting.task_dead_letters
      (task_id,run_id,attempt_number,terminal_reason,error_code,error_message,failed_at)
    VALUES (:task_id,:run_id,:attempt,:reason,:code,:message,clock_timestamp())
    ON CONFLICT (task_id) DO UPDATE SET task_id=EXCLUDED.task_id
      WHERE reporting.task_dead_letters.run_id=EXCLUDED.run_id
    RETURNING task_id
    """), {"task_id": task_id, "run_id": run_id, "attempt": attempt_number,
           "reason": safe_code(reason), "code": safe_code(error_code),
           "message": sanitize_message(error_message)}).first()
    return row is not None


def fail_claim(connection: Any, *, task_id: UUID, claim_token: UUID, epoch: int,
               state: str, error_code: str, error_message: str) -> bool:
    """Close a preflight claim using its fencing identity."""
    row = connection.execute(text("""
      UPDATE reporting.async_tasks SET execution_state=:state,execution_phase='terminal',
        celery_state='FAILURE',
        published=FALSE,error_code=:code,error_message=:message,claim_expires_at=NULL,
        finished_at=clock_timestamp(),updated_at=clock_timestamp()
      WHERE task_id=:task_id AND execution_state='claimed' AND execution_phase='preflight'
        AND claim_token=:token AND execution_epoch=:epoch RETURNING task_id
    """), {"task_id": task_id, "token": claim_token, "epoch": epoch, "state": state,
           "code": safe_code(error_code), "message": sanitize_message(error_message)}).first()
    return row is not None
