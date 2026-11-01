"""Focused, database-free contracts for DEV-55 Block B safety boundaries."""

from uuid import uuid4

from services.task_queue import reconciliation, relay, store, tasks
from services.task_queue.celery_app import resolve_runtime_limits
from services.task_queue.sanitization import sanitize_message
from services.task_queue.tasks import run_weekly_pipeline_task
from pipelines.database_identity import sqlalchemy_postgresql_url


def test_sanitizer_removes_supported_secret_shapes():
    message = (
        "postgresql://alice:p4ss@db/report Bearer abc.def.ghi "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature api_key=topsecret"
    )
    safe = sanitize_message(message)
    for secret in ("alice", "p4ss", "abc.def.ghi", "eyJhbGci", "topsecret"):
        assert secret not in safe


def test_claim_sql_recovers_only_expired_preflight_claims():
    sql = str(store._CLAIM)
    assert "execution_phase='preflight'" in sql
    assert "claim_expires_at<=clock_timestamp()" in sql
    assert "execution_state='running'" not in sql
    assert "execution_epoch=execution_epoch+1" in sql


def test_prefect_boundary_is_fenced_by_token_and_epoch():
    sql = str(store._START_PREFECT)
    assert "claim_token=:claim_token" in sql
    assert "execution_epoch=:epoch" in sql
    assert "execution_phase='preflight'" in sql
    assert "claim_expires_at>clock_timestamp()" in sql
    assert "claim_expires_at=clock_timestamp()+(:lease_seconds" in sql


def test_running_heartbeat_is_fenced_and_abandonment_requires_reconciliation():
    sql = str(store._HEARTBEAT)
    assert "execution_state='running'" in sql
    assert "execution_phase='prefect_started'" in sql
    assert "claim_token=:claim_token" in sql
    assert "execution_epoch=:epoch" in sql
    import inspect
    source = inspect.getsource(reconciliation.reconcile_task)
    assert "claim_expires_at<=clock_timestamp()" in source
    assert "execution_abandoned" in source
    assert "reconciliation_required" in source


def test_durable_transitions_record_real_celery_lifecycle_states():
    assert "celery_state='STARTED'" in str(store._CLAIM)
    assert "celery_state='RETRY'" in str(store._RETRY)
    assert "celery_state=:celery_state" in str(store._FINISH)


def test_terminal_preflight_failure_records_kpi_as_not_published():
    import inspect
    source = inspect.getsource(store.fail_claim)
    assert "published=FALSE" in source


def test_three_failed_executions_are_the_durable_limit():
    assert store.MAX_FAILED_EXECUTIONS == 3
    assert run_weekly_pipeline_task.max_retries == 3


def test_relay_uses_stable_task_id_and_lightweight_payload(monkeypatch):
    task_id, run_id = uuid4(), uuid4()
    observed = {}
    monkeypatch.setattr(relay.celery_app, "send_task", lambda *args, **kwargs: observed.update(args=args, kwargs=kwargs))
    relay._publish({"contract_version": 1, "task_id": str(task_id),
                    "run_id": str(run_id), "week_start": "2026-01-05"})
    assert observed["kwargs"]["task_id"] == str(task_id)
    assert observed["kwargs"]["kwargs"] == {
        "contract_version": 1, "run_id": str(run_id), "week_start": "2026-01-05"
    }


def test_dead_letter_insert_is_idempotent_by_task_id():
    import inspect
    source = inspect.getsource(store.insert_dead_letter)
    assert "ON CONFLICT (task_id) DO UPDATE" in source
    assert "task_dead_letters.run_id=EXCLUDED.run_id" in source


def test_runtime_limits_require_complete_ordered_configuration():
    values = {"TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS": "10", "TRACKFLOW_TASK_TIME_LIMIT_SECONDS": "20",
              "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS": "30", "TRACKFLOW_RESULT_EXPIRES_SECONDS": "40"}
    assert resolve_runtime_limits(values)["TRACKFLOW_TASK_TIME_LIMIT_SECONDS"] == 20


def test_runtime_limits_reject_partial_or_unsafe_ordering():
    import pytest
    with pytest.raises(ValueError, match="configured together"):
        resolve_runtime_limits({"TRACKFLOW_TASK_TIME_LIMIT_SECONDS": "20"})
    with pytest.raises(ValueError, match="soft < hard < visibility"):
        resolve_runtime_limits({"TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS": "20", "TRACKFLOW_TASK_TIME_LIMIT_SECONDS": "10",
            "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS": "30", "TRACKFLOW_RESULT_EXPIRES_SECONDS": "40"})
    with pytest.raises(ValueError, match="retention"):
        resolve_runtime_limits({"TRACKFLOW_TASK_SOFT_TIME_LIMIT_SECONDS": "10", "TRACKFLOW_TASK_TIME_LIMIT_SECONDS": "20",
            "TRACKFLOW_VISIBILITY_TIMEOUT_SECONDS": "30", "TRACKFLOW_RESULT_EXPIRES_SECONDS": "20"})


def test_registry_uses_reporting_database_not_pipeline_runtime(monkeypatch):
    observed = {}
    monkeypatch.setenv("TRACKFLOW_REPORTING_DATABASE_URL", "postgresql://qa.invalid/reporting")
    monkeypatch.setenv("TRACKFLOW_PIPELINE_DATABASE_URL", "postgresql://qa.invalid/pipeline")
    monkeypatch.setattr(tasks, "_task_engine", None)
    monkeypatch.setattr(tasks, "create_engine", lambda url, **kwargs: observed.update(url=url) or object())
    tasks.get_task_engine()
    assert observed["url"] == "postgresql+psycopg2://qa.invalid/reporting"


def test_plain_postgresql_url_selects_the_installed_psycopg2_driver():
    assert sqlalchemy_postgresql_url("postgresql://user:secret@db/name") == (
        "postgresql+psycopg2://user:secret@db/name"
    )
    assert sqlalchemy_postgresql_url("postgresql+psycopg2://db/name") == (
        "postgresql+psycopg2://db/name"
    )


def test_relay_records_uncertain_publish_and_bounds_attempts():
    import inspect
    source = inspect.getsource(relay._claim_one)
    assert "publication_uncertain" in source
    assert "publish_attempts>=:max_attempts" in source
    assert "reconciliation_required" in source


def test_reconciler_repairs_dlq_before_marking_dead_lettered():
    import inspect
    source = inspect.getsource(reconciliation.reconcile_task)
    assert 'execution_state\"]==\"failure_pending_dlq' in source
    assert "insert_dead_letter" in source
    assert "dead_lettered" in source
    scan_source = inspect.getsource(reconciliation.reconcile_one)
    assert "FOR UPDATE OF t SKIP LOCKED" in scan_source
    assert "failure_pending_dlq" in scan_source
    assert "claim_expires_at<=clock_timestamp()" in scan_source
    assert scan_source.count("claim_expires_at<=clock_timestamp()") == 2
    assert "t.execution_state='running'" in scan_source
    assert "t.execution_phase='prefect_started'" in scan_source


def test_dlq_write_failure_isolated_for_failure_pending_state(monkeypatch):
    class Savepoint:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    class Connection:
        def begin_nested(self): return Savepoint()
    monkeypatch.setattr(tasks, "insert_dead_letter", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db")))
    assert tasks._try_dead_letter(Connection(), task_id=uuid4()) is False
