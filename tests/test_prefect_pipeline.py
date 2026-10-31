from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pipelines.pipeline as pipeline
from pipelines.coordinator import CoordinatorResult, CoverageEvidence

WEEK = date(2026, 1, 5)


def runtime(*, observer=None, state_sink=None):
    return pipeline.PipelineRuntime(
        engine=object(),
        coverage=CoverageEvidence(frozenset(), (), False, "synthetic_fixture"),
        dependency_fingerprint="telemetry=a;movements=b;stockexit=c;rules=v1",
        optional_observer=observer,
        optional_state_sink=state_sink,
        clock=lambda: datetime(2026, 1, 19, tzinfo=timezone.utc),
    )


def test_prefect_3_task_contracts_are_safe_and_cache_is_exactly_one_hour():
    from prefect.tasks import Task

    tasks = [
        pipeline.extract_week_task,
        pipeline.relay_outbox_task,
        pipeline.transform_week_task,
        pipeline.create_run_task,
        pipeline.acquire_lease_task,
        pipeline.fail_run_task,
        pipeline.quality_gate_task,
        pipeline.publish_week_task,
        pipeline.optional_observer_task,
    ]
    assert len(tasks) == 9 and all(isinstance(item, Task) for item in tasks)
    assert pipeline.extract_week_task.retries == 2
    assert pipeline.extract_week_task.retry_delay_seconds == 5
    assert pipeline.publish_week_task.retries == 0
    assert pipeline.create_run_task.retries == 0
    assert pipeline.acquire_lease_task.retries == 0
    assert pipeline.transform_week_task.cache_expiration == timedelta(hours=1)
    assert pipeline.transform_week_task.cache_key_fn is pipeline.transformation_cache_key


def test_transform_cache_key_invalidates_every_required_dimension():
    extracted = SimpleNamespace(metadata=SimpleNamespace(week_start=WEEK), events=("evidence-a",))
    base = {
        "extracted": extracted,
        "week_start": WEEK,
        "dependency_fingerprint": "telemetry=a;movements=b;stockexit=c;rules=v1",
        "rules_version": "v1",
    }
    original = pipeline.transformation_cache_key(None, base)
    variants = (
        {**base, "week_start": date(2026, 1, 12)},
        {**base, "extracted": SimpleNamespace(metadata=extracted.metadata, events=("evidence-b",))},
        {**base, "dependency_fingerprint": "telemetry=z;movements=b;stockexit=c;rules=v1"},
        {**base, "rules_version": "v2"},
        {**base, "technical_coverage_identity": "attestation-b"},
    )
    assert all(pipeline.transformation_cache_key(None, item) != original for item in variants)


def test_extract_and_transform_tasks_delegate_without_reimplementing(monkeypatch):
    extracted = SimpleNamespace(
        metadata=SimpleNamespace(week_start=WEEK),
        events=("rows",),
        commercial_order_evidence="orders",
        movement_week_evidence="movement",
    )
    monkeypatch.setattr(pipeline, "extract_weekly_inputs", lambda engine, week: extracted)
    assert pipeline.extract_week_task.fn("engine", WEEK) is extracted
    observed = {}

    def transform(events, *, commercial_order_evidence, movement_week_evidence):
        observed.update(events=events, orders=commercial_order_evidence, movement=movement_week_evidence)
        return "transformed"

    monkeypatch.setattr(pipeline, "transform_weekly_performance", transform)
    assert pipeline.transform_week_task.fn(extracted, WEEK, "fingerprint", "rules") == "transformed"
    assert observed == {"events": ("rows",), "orders": "orders", "movement": "movement"}


def test_extract_task_uses_optional_source_bind(monkeypatch):
    observed = {}

    def extract(engine, week, *, inventory_bind):
        observed.update(engine=engine, week=week, inventory_bind=inventory_bind)
        return "extracted"

    monkeypatch.setattr(pipeline, "extract_weekly_inputs", extract)

    assert pipeline.extract_week_task.fn("reporting", WEEK, "source") == "extracted"
    assert observed == {"engine": "reporting", "week": WEEK, "inventory_bind": "source"}


