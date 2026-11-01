"""Conservative repair from authoritative pipeline history; never reexecutes."""
from __future__ import annotations
import json
from typing import Any
from uuid import UUID
from sqlalchemy import text
from services.task_queue.store import insert_dead_letter


def reconcile_one(engine: Any) -> bool:
    """Repair one safely decidable task; ambiguous outcomes remain visible."""
    with engine.begin() as connection:
        row = connection.execute(text("""
          SELECT t.task_id FROM reporting.async_tasks t
          JOIN reporting.pipeline_publication_runs r ON r.run_id=t.run_id
          WHERE t.execution_state='failure_pending_dlq'
             OR (t.execution_state='running'
                 AND t.execution_phase='prefect_started'
                 AND t.claim_expires_at IS NOT NULL
                 AND t.claim_expires_at<=clock_timestamp())
             OR (t.execution_state IN ('claimed','running')
                 AND t.claim_expires_at IS NOT NULL
                 AND t.claim_expires_at<=clock_timestamp()
                 AND r.status IN ('succeeded','succeeded_noop','blocked_quality',
                                  'failed_retryable','superseded','skipped_in_progress'))
          ORDER BY t.updated_at,t.task_id
          FOR UPDATE OF t SKIP LOCKED LIMIT 1
        """)).first()
        if row is None:
            return False
        reconcile_task(connection, row[0])
        return True

def reconcile_task(connection: Any, task_id: UUID) -> str:
    row=connection.execute(text("""SELECT t.execution_state,t.run_id,t.attempt_count,t.error_code,t.error_message,
      t.execution_phase,
      (t.claim_expires_at IS NOT NULL AND t.claim_expires_at<=clock_timestamp()) AS claim_expired,
      r.status,r.commit_result,r.rows_inserted,r.rows_updated,r.rows_unchanged FROM reporting.async_tasks t
      JOIN reporting.pipeline_publication_runs r ON r.run_id=t.run_id
      WHERE t.task_id=:task_id FOR UPDATE OF t"""),{"task_id":task_id}).mappings().first()
    if row is None:return "not_found"
    if row["execution_state"]=="failure_pending_dlq":
        try:
            with connection.begin_nested():
                confirmed=insert_dead_letter(connection,task_id=task_id,run_id=row["run_id"],
                  attempt_number=max(1,int(row["attempt_count"])),
                  reason="retries_exhausted" if int(row["attempt_count"])>=3 else "non_retryable",
                  error_code=row["error_code"] or "terminal_failure",
                  error_message=row["error_message"] or "terminal task failure")
        except Exception:
            confirmed=False
        if confirmed:
            connection.execute(text("""UPDATE reporting.async_tasks SET execution_state='dead_lettered',
              celery_state='FAILURE',updated_at=clock_timestamp() WHERE task_id=:task_id
              AND execution_state='failure_pending_dlq'"""),{"task_id":task_id})
            return "dead_lettered"
        return "failure_pending_dlq"
    if row["execution_state"] in {"succeeded","dead_lettered","failed","reconciliation_required"}:return "unchanged"
    status=row["status"]
    if row["commit_result"]=="uncertain": state,published="reconciliation_required",None
    elif status in {"succeeded","succeeded_noop"} and row["commit_result"]=="technically_observed": state,published="succeeded",True
    elif status=="blocked_quality": state,published="succeeded",False
    elif status in {"superseded","skipped_in_progress","failed_retryable"}: state,published="failed",False
    elif (row["execution_state"]=="running" and row["execution_phase"]=="prefect_started"
          and row["claim_expired"]):
        connection.execute(text("""UPDATE reporting.async_tasks SET
          execution_state='reconciliation_required',execution_phase='terminal',
          pipeline_status='execution_abandoned',
          published=NULL,error_code='execution_abandoned',
          error_message='worker heartbeat expired after Prefect started',
          claim_expires_at=NULL,finished_at=clock_timestamp(),updated_at=clock_timestamp()
          WHERE task_id=:task_id AND execution_state='running'
            AND execution_phase='prefect_started' AND claim_expires_at<=clock_timestamp()"""),
          {"task_id":task_id})
        return "reconciliation_required"
    else:return "ambiguous"
    result=json.dumps({"pipeline_status":status,"commit_result":row["commit_result"],"counts":{
      "inserted":row["rows_inserted"] or 0,"updated":row["rows_updated"] or 0,"unchanged":row["rows_unchanged"] or 0}},separators=(",",":"))
    connection.execute(text("""UPDATE reporting.async_tasks SET execution_state=:state,execution_phase='terminal',
      pipeline_status=:status,published=:published,result_summary=CAST(:result AS jsonb),claim_token=NULL,
      claim_expires_at=NULL,finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE task_id=:task_id"""),
      {"task_id":task_id,"state":state,"status":status,"published":published,"result":result})
    return state
