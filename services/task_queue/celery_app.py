"""Central Celery application for TrackFlow asynchronous tasks."""

from __future__ import annotations

import os

from celery import Celery

REDIS_URL_ENV = "REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
SOFT_LIMIT_ENV = "TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS"
HARD_LIMIT_ENV = "TRACKFLOW_TASK_TIME_LIMIT_SECONDS"
VISIBILITY_ENV = "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS"
RESULT_EXPIRES_ENV = "TRACKFLOW_RESULT_EXPIRES_SECONDS"


def resolve_runtime_limits(environment: dict[str, str] | None = None) -> dict[str, int]:
    """Validate operator-supplied measured limits without inventing durations."""
    values = os.environ if environment is None else environment
    names = (SOFT_LIMIT_ENV, HARD_LIMIT_ENV, VISIBILITY_ENV, RESULT_EXPIRES_ENV)
    present = [name in values and str(values[name]).strip() != "" for name in names]
    if not any(present):
        return {}
    if not all(present):
        raise ValueError("all Celery runtime limit variables must be configured together")
    resolved = {name: int(values[name]) for name in names}
    if any(value <= 0 for value in resolved.values()):
        raise ValueError("Celery runtime limits must be positive")
    if not resolved[SOFT_LIMIT_ENV] < resolved[HARD_LIMIT_ENV] < resolved[VISIBILITY_ENV]:
        raise ValueError("Celery limits must satisfy soft < hard < visibility")
    if resolved[RESULT_EXPIRES_ENV] <= resolved[HARD_LIMIT_ENV]:
        raise ValueError("Celery result retention must exceed the hard time limit")
    return resolved


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
    limits = resolve_runtime_limits()
    if limits:
        visibility = limits[VISIBILITY_ENV]
        app.conf.update(
            task_soft_time_limit=limits[SOFT_LIMIT_ENV],
            task_time_limit=limits[HARD_LIMIT_ENV],
            result_expires=limits[RESULT_EXPIRES_ENV],
            visibility_timeout=visibility,
            broker_transport_options={"visibility_timeout": visibility},
            result_backend_transport_options={"visibility_timeout": visibility},
        )
    return app


celery_app = create_celery_app()