def test_flow_relays_before_extraction_when_source_is_distinct(monkeypatch):
    configured = runtime()
    configured = pipeline.PipelineRuntime(
        configured.engine,
        configured.coverage,
        configured.dependency_fingerprint,
        source_engine=object(),
        clock=configured.clock,
    )
    run_id = uuid4()
    calls = []
    expected = CoordinatorResult(WEEK, run_id, "superseded")
    state = lambda value: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: value)
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    monkeypatch.setattr(
        pipeline,
        "relay_outbox_task",
        lambda *_args, **_kwargs: calls.append("relay") or state(pipeline.RelayResult(1, 0)),
    )
    audit = SimpleNamespace(complete=False, evidence=configured.coverage)
    monkeypatch.setattr(
        pipeline,
        "audit_operational_coverage_task",
        lambda *_args, **_kwargs: calls.append("audit") or state(audit),
    )
    extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
    monkeypatch.setattr(
        pipeline,
        "extract_week_task",
        lambda *_args, **_kwargs: calls.append("extract") or state(extracted),
    )
    monkeypatch.setattr(pipeline, "transform_week_task", lambda *_args, **_kwargs: state(object()))
    monkeypatch.setattr(pipeline, "quality_gate_task", lambda *_args: None)
    monkeypatch.setattr(
        pipeline, "acquire_lease_task", lambda *_args: pipeline.PreparedRun(run_id, None, expected)
    )
    monkeypatch.setattr(pipeline, "_run_optional_observer", lambda *_args: None)

    assert pipeline.weekly_performance_flow.fn(configured, WEEK) is expected
    assert calls == ["relay", "audit", "extract"]


def test_relay_failure_is_persisted_and_stops_before_extraction(monkeypatch):
    configured = runtime()
    configured = pipeline.PipelineRuntime(
        configured.engine,
        configured.coverage,
        configured.dependency_fingerprint,
        source_engine=object(),
        clock=configured.clock,
    )
    run_id = uuid4()
    expected = CoordinatorResult(WEEK, run_id, "failed_retryable")
    persisted = []
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    monkeypatch.setattr(
        pipeline,
        "relay_outbox_task",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_failed=lambda: True,
            result=lambda **_kw: RuntimeError("outbox_relay_incomplete"),
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "fail_run_task",
        lambda _runtime, _week, _run, stage, _error: persisted.append(stage) or expected,
    )
    monkeypatch.setattr(
        pipeline,
        "extract_week_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("extract must not run")),
    )
    monkeypatch.setattr(pipeline, "_run_optional_observer", lambda *_args: None)

    assert pipeline.weekly_performance_flow.fn(configured, WEEK) is expected
    assert persisted == ["relay"]


def test_optional_failure_is_visible_and_actual_flow_returns_main_result(monkeypatch):
    def fail(_week, _run_id):
        raise RuntimeError("non-critical observer unavailable")

    run_id = uuid4()
    expected = CoordinatorResult(WEEK, run_id, "superseded")
    states = []
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
    monkeypatch.setattr(
        pipeline,
        "extract_week_task",
        lambda *_args, **_kwargs: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: extracted),
    )
    transformed = SimpleNamespace(aggregates=(), quality=None)
    monkeypatch.setattr(
        pipeline,
        "transform_week_task",
        lambda *_args, **_kwargs: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: transformed),
    )
    monkeypatch.setattr(pipeline, "quality_gate_task", lambda *_args: None)
    monkeypatch.setattr(
        pipeline, "acquire_lease_task", lambda *_args: pipeline.PreparedRun(run_id, None, expected)
    )
    result = pipeline.weekly_performance_flow(
        runtime(observer=fail, state_sink=states.append), WEEK
    )
    assert result is expected
    assert len(states) == 1 and states[0].is_failed()
    assert "non-critical observer unavailable" in str(states[0].result(raise_on_failure=False))


def test_flow_preserves_terminal_non_success_and_requests_optional_state(monkeypatch):
    run_id = uuid4()
    expected = CoordinatorResult(WEEK, run_id, "superseded")
    calls = []
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
    monkeypatch.setattr(
        pipeline,
        "extract_week_task",
        lambda *_args, **_kwargs: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: extracted),
    )
    transformed = SimpleNamespace(aggregates=(), quality=None)
    monkeypatch.setattr(
        pipeline,
        "transform_week_task",
        lambda *_args, **_kwargs: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: transformed),
    )
    monkeypatch.setattr(pipeline, "quality_gate_task", lambda *_args: None)
    monkeypatch.setattr(
        pipeline, "acquire_lease_task", lambda *_args: pipeline.PreparedRun(run_id, None, expected)
    )
    monkeypatch.setattr(
        pipeline, "_run_optional_observer", lambda *_args: calls.append("observed")
    )
    result = pipeline.weekly_performance_flow.fn(runtime(), WEEK)
    assert result.status == "superseded"
    assert calls == ["observed"]


