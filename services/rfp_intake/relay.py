"""Independent operational PostgreSQL-to-Celery relay for RFP jobs."""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any, Callable

from sqlalchemy import create_engine
from sqlmodel import Session

from pipelines.database_identity import sqlalchemy_postgresql_url
from services.rfp_intake.store import claim_outbox, finish_outbox
from services.task_queue.celery_app import celery_app
from services.task_queue.sanitization import public_exception

logger = logging.getLogger(__name__)


def _publish(claimed: dict[str, Any]) -> None:
    celery_app.send_task("trackflow.process_rfp", task_id=str(claimed["outbox_id"]),
                         kwargs={"job_id": str(claimed["job_id"])})


def relay_once(engine: Any, *, publisher: Callable[[dict[str, Any]], None] = _publish,
               lease_seconds: int = 600) -> bool:
    with Session(engine) as session:
        with session.begin():
            claimed = claim_outbox(session, lease_seconds=lease_seconds)
    if claimed is None:
        return False
    try:
        publisher(claimed)
    except Exception as exc:
        code, _ = public_exception(exc)
        with Session(engine) as session:
            with session.begin():
                finish_outbox(session, claimed=claimed, published=False, error_code=code)
        logger.warning("rfp_dispatch_retry outbox_id=%s error_code=%s", claimed["outbox_id"], code)
        return True
    with Session(engine) as session:
        with session.begin():
            finish_outbox(session, claimed=claimed, published=True)
    return True


def main() -> None:
    url = os.getenv("DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise SystemExit("DATABASE_URL must be PostgreSQL")
    engine = create_engine(sqlalchemy_postgresql_url(url), pool_pre_ping=True)
    stopping = False
    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    poll = float(os.getenv("RFP_RELAY_POLL_SECONDS", "1"))
    if poll <= 0:
        raise SystemExit("RFP_RELAY_POLL_SECONDS must be positive")
    while not stopping:
        if not relay_once(engine):
            time.sleep(poll)


if __name__ == "__main__":
    main()
