"""Static infrastructure contracts for the DEV-55 durable queue."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DDL = ROOT / "infra/sql/reporting_async_tasks.sql"
RUNBOOK = ROOT / "services/task_queue/README.md"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_explicit_ddl_declares_registry_outbox_and_idempotent_dlq() -> None:
    sql = DDL.read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())

    assert "create table if not exists reporting.async_tasks" in normalized
    assert "create table if not exists reporting.task_dispatch_outbox" in normalized
    assert "create table if not exists reporting.task_dead_letters" in normalized
    assert "task_id uuid primary key" in normalized
    assert "foreign key (run_id) references reporting.pipeline_publication_runs" in normalized
    assert "foreign key (task_id) references reporting.async_tasks" in normalized
    assert "where execution_state = 'claimed'" in normalized
    assert "where dispatch_status in ('pending', 'retry_wait')" in normalized
    assert "drop table" not in normalized
    assert "truncate" not in normalized


def test_ddl_contains_claim_fencing_retry_and_safe_error_fields() -> None:
    sql = DDL.read_text(encoding="utf-8").lower()

    for field in (
        "attempt_id",
        "attempt_count",
        "claim_token",
        "execution_epoch",
        "execution_phase",
        "claim_expires_at",
        "heartbeat_at",
        "next_attempt_at",
        "relay_token",
        "relay_lease_expires_at",
        "error_code",
        "error_message",
    ):
        assert field in sql
    assert "attempt_count between 0 and 3" in sql
    assert "length(error_message) <= 512" in sql


def test_task_relay_is_opt_in_independent_and_redis_ordered() -> None:
    services = _compose()["services"]
    relay = services["task-relay"]

    assert relay["profiles"] == ["async-tasks"]
    assert relay["command"] == ["python", "-m", "services.task_queue.relay"]
    assert "REDIS_URL=redis://redis:6379/0" in relay["environment"]
    for name in (
        "TRACKFLOW_RELAY_POLL_SECONDS",
        "TRACKFLOW_RELAY_LEASE_SECONDS",
        "TRACKFLOW_RELAY_MAX_BACKOFF_SECONDS",
        "TRACKFLOW_RELAY_MAX_PUBLISH_ATTEMPTS",
    ):
        assert f"{name}=${{{name}:-}}" in relay["environment"]
    assert relay["depends_on"] == {"redis": {"condition": "service_healthy"}}
    assert "api" not in relay["depends_on"]
    assert relay["restart"] == "unless-stopped"


def test_api_has_coherent_broker_config_without_redis_start_dependency() -> None:
    api = _compose()["services"]["api"]

    assert "REDIS_URL=redis://redis:6379/0" in api["environment"]
    assert "depends_on" not in api or "redis" not in api["depends_on"]
    assert "profiles" not in api


def test_worker_requires_measured_limits_instead_of_guessing_defaults() -> None:
    worker = _compose()["services"]["celery-worker"]
    command = worker["command"][2]
    environment = set(worker["environment"])

    names = (
        "TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS",
        "TRACKFLOW_TASK_TIME_LIMIT_SECONDS",
        "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS",
        "TRACKFLOW_RESULT_EXPIRES_SECONDS",
        "TRACKFLOW_TASK_CLAIM_LEASE_SECONDS",
        "TRACKFLOW_TASK_HEARTBEAT_SECONDS",
        "TRACKFLOW_TASK_RETRY_MAX_BACKOFF_SECONDS",
        "TRACKFLOW_WORKER_SHUTDOWN_GRACE_SECONDS",
    )
    for name in names:
        assert f'test -n "$${name}"' in command
        assert f"{name}=${{{name}:-}}" in environment
    assert "exec celery" in command
    assert worker["stop_grace_period"] == "${TRACKFLOW_WORKER_SHUTDOWN_GRACE_SECONDS:-30}s"


def test_flower_requires_auth_and_remains_loopback_only() -> None:
    flower = _compose()["services"]["flower"]
    command = flower["command"][2]

    assert 'test -n "$$FLOWER_BASIC_AUTH"' in command
    assert "exec celery" in command and " flower " in f" {command} "
    assert flower["ports"] == ["127.0.0.1:5555:5555"]
    assert "FLOWER_BASIC_AUTH=${FLOWER_BASIC_AUTH:-}" in flower["environment"]


def test_runbook_requires_explicit_ddl_limits_and_safe_stop() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "infra/sql/reporting_async_tasks.sql" in runbook
    assert "never create these tables at startup" in runbook
    assert "measured isolated-pipeline run" in runbook
    assert "task-relay" in runbook
    assert "does not contact Redis" in runbook
    assert "does not start Redis unexpectedly" in runbook
    assert "down -v" in runbook