def test_extract_and_transform_failures_persist_without_acquiring_lease(monkeypatch):
    for failed_stage in ("extract", "transform"):
        run_id = uuid4()
        persisted = []
        monkeypatch.setattr(pipeline, "create_run_task", lambda *_args, value=run_id: value)
        extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
        extract_failed = failed_stage == "extract"
        monkeypatch.setattr(
            pipeline,
            "extract_week_task",
            lambda *_args, failed=extract_failed, **_kwargs: SimpleNamespace(
                is_failed=lambda: failed,
                result=lambda **_kw: RuntimeError("extract failed") if failed else extracted,
            ),
        )
        transform_failed = failed_stage == "transform"
        monkeypatch.setattr(
            pipeline,
            "transform_week_task",
            lambda *_args, failed=transform_failed, **_kwargs: SimpleNamespace(
                is_failed=lambda: failed,
                result=lambda **_kw: RuntimeError("transform failed") if failed else object(),
            ),
        )
        monkeypatch.setattr(
            pipeline,
            "fail_run_task",
            lambda _runtime, _week, _run, stage, _error, *_counts: persisted.append(stage)
            or CoordinatorResult(WEEK, run_id, "failed_retryable"),
        )
        monkeypatch.setattr(
            pipeline,
            "acquire_lease_task",
            lambda *_args: (_ for _ in ()).throw(AssertionError("lease must not be acquired")),
        )
        monkeypatch.setattr(pipeline, "_run_optional_observer", lambda *_args: None)
        result = pipeline.weekly_performance_flow.fn(runtime(), WEEK)
        assert result.status == "failed_retryable"
        assert persisted == [failed_stage]


def test_quality_block_is_terminal_before_lease(monkeypatch):
    run_id = uuid4()
    blocked = CoordinatorResult(WEEK, run_id, "blocked_quality", errors=("coverage_pending",))
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
    state = lambda value: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: value)
    monkeypatch.setattr(pipeline, "extract_week_task", lambda *_args, **_kwargs: state(extracted))
    monkeypatch.setattr(pipeline, "transform_week_task", lambda *_args, **_kwargs: state(object()))
    monkeypatch.setattr(pipeline, "quality_gate_task", lambda *_args: blocked)
    monkeypatch.setattr(
        pipeline,
        "acquire_lease_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("blocked run must not acquire lease")),
    )
    monkeypatch.setattr(pipeline, "_run_optional_observer", lambda *_args: None)
    assert pipeline.weekly_performance_flow.fn(runtime(), WEEK) is blocked


def test_runner_delegates_to_the_prefect_flow(monkeypatch):
    expected = CoordinatorResult(WEEK, uuid4(), "commit_uncertain")
    monkeypatch.setattr(pipeline, "weekly_performance_flow", lambda *_args: expected)
    assert pipeline.run_weekly_pipeline(runtime(), WEEK) is expected


def test_publish_task_wires_records_processed_to_coordinator(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def begin(self):
            return None

    class Engine:
        def connect(self):
            return Connection()

    observed = {}
    expected = CoordinatorResult(WEEK, uuid4(), "succeeded")

    def publish(**kwargs):
        observed.update(kwargs)
        return expected

    monkeypatch.setattr(pipeline, "publish_weekly_business_report", publish)
    configured = runtime()
    configured = pipeline.PipelineRuntime(
        Engine(), configured.coverage, configured.dependency_fingerprint, clock=configured.clock
    )
    prepared = pipeline.PreparedRun(
        expected.run_id,
        pipeline.ConfirmedLeaseRight(WEEK, expected.run_id, 1, True),
    )
    transformed = SimpleNamespace(aggregates=(), quality=None)
    assert pipeline.publish_week_task.fn(configured, WEEK, prepared, transformed, 17) is expected
    assert observed["records_processed"] == 17
