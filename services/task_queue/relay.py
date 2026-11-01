"""Independent PostgreSQL-to-Celery transactional-outbox relay."""
from __future__ import annotations
import json, logging, os, signal, time
from typing import Any, Callable
from uuid import UUID, uuid4
from sqlalchemy import create_engine, text
from pipelines.database_identity import sqlalchemy_postgresql_url
from services.task_queue.celery_app import celery_app
from services.task_queue.sanitization import public_exception
from services.task_queue.reconciliation import reconcile_one

logger = logging.getLogger(__name__)

def _claim_one(connection: Any, owner: str, token: UUID, lease_seconds: int, max_attempts: int):
    connection.execute(text("""UPDATE reporting.task_dispatch_outbox
      SET dispatch_status='publication_uncertain',last_error_code='publication_uncertain',updated_at=clock_timestamp()
      WHERE dispatch_status='publishing' AND relay_lease_expires_at<=clock_timestamp()"""))
    connection.execute(text("""UPDATE reporting.task_dispatch_outbox
      SET dispatch_status='reconciliation_required',last_error_code='publish_attempts_exhausted',updated_at=clock_timestamp()
      WHERE dispatch_status IN ('pending','retry_wait','publication_uncertain') AND publish_attempts>=:max_attempts"""),
      {"max_attempts":max_attempts})
    connection.execute(text("""UPDATE reporting.async_tasks SET dispatch_status='reconciliation_required',
      updated_at=clock_timestamp() WHERE task_id IN (SELECT task_id FROM reporting.task_dispatch_outbox
      WHERE dispatch_status='reconciliation_required') AND dispatch_status<>'reconciliation_required'"""))
    row = connection.execute(text("""SELECT task_id,payload FROM reporting.task_dispatch_outbox
      WHERE dispatch_status IN ('pending','retry_wait','publication_uncertain') AND publish_attempts<:max_attempts
      AND next_attempt_at<=clock_timestamp()
      AND (relay_lease_expires_at IS NULL OR relay_lease_expires_at<=clock_timestamp())
      ORDER BY next_attempt_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1"""),{"max_attempts":max_attempts}).mappings().first()
    if row is None: return None
    return connection.execute(text("""UPDATE reporting.task_dispatch_outbox SET dispatch_status='publishing',
      relay_owner=:owner,relay_token=:token,relay_lease_expires_at=clock_timestamp()+(:lease*INTERVAL '1 second'),
      publish_attempts=publish_attempts+1,updated_at=clock_timestamp() WHERE task_id=:task_id
      RETURNING task_id,payload,publish_attempts"""), {"task_id":row["task_id"],"owner":owner,
      "token":token,"lease":lease_seconds}).mappings().first()

def _publish(payload: dict[str, Any]) -> None:
    celery_app.send_task("trackflow.run_weekly_pipeline", task_id=str(payload["task_id"]),
      kwargs={"contract_version":int(payload["contract_version"]),"run_id":str(payload["run_id"]),
              "week_start":str(payload["week_start"])})

def relay_once(engine: Any, *, owner: str, publisher: Callable[[dict[str, Any]],None]=_publish,
               lease_seconds: int=30, max_backoff_seconds: int=300, max_publish_attempts: int=8) -> bool:
    token=uuid4()
    with engine.begin() as connection: claimed=_claim_one(connection,owner,token,lease_seconds,max_publish_attempts)
    if claimed is None: return False
    payload=claimed["payload"] if not isinstance(claimed["payload"],str) else json.loads(claimed["payload"])
    try: publisher(dict(payload))
    except Exception as exc:
        code,_=public_exception(exc); delay=min(max_backoff_seconds,2**min(int(claimed["publish_attempts"]),8))
        with engine.begin() as connection:
            connection.execute(text("""UPDATE reporting.task_dispatch_outbox SET
              dispatch_status=CASE WHEN publish_attempts>=:max_attempts THEN 'reconciliation_required' ELSE 'retry_wait' END,
              next_attempt_at=clock_timestamp()+(:delay*INTERVAL '1 second'),relay_owner=NULL,relay_token=NULL,
              relay_lease_expires_at=NULL,last_error_code=:code,updated_at=clock_timestamp()
              WHERE task_id=:task_id AND relay_token=:token AND dispatch_status='publishing'"""),
              {"task_id":claimed["task_id"],"token":token,"delay":delay,"code":code,"max_attempts":max_publish_attempts})
            if int(claimed["publish_attempts"]) >= max_publish_attempts:
                connection.execute(text("""UPDATE reporting.async_tasks SET dispatch_status='reconciliation_required',
                  updated_at=clock_timestamp() WHERE task_id=:task_id"""),{"task_id":claimed["task_id"]})
        logger.warning("task_dispatch_retry task_id=%s error_code=%s",claimed["task_id"],code); return True
    with engine.begin() as connection:
        done=connection.execute(text("""UPDATE reporting.task_dispatch_outbox SET dispatch_status='published',
          published_at=clock_timestamp(),relay_owner=NULL,relay_token=NULL,relay_lease_expires_at=NULL,
          last_error_code=NULL,updated_at=clock_timestamp() WHERE task_id=:task_id AND relay_token=:token
          AND dispatch_status='publishing' RETURNING task_id"""),{"task_id":claimed["task_id"],"token":token}).first()
        if done:
            connection.execute(text("""UPDATE reporting.async_tasks SET dispatch_status='published',
              execution_state=CASE WHEN execution_state='accepted' THEN 'queued' ELSE execution_state END,
              updated_at=clock_timestamp() WHERE task_id=:task_id"""),{"task_id":claimed["task_id"]})
    return True

def main() -> None:
    url=os.environ.get("TRACKFLOW_REPORTING_DATABASE_URL","")
    if not url.startswith(("postgresql://","postgresql+psycopg2://")): raise SystemExit("TRACKFLOW_REPORTING_DATABASE_URL must be PostgreSQL")
    engine=create_engine(sqlalchemy_postgresql_url(url),pool_pre_ping=True); owner=f"relay-{uuid4()}"; stopping=False
    def stop(_signum:int,_frame:Any)->None:
        nonlocal stopping; stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    poll=float(os.environ["TRACKFLOW_RELAY_POLL_SECONDS"])
    lease_seconds=int(os.environ["TRACKFLOW_RELAY_LEASE_SECONDS"])
    max_backoff_seconds=int(os.environ["TRACKFLOW_RELAY_MAX_BACKOFF_SECONDS"])
    max_publish_attempts=int(os.environ["TRACKFLOW_RELAY_MAX_PUBLISH_ATTEMPTS"])
    if poll <= 0 or lease_seconds <= 0 or max_backoff_seconds <= 0 or max_publish_attempts <= 0:
        raise SystemExit("relay timing and attempt configuration must be positive")
    while not stopping:
        dispatched=relay_once(engine,owner=owner,lease_seconds=lease_seconds,
          max_backoff_seconds=max_backoff_seconds,max_publish_attempts=max_publish_attempts)
        reconciled=reconcile_one(engine)
        if not dispatched and not reconciled:
            time.sleep(max(.1,poll))
if __name__=="__main__": main()
