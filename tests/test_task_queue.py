from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from pipelines.coordinator import CoordinatorResult
from services.task_queue.celery_app import create_celery_app, resolve_redis_url
from services.task_queue.tasks import (
    RUN_WEEKLY_PIPELINE_TASK,
    TaskExecutionError,
    build_worker_runtime,
    run_weekly_pipeline_task,
)


def test_celery_app_uses_redis_json_events_and_safe_worker_defaults():
    app = create_celery_app("redis://queue.example:6379/4")

    assert app.conf.broker_url == "redis://queue.example:6379/4"
    assert app.conf.result_backend == "redis://queue.example:6379/4"
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert app.conf.event_serializer == "json"
    assert app.conf.task_track_started is True
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.worker_send_task_events is True
    assert app.conf.task_send_sent_event is True
    assert "services.task_queue.tasks" in app.conf.include


def test_redis_url_reads_environment_and_rejects_non_redis_schemes():
    assert resolve_redis_url({"REDIS_URL": "rediss://queue.example:6380/2"}) == (
        "rediss://queue.example:6380/2"
    )
    with pytest.raises(ValueError, match="REDIS_URL"):
        resolve_redis_url({"REDIS_URL": "amqp://guest:guest@example/"})


def test_real_weekly_task_is_registered_without_importing_fastapi_app():
    assert run_weekly_pipeline_task.name == RUN_WEEKLY_PIPELINE_TASK
    assert RUN_WEEKLY_PIPELINE_TASK in run_weekly_pipeline_task.app.tasks


def test_worker_runtime_reuses_cli_builder_and_correlates_accepted_run(monkeypatch):
    run_id = uuid4()
    from pipelines.pipeline import PipelineRuntime

    original = PipelineRuntime(
        engine=object(),
        coverage=object(),
        dependency_fingerprint="fingerprint",
    )
    monkeypatch.setattr("services.task_queue.tasks.build_cli_runtime", lambda: original)

    runtime = build_worker_runtime(run_id)

    assert runtime.engine is original.engine
    assert runtime.accepted_run_id == run_id
    assert runtime.trigger_type == "celery"


def test_weekly_task_reuses_pipeline_entry_with_lightweight_contract(monkeypatch):
    run_id = uuid4()
    week = date(2026, 1, 5)
    runtime = object()
    calls = []

    monkeypatch.setattr(
        "services.task_queue.tasks.build_worker_runtime",
        lambda supplied_run_id: calls.append(("runtime", supplied_run_id)) or runtime,
    )
    monkeypatch.setattr(
        "services.task_queue.tasks.run_weekly_pipeline",
        lambda supplied_runtime, supplied_week: calls.append(
            ("runner", supplied_runtime, supplied_week)
        )
        or CoordinatorResult(
            week_start=week,
            run_id=run_id,
            status="blocked_quality",
            errors=("coverage_pending",),
            coverage_source="operational_inventory_outbox",
        ),
    )

    result = run_weekly_pipeline_task.apply(
        kwargs={"run_id": str(run_id), "week_start": week.isoformat()},
        task_id=str(uuid4()),
        throw=True,
    ).get()

    assert calls == [("runtime", run_id), ("runner", runtime, week)]
    assert result["run_id"] == str(run_id)
    assert result["week_start"] == week.isoformat()
    assert result["status"] == "blocked_quality"
    assert result["errors"] == ["coverage_pending"]
    assert result["production_certified"] is False
    json.dumps(result)


def test_result_errors_preserve_codes_and_redact_arbitrary_text(monkeypatch):
    run_id = uuid4()
    secret_marker = "postgresql://user:password@example.invalid/db"
    monkeypatch.setattr("services.task_queue.tasks.build_worker_runtime", lambda _run_id: object())
    monkeypatch.setattr(
        "services.task_queue.tasks.run_weekly_pipeline",
        lambda _runtime, week: CoordinatorResult(
            week_start=week,
            run_id=run_id,
            status="blocked_quality",
            errors=("coverage_pending", secret_marker, "Bearer fake-secret"),
        ),
    )

    result = run_weekly_pipeline_task.apply(
        kwargs={"run_id": str(run_id), "week_start": "2026-01-05"},
        throw=True,
    ).get()
    serialized = json.dumps(result)

    assert result["errors"] == [
        "coverage_pending",
        "redacted_diagnostic",
        "redacted_diagnostic",
    ]
    assert secret_marker not in serialized
    assert "fake-secret" not in serialized


def test_docker_context_excludes_secrets_databases_and_runtime_data():
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {".env", ".env.*", "env.example", "services/api/data/"} <= patterns
    assert {"*.db", "*.sqlite", "*.sqlite3", "*.key", "*.pem"} <= patterns
    assert "data/raw/*" in patterns


def test_weekly_task_rejects_mismatched_pipeline_run_id(monkeypatch):
    requested = uuid4()
    monkeypatch.setattr("services.task_queue.tasks.build_worker_runtime", lambda _run_id: object())
    monkeypatch.setattr(
        "services.task_queue.tasks.run_weekly_pipeline",
        lambda _runtime, week: CoordinatorResult(
            week_start=week,
            run_id=uuid4(),
            status="succeeded",
        ),
    )

    with pytest.raises(TaskExecutionError, match="RuntimeError"):
        run_weekly_pipeline_task.apply(
            kwargs={"run_id": str(requested), "week_start": "2026-01-05"},
            throw=True,
        ).get()


def test_weekly_task_logs_only_safe_identifiers_and_error_type(monkeypatch, caplog):
    secret_marker = "postgresql://user:password@example.invalid/db"

    def fail(_run_id):
        raise RuntimeError(secret_marker)

    monkeypatch.setattr("services.task_queue.tasks.build_worker_runtime", fail)

    with caplog.at_level("ERROR"), pytest.raises(TaskExecutionError) as caught:
        run_weekly_pipeline_task.apply(
            kwargs={"run_id": str(uuid4()), "week_start": "2026-01-05"},
            throw=True,
        ).get()

    assert secret_marker not in caplog.text
    assert secret_marker not in str(caught.value)
    assert "error_type=RuntimeError" in caplog.text


def test_invalid_run_id_is_not_echoed_to_logs_or_result(caplog):
    secret_marker = "postgresql://user:password@example.invalid/db"

    with caplog.at_level("ERROR"), pytest.raises(TaskExecutionError) as caught:
        run_weekly_pipeline_task.apply(
            kwargs={"run_id": secret_marker, "week_start": "2026-01-05"},
            throw=True,
        ).get()

    assert secret_marker not in caplog.text
    assert secret_marker not in str(caught.value)
    assert "run_id=invalid" in caplog.text
