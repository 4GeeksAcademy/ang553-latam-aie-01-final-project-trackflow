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
    from prefect.cache_policies import NO_CACHE
    from prefect.tasks import Task

    uncached_tasks = [
        pipeline.extract_week_task,
        pipeline.relay_outbox_task,
        pipeline.audit_operational_coverage_task,
        pipeline.create_run_task,
        pipeline.acquire_lease_task,
        pipeline.fail_run_task,
        pipeline.quality_gate_task,
        pipeline.publish_week_task,
        pipeline.optional_observer_task,
    ]
    tasks = [*uncached_tasks, pipeline.transform_week_task]
    assert len(tasks) == 10 and all(isinstance(item, Task) for item in tasks)
    assert all(item.cache_policy == NO_CACHE for item in uncached_tasks)
    assert pipeline.extract_week_task.retries == 2
    assert pipeline.extract_week_task.retry_delay_seconds == 5
    assert pipeline.publish_week_task.retries == 0
    assert pipeline.create_run_task.retries == 0
    assert pipeline.acquire_lease_task.retries == 0
    assert pipeline.transform_week_task.cache_expiration == timedelta(hours=1)
    assert pipeline.transform_week_task.cache_key_fn is pipeline.transformation_cache_key


def test_engine_tasks_do_not_hash_sqlalchemy_runtime_inputs():
    from prefect.cache_policies import NO_CACHE
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    try:
        for item in (
            pipeline.extract_week_task,
            pipeline.relay_outbox_task,
            pipeline.audit_operational_coverage_task,
            pipeline.create_run_task,
            pipeline.acquire_lease_task,
            pipeline.fail_run_task,
            pipeline.quality_gate_task,
            pipeline.publish_week_task,
            pipeline.optional_observer_task,
        ):
            assert item.cache_policy == NO_CACHE
            assert (
                item.cache_policy.compute_key(
                    task_ctx=None,
                    inputs={"runtime": runtime(), "engine": engine},
                    flow_parameters={},
                )
                is None
            )
    finally:
        engine.dispose()


def test_prefect_executes_engine_task_without_cache_serialization(monkeypatch, caplog):
    from prefect import flow
    from prefect.testing.utilities import prefect_test_harness
    from sqlalchemy import create_engine

    extracted = SimpleNamespace(metadata=SimpleNamespace(week_start=WEEK), events=())
    monkeypatch.setattr(pipeline, "extract_weekly_inputs", lambda _engine, _week: extracted)
    engine = create_engine("sqlite://")

    @flow(name="trackflow-engine-cache-regression")
    def invoke_engine_task():
        return pipeline.extract_week_task(engine, WEEK)

    try:
        with prefect_test_harness():
            assert invoke_engine_task() == extracted
        assert "Unable to serialize unknown type" not in caplog.text
        assert "cannot pickle '_thread.RLock' object" not in caplog.text
    finally:
        engine.dispose()


def test_domain_subflows_and_metric_tasks_have_explicit_prefect_contracts():
    from prefect.flows import Flow
    from prefect.tasks import Task

    subflows = (
        pipeline.extract_weekly_warehouse_events_flow,
        pipeline.calculate_warehouse_client_indicators_flow,
        pipeline.publish_weekly_business_report_flow,
        pipeline.optional_weekly_observer_flow,
    )
    assert all(isinstance(item, Flow) for item in subflows)
    assert [item.name for item in subflows[:3]] == [
        "trackflow-extract-weekly-warehouse-events",
        "trackflow-calculate-warehouse-client-indicators",
        "trackflow-publish-weekly-business-report",
    ]
    assert all(
        isinstance(item, Task)
        for item in (
            pipeline.calculate_inbound_volume_task,
            pipeline.calculate_outbound_throughput_task,
            pipeline.calculate_stockout_frequency_task,
            pipeline.calculate_discrepancy_metrics_task,
        )
    )


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
    calls = []
    state = lambda value: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: value)
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
    stage = pipeline.extract_weekly_warehouse_events_flow.fn(configured, WEEK)
    assert stage.extracted is extracted
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
        "extract_week_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("extract must not run")),
    )
    stage = pipeline.extract_weekly_warehouse_events_flow.fn(configured, WEEK)
    assert stage.failed_stage == "relay"


def test_optional_failure_is_visible_and_actual_flow_returns_main_result(monkeypatch):
    def fail(_week, _run_id):
        raise RuntimeError("non-critical observer unavailable")

    run_id = uuid4()
    expected = CoordinatorResult(WEEK, run_id, "superseded")
    states = []
    failed_state = SimpleNamespace(
        is_failed=lambda: True,
        result=lambda **_kwargs: RuntimeError("non-critical observer unavailable"),
    )
    monkeypatch.setattr(
        pipeline,
        "optional_weekly_observer_flow",
        lambda *_args, **_kwargs: fail(WEEK, run_id) if False else failed_state,
    )
    configured = runtime(observer=fail, state_sink=states.append)
    returned = pipeline._run_optional_observer(configured, WEEK, run_id)
    assert returned is failed_state
    assert len(states) == 1 and states[0].is_failed()
    assert "non-critical observer unavailable" in str(states[0].result(raise_on_failure=False))


def test_flow_preserves_terminal_non_success_and_requests_optional_state(monkeypatch):
    run_id = uuid4()
    expected = CoordinatorResult(WEEK, run_id, "superseded")
    calls = []
    monkeypatch.setattr(pipeline, "create_run_task", lambda *_args: run_id)
    extracted = SimpleNamespace(metadata=SimpleNamespace(event_count=0), events=())
    transformed = SimpleNamespace(aggregates=(), quality=None)
    state = lambda value: SimpleNamespace(is_failed=lambda: False, result=lambda **_kw: value)
    monkeypatch.setattr(
        pipeline,
        "extract_weekly_warehouse_events_flow",
        lambda configured, *_args, **_kwargs: state(
            pipeline.WeeklyExtractionStage(configured, extracted)
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "calculate_warehouse_client_indicators_flow",
        lambda *_args, **_kwargs: state(transformed),
    )
    monkeypatch.setattr(
        pipeline,
        "publish_weekly_business_report_flow",
        lambda *_args, **_kwargs: state(expected),
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
        state = lambda value, failed=False: SimpleNamespace(
            is_failed=lambda: failed,
            result=lambda **_kw: value,
        )
        monkeypatch.setattr(
            pipeline,
            "extract_weekly_warehouse_events_flow",
            lambda configured, *_args, failed=extract_failed, **_kwargs: state(
                RuntimeError("extract failed")
                if failed
                else pipeline.WeeklyExtractionStage(configured, extracted),
                failed,
            ),
        )
        transform_failed = failed_stage == "transform"
        monkeypatch.setattr(
            pipeline,
            "calculate_warehouse_client_indicators_flow",
            lambda *_args, failed=transform_failed, **_kwargs: state(
                RuntimeError("transform failed") if failed else object(), failed
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
    monkeypatch.setattr(
        pipeline,
        "extract_weekly_warehouse_events_flow",
        lambda configured, *_args, **_kwargs: state(
            pipeline.WeeklyExtractionStage(configured, extracted)
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "calculate_warehouse_client_indicators_flow",
        lambda *_args, **_kwargs: state(object()),
    )
    monkeypatch.setattr(
        pipeline,
        "publish_weekly_business_report_flow",
        lambda *_args, **_kwargs: state(blocked),
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
