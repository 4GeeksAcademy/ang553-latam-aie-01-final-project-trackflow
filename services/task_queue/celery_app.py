"""Central Celery application for TrackFlow asynchronous tasks."""

from __future__ import annotations

import os

from celery import Celery

REDIS_URL_ENV = "REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def resolve_redis_url(environment: dict[str, str] | None = None) -> str:
    """Resolve the broker/backend URL without logging connection details."""
    values = os.environ if environment is None else environment
    value = values.get(REDIS_URL_ENV, DEFAULT_REDIS_URL).strip()
    if not value.startswith(("redis://", "rediss://")):
        raise ValueError("REDIS_URL must use redis:// or rediss://")
    return value


def create_celery_app(redis_url: str | None = None) -> Celery:
    """Build the shared app with JSON-only transport and worker safety defaults."""
    resolved_url = redis_url or resolve_redis_url()
    app = Celery(
        "trackflow",
        broker=resolved_url,
        backend=resolved_url,
        include=["services.task_queue.tasks"],
    )
    app.conf.update(
        accept_content=["json"],
        task_serializer="json",
        result_serializer="json",
        event_serializer="json",
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        worker_send_task_events=True,
        task_send_sent_event=True,
        broker_connection_retry_on_startup=True,
        enable_utc=True,
        timezone="UTC",
    )
    return app


celery_app = create_celery_app()
