"""TrackFlow's process-independent Celery task service."""

from services.task_queue.celery_app import celery_app

__all__ = ["celery_app"]
