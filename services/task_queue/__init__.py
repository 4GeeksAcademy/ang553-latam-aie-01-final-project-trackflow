"""TrackFlow's process-independent task service.

Keep package import side-effect free so FastAPI can use the PostgreSQL store
without installing or initializing Celery. Worker processes import
``services.task_queue.celery_app`` explicitly.
"""
