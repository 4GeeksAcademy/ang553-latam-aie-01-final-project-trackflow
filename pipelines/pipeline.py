"""Prefect 3 orchestration for TrackFlow's existing weekly pipeline.

The tasks in this module only define orchestration boundaries.  Extraction,
business transformation, publication, locking and run persistence remain owned
by their existing modules.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from prefect import flow, task
from prefect.cache_policies import NO_CACHE
from prefect.runtime import flow_run, task_run
from sqlalchemy import create_engine

from pipelines.database_identity import sqlalchemy_postgresql_url

from pipelines.concurrency import acquire_week_lease, record_lease_run_status
from pipelines.coordinator import (
    ConfirmedLeaseRight,
    CoordinatorResult,
    CoverageEvidence,
    _pre_write_issues,
    publish_weekly_business_report,
)
from pipelines.extraction import WeeklyExtractionResult, extract_weekly_inputs
from pipelines.outbox_relay import RelayResult, relay_inventory_outbox
from pipelines.operational_coverage import (
    OPERATIONAL_COVERAGE_SOURCE,
    OperationalCoverageAudit,
    apply_operational_coverage,
    audit_operational_coverage,
)
from pipelines.run_store import (
    STATUS_BLOCKED_QUALITY,
    STATUS_FAILED_RETRYABLE,
    STATUS_PENDING,
    STATUS_SKIPPED_IN_PROGRESS,
    STATUS_SUPERSEDED,
    correlate_pending_run,
    create_run,
    get_run,
    update_run,
)
from pipelines.weekly_performance import (
    MetricContribution,
    PreparedWeeklyEvents,
    WeeklyTransformationResult,
    calculate_discrepancy_events,
    calculate_inbound_volume,
    calculate_outbound_throughput,
    calculate_stockout_frequency,
    compose_weekly_performance,
    prepare_weekly_events,
    transform_weekly_performance,
)
from pipelines.technical_coverage import (
    TechnicalCoverageEvidence,
    apply_verified_technical_coverage,
)

RULES_VERSION = "weekly-performance-v1"
SAFE_TERMINAL_STATUSES = frozenset(
    {"succeeded", "succeeded_noop", "blocked_quality", "superseded", "skipped_in_progress"}
)
PIPELINE_DATABASE_URL_ENV = "TRACKFLOW_PIPELINE_DATABASE_URL"
PIPELINE_SOURCE_DATABASE_URL_ENV = "TRACKFLOW_PIPELINE_SOURCE_DATABASE_URL"
COVERAGE_EVIDENCE_FILE_ENV = "TRACKFLOW_COVERAGE_EVIDENCE_FILE"
DEPENDENCY_FINGERPRINT_ENV = "TRACKFLOW_PIPELINE_DEPENDENCY_FINGERPRINT"
RULES_VERSION_ENV = "TRACKFLOW_PIPELINE_RULES_VERSION"

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_CONFLICT = 3
EXIT_COMMIT_UNCERTAIN = 4


@dataclass(frozen=True)
class PipelineRuntime:
    """Explicit runtime dependencies reusable by the future CLI and HTTP trigger."""

    engine: Any
    coverage: CoverageEvidence
    dependency_fingerprint: str
    source_engine: Any | None = None
    rules_version: str = RULES_VERSION
    lease_seconds: int = 900
    trigger_type: str = "prefect"
    optional_observer: Callable[[date, UUID], None] | None = None
    optional_state_sink: Callable[[Any], None] | None = None
    accepted_run_id: UUID | None = None
    technical_coverage: TechnicalCoverageEvidence | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


@dataclass(frozen=True)
class PreparedRun:
    run_id: UUID
    lease: ConfirmedLeaseRight | None
    terminal_result: CoordinatorResult | None = None


@dataclass(frozen=True)
class WeeklyExtractionStage:
    runtime: PipelineRuntime
    extracted: WeeklyExtractionResult | None = None
    operational_audit: OperationalCoverageAudit | None = None
    failed_stage: str | None = None
    error: str | None = None


def _runtime_id(value: Any) -> str | None:
    return None if value is None else str(value)


def transformation_cache_key(_context: Any, parameters: dict[str, Any]) -> str:
    """Invalidate on week, extracted data/evidence, dependency and rule version."""
    material = (
        parameters["week_start"].isoformat(),
        repr(parameters["extracted"]),
        parameters["dependency_fingerprint"],
        parameters["rules_version"],
        parameters.get("technical_coverage_identity"),
    )
    return sha256(repr(material).encode("utf-8")).hexdigest()


@task(
    name="trackflow-relay-inventory-outbox",
    retries=2,
    retry_delay_seconds=5,
    cache_policy=NO_CACHE,
)
def relay_outbox_task(source_engine: Any, reporting_engine: Any) -> RelayResult:
    """Relay authentic movement events before taking the weekly snapshot."""
    result = relay_inventory_outbox(source_engine, reporting_engine)
    if result.failed:
        codes = ",".join(result.error_codes) or "destination_failure"
        raise RuntimeError(f"outbox_relay_incomplete:{codes}")
    return result


@task(
    name="trackflow-audit-operational-coverage",
    retries=2,
    retry_delay_seconds=5,
    cache_policy=NO_CACHE,
)
def audit_operational_coverage_task(
    source_engine: Any,
    reporting_engine: Any,
    week_start: date,
) -> OperationalCoverageAudit:
    return audit_operational_coverage(source_engine, reporting_engine, week_start)


@task(
    name="trackflow-extract-week",
    retries=2,
    retry_delay_seconds=5,
    cache_policy=NO_CACHE,
)
def extract_week_task(
    engine: Any,
    week_start: date,
    source_engine: Any | None = None,
) -> WeeklyExtractionResult:
    """Retry the read-only external snapshot; no publication occurs here."""
    if source_engine is None:
        return extract_weekly_inputs(engine, week_start)
    return extract_weekly_inputs(engine, week_start, inventory_bind=source_engine)


@task(
    name="trackflow-transform-week",
    cache_key_fn=transformation_cache_key,
    cache_expiration=timedelta(hours=1),
    persist_result=True,
)
def transform_week_task(
    extracted: WeeklyExtractionResult,
    week_start: date,
    dependency_fingerprint: str,
    rules_version: str,
    technical_coverage_identity: str | None = None,
    prepared: PreparedWeeklyEvents | None = None,
    inbound: MetricContribution | None = None,
    outbound: MetricContribution | None = None,
    stockouts: MetricContribution | None = None,
    discrepancies: MetricContribution | None = None,
) -> WeeklyTransformationResult:
    """Run the existing pure transformation with a one-hour evidence-aware cache."""
    if extracted.metadata.week_start != week_start:
        raise ValueError("extraction week does not match transformation week")
    contributions = (inbound, outbound, stockouts, discrepancies)
    if prepared is not None and all(item is not None for item in contributions):
        return compose_weekly_performance(
            prepared,
            tuple(item for item in contributions if item is not None),
        )
    if prepared is not None or any(item is not None for item in contributions):
        raise ValueError("prepared events and all KPI contributions must be supplied together")
    return transform_weekly_performance(
        extracted.events,
        commercial_order_evidence=extracted.commercial_order_evidence,
        movement_week_evidence=extracted.movement_week_evidence,
    )


@task(name="trackflow-prepare-weekly-business-events")
def prepare_weekly_events_task(
    extracted: WeeklyExtractionResult,
    week_start: date,
) -> PreparedWeeklyEvents:
    if extracted.metadata.week_start != week_start:
        raise ValueError("extraction week does not match calculation week")
    return prepare_weekly_events(
        extracted.events,
        movement_week_evidence=extracted.movement_week_evidence,
    )


@task(name="trackflow-calculate-inbound-volume")
def calculate_inbound_volume_task(prepared: PreparedWeeklyEvents) -> MetricContribution:
    return calculate_inbound_volume(prepared)


@task(name="trackflow-calculate-outbound-throughput")
def calculate_outbound_throughput_task(
    prepared: PreparedWeeklyEvents,
    extracted: WeeklyExtractionResult,
) -> MetricContribution:
    return calculate_outbound_throughput(prepared, extracted.commercial_order_evidence)


@task(name="trackflow-calculate-stockout-frequency")
def calculate_stockout_frequency_task(prepared: PreparedWeeklyEvents) -> MetricContribution:
    return calculate_stockout_frequency(prepared)


@task(name="trackflow-calculate-discrepancy-metrics")
def calculate_discrepancy_metrics_task(prepared: PreparedWeeklyEvents) -> MetricContribution:
    return calculate_discrepancy_events(prepared)


@task(name="trackflow-create-run", cache_policy=NO_CACHE)
def create_run_task(runtime: PipelineRuntime, week_start: date) -> UUID:
    """Persist the run and its Prefect correlation before external work."""
    run_id = runtime.accepted_run_id or uuid4()
    now = runtime.clock()
    with runtime.engine.connect() as connection:
        connection.begin()
        if runtime.accepted_run_id is not None:
            existing = get_run(connection, run_id)
            if (
                existing is None
                or existing.target_week_start != week_start
                or existing.status != STATUS_PENDING
            ):
                raise RuntimeError("accepted run is missing, mismatched, or no longer pending")
            correlate_pending_run(
                connection,
                run_id,
                prefect_flow_run_id=_runtime_id(flow_run.id) or "unavailable-flow-run",
                prefect_task_run_id=_runtime_id(task_run.id) or "unavailable-task-run",
            )
            connection.commit()
            return run_id
        create_run(
            connection,
            target_week_start=week_start,
            trigger_type=runtime.trigger_type,
            run_id=run_id,
            started_at=now,
            stage_reached="prefect",
            prefect_flow_run_id=_runtime_id(flow_run.id),
            prefect_task_run_id=_runtime_id(task_run.id),
        )
        connection.commit()
    return run_id


@task(name="trackflow-acquire-lease", cache_policy=NO_CACHE)
def acquire_lease_task(runtime: PipelineRuntime, week_start: date, run_id: UUID) -> PreparedRun:
    """Acquire a lease only after extraction and transformation have completed."""
    now = runtime.clock()
    with runtime.engine.connect() as connection:
        connection.begin()
        decision = acquire_week_lease(
            connection, week_start, run_id, lease_seconds=runtime.lease_seconds
        )
        if decision.status not in {"acquired", "recovered"}:
            status = (
                STATUS_SKIPPED_IN_PROGRESS
                if decision.status in {"occupied", "already_held"}
                else STATUS_SUPERSEDED
            )
            record_lease_run_status(
                connection, run_id, status=status, finished_at=now, reason=decision.status
            )
            connection.commit()
            return PreparedRun(
                run_id,
                None,
                CoordinatorResult(week_start, run_id, status, errors=(decision.status,)),
            )
        connection.commit()
        return PreparedRun(
            run_id,
            ConfirmedLeaseRight(
                week_start, run_id, decision.fencing_token, acquisition_committed=True
            ),
        )


@task(name="trackflow-fail-run", cache_policy=NO_CACHE)
def fail_run_task(
    runtime: PipelineRuntime,
    week_start: date,
    run_id: UUID,
    stage: str,
    error: str,
    records_processed: int | None = None,
) -> CoordinatorResult:
    """Persist a pre-publication failure; no lease exists at these stages."""
    with runtime.engine.connect() as connection:
        connection.begin()
        update_run(
            connection,
            run_id,
            expected_status=STATUS_PENDING,
            status=STATUS_FAILED_RETRYABLE,
            finished_at=runtime.clock(),
            stage_reached=stage,
            records_processed=records_processed,
            error_code=f"{stage}_failed",
            error_message=error[:500],
            prefect_flow_run_id=_runtime_id(flow_run.id),
            prefect_task_run_id=_runtime_id(task_run.id),
        )
        connection.commit()
    return CoordinatorResult(
        week_start,
        run_id,
        STATUS_FAILED_RETRYABLE,
        errors=(f"{stage}_failed",),
    )


@task(name="trackflow-quality-gate", cache_policy=NO_CACHE)
def quality_gate_task(
    runtime: PipelineRuntime,
    week_start: date,
    run_id: UUID,
    extracted: WeeklyExtractionResult,
    transformed: WeeklyTransformationResult,
    records_processed: int,
) -> CoordinatorResult | None:
    """Reuse coordinator validation before acquiring a lease."""
    technical_coverage_error: ValueError | None = None
    try:
        transformed = apply_verified_technical_coverage(
            extracted,
            transformed,
            runtime.technical_coverage,
            week_start=week_start,
            dependency_fingerprint=runtime.dependency_fingerprint,
            rules_version=runtime.rules_version,
            engine=runtime.engine,
        )
    except ValueError as exc:
        # A supplied attestation is untrusted input. Persist a terminal quality
        # block instead of abandoning the durable run in pending state.
        technical_coverage_error = exc
    now = runtime.clock()
    issues = (
        ("technical_coverage_invalid",)
        if technical_coverage_error is not None
        else _pre_write_issues(
            week_start,
            transformed.aggregates,
            transformed.quality,
            runtime.coverage,
            runtime.dependency_fingerprint,
            runtime.rules_version,
            now,
        )
    )
    if not issues:
        return None
    with runtime.engine.connect() as connection:
        connection.begin()
        update_run(
            connection,
            run_id,
            expected_status=STATUS_PENDING,
            status=STATUS_BLOCKED_QUALITY,
            finished_at=now,
            stage_reached="quality",
            records_processed=records_processed,
            aggregates_calculated=len(transformed.aggregates),
            block_reason=issues[0],
            error_message=(
                "technical_coverage_invalid"
                if technical_coverage_error is not None
                else ",".join(issues)[:500]
            ),
            prefect_flow_run_id=_runtime_id(flow_run.id),
            prefect_task_run_id=_runtime_id(task_run.id),
        )
        connection.commit()
    return CoordinatorResult(week_start, run_id, STATUS_BLOCKED_QUALITY, errors=issues)


@task(name="trackflow-publish-week", cache_policy=NO_CACHE)
def publish_week_task(
    runtime: PipelineRuntime,
    week_start: date,
    prepared: PreparedRun,
    transformed: WeeklyTransformationResult,
    records_processed: int,
) -> CoordinatorResult:
    """Perform the existing atomic coordinator path exactly once; never retry commit."""
    if prepared.terminal_result is not None:
        return prepared.terminal_result
    if prepared.lease is None:
        raise RuntimeError("a durable lease is required for publication")
    with runtime.engine.connect() as business, runtime.engine.connect() as operational:
        business.begin()
        operational.begin()
        result = publish_weekly_business_report(
            week_start=week_start,
            run_id=prepared.run_id,
            aggregates=transformed.aggregates,
            quality=transformed.quality,
            coverage=runtime.coverage,
            dependency_fingerprint=runtime.dependency_fingerprint,
            rules_version=runtime.rules_version,
            lease=prepared.lease,
            business=business,
            operational=operational,
            now=runtime.clock(),
            records_processed=records_processed,
        )
    if result.status not in SAFE_TERMINAL_STATUSES and result.status != "commit_uncertain":
        return result
    return result


@task(name="trackflow-optional-observer", cache_policy=NO_CACHE)
def optional_observer_task(runtime: PipelineRuntime, week_start: date, run_id: UUID) -> None:
    """Non-critical observability hook; callers deliberately consume its State."""
    if runtime.optional_observer is not None:
        runtime.optional_observer(week_start, run_id)


@flow(name="trackflow-extract-weekly-warehouse-events")
def extract_weekly_warehouse_events_flow(
    runtime: PipelineRuntime,
    week_start: date,
) -> WeeklyExtractionStage:
    """Relay pending outbox rows, audit coverage, then take a read-only snapshot."""
    operational_audit: OperationalCoverageAudit | None = None
    if runtime.source_engine is not None and runtime.source_engine is not runtime.engine:
        relay_state = relay_outbox_task(
            runtime.source_engine, runtime.engine, return_state=True
        )
        if relay_state.is_failed():
            return WeeklyExtractionStage(
                runtime,
                failed_stage="relay",
                error=str(relay_state.result(raise_on_failure=False)),
            )
        audit_state = audit_operational_coverage_task(
            runtime.source_engine, runtime.engine, week_start, return_state=True
        )
        if audit_state.is_failed():
            return WeeklyExtractionStage(
                runtime,
                failed_stage="coverage_audit",
                error=str(audit_state.result(raise_on_failure=False)),
            )
        operational_audit = audit_state.result()
        runtime = replace(runtime, coverage=operational_audit.evidence)
    extracted_state = extract_week_task(
        runtime.engine,
        week_start,
        runtime.source_engine,
        return_state=True,
    )
    if extracted_state.is_failed():
        return WeeklyExtractionStage(
            runtime,
            failed_stage="extract",
            error=str(extracted_state.result(raise_on_failure=False)),
        )
    return WeeklyExtractionStage(runtime, extracted_state.result(), operational_audit)


@flow(name="trackflow-calculate-warehouse-client-indicators")
def calculate_warehouse_client_indicators_flow(
    runtime: PipelineRuntime,
    week_start: date,
    extracted: WeeklyExtractionResult,
    operational_audit: OperationalCoverageAudit | None = None,
) -> WeeklyTransformationResult:
    """Calculate the four official KPI counters through independent tasks."""
    prepared = prepare_weekly_events_task(extracted, week_start)
    inbound = calculate_inbound_volume_task(prepared)
    outbound = calculate_outbound_throughput_task(prepared, extracted)
    stockouts = calculate_stockout_frequency_task(prepared)
    discrepancies = calculate_discrepancy_metrics_task(prepared)
    transformed_state = transform_week_task(
        extracted,
        week_start,
        runtime.dependency_fingerprint,
        runtime.rules_version,
        None if runtime.technical_coverage is None else runtime.technical_coverage.identity,
        prepared,
        inbound,
        outbound,
        stockouts,
        discrepancies,
        return_state=True,
    )
    if transformed_state.is_failed():
        raise RuntimeError(str(transformed_state.result(raise_on_failure=False)))
    transformed = transformed_state.result()
    if operational_audit is not None:
        transformed = apply_operational_coverage(
            transformed, operational_audit, extracted
        )
    return transformed


@flow(name="trackflow-publish-weekly-business-report")
def publish_weekly_business_report_flow(
    runtime: PipelineRuntime,
    week_start: date,
    run_id: UUID,
    extracted: WeeklyExtractionResult,
    transformed: WeeklyTransformationResult,
) -> CoordinatorResult:
    """Apply quality, lease and atomic publication controls as one domain subflow."""
    blocked = quality_gate_task(
        runtime, week_start, run_id, extracted, transformed, extracted.metadata.event_count
    )
    if blocked is not None:
        return blocked
    transformed = apply_verified_technical_coverage(
        extracted,
        transformed,
        runtime.technical_coverage,
        week_start=week_start,
        dependency_fingerprint=runtime.dependency_fingerprint,
        rules_version=runtime.rules_version,
        engine=runtime.engine,
    )
    prepared = acquire_lease_task(runtime, week_start, run_id)
    if prepared.terminal_result is not None:
        return prepared.terminal_result
    return publish_week_task(
        runtime, week_start, prepared, transformed, extracted.metadata.event_count
    )


@flow(name="trackflow-optional-weekly-observer")
def optional_weekly_observer_flow(
    runtime: PipelineRuntime,
    week_start: date,
    run_id: UUID,
) -> None:
    optional_observer_task(runtime, week_start, run_id)


def _run_optional_observer(runtime: PipelineRuntime, week_start: date, run_id: UUID) -> Any:
    """Expose the optional subflow State while never promoting its failure."""
    state = optional_weekly_observer_flow(runtime, week_start, run_id, return_state=True)
    if runtime.optional_state_sink is not None:
        runtime.optional_state_sink(state)
    return state


@flow(name="trackflow-weekly-performance")
def weekly_performance_flow(runtime: PipelineRuntime, week_start: date) -> CoordinatorResult:
    """Run extraction, calculation and publication subflows sequentially."""
    run_id = create_run_task(runtime, week_start)
    extraction_state = extract_weekly_warehouse_events_flow(
        runtime, week_start, return_state=True
    )
    if extraction_state.is_failed():
        result = fail_run_task(
            runtime,
            week_start,
            run_id,
            "extract",
            str(extraction_state.result(raise_on_failure=False)),
        )
        _run_optional_observer(runtime, week_start, run_id)
        return result
    stage = extraction_state.result()
    if stage.failed_stage is not None or stage.extracted is None:
        result = fail_run_task(
            stage.runtime,
            week_start,
            run_id,
            stage.failed_stage or "extract",
            stage.error or "weekly extraction did not return data",
        )
        _run_optional_observer(stage.runtime, week_start, run_id)
        return result
    calculation_state = calculate_warehouse_client_indicators_flow(
        stage.runtime,
        week_start,
        stage.extracted,
        stage.operational_audit,
        return_state=True,
    )
    if calculation_state.is_failed():
        result = fail_run_task(
            stage.runtime,
            week_start,
            run_id,
            "transform",
            str(calculation_state.result(raise_on_failure=False)),
            stage.extracted.metadata.event_count,
        )
        _run_optional_observer(stage.runtime, week_start, run_id)
        return result
    publication_state = publish_weekly_business_report_flow(
        stage.runtime,
        week_start,
        run_id,
        stage.extracted,
        calculation_state.result(),
        return_state=True,
    )
    if publication_state.is_failed():
        _run_optional_observer(stage.runtime, week_start, run_id)
        raise RuntimeError(str(publication_state.result(raise_on_failure=False)))
    result = publication_state.result()
    _run_optional_observer(stage.runtime, week_start, run_id)
    return result


def run_weekly_pipeline(runtime: PipelineRuntime, week_start: date) -> CoordinatorResult:
    """Stable runner entry point for the F4 CLI and F5 HTTP scheduler."""
    return weekly_performance_flow(runtime, week_start)


class CliConfigurationError(RuntimeError):
    """Safe CLI configuration failure whose message never contains a secret."""


def latest_closed_iso_week(now: datetime) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(timezone.utc).date()
    current_monday = today - timedelta(days=today.weekday())
    return current_monday - timedelta(days=7)


def parse_closed_week(value: str | None, *, now: datetime) -> date:
    current_monday = latest_closed_iso_week(now) + timedelta(days=7)
    if value is None:
        return current_monday - timedelta(days=7)
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("week_start must use YYYY-MM-DD") from exc
    if selected.weekday() != 0:
        raise ValueError("week_start must be an ISO Monday")
    if selected >= current_monday:
        raise ValueError("week_start must identify a fully closed ISO UTC week")
    return selected


def _load_coverage_evidence(environment: dict[str, str]) -> CoverageEvidence:
    evidence_path = environment.get(COVERAGE_EVIDENCE_FILE_ENV)
    if not evidence_path:
        raise CliConfigurationError(
            f"{COVERAGE_EVIDENCE_FILE_ENV} is required; coverage is not inferred"
        )
    try:
        payload = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        confirmed = frozenset(payload["confirmed_event_types"])
        pairs = tuple(tuple(pair) for pair in payload["dispatch_order_ids"])
        lookup_complete = payload["lookup_complete"]
        source = payload["source"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CliConfigurationError("coverage evidence file is unavailable or invalid") from exc
    return CoverageEvidence(confirmed, pairs, lookup_complete, source)


def _load_runtime_contract(
    environment: dict[str, str],
) -> tuple[CoverageEvidence, str, str]:
    """Load the conservative configuration shared by CLI and HTTP runtimes."""
    fingerprint = environment.get(DEPENDENCY_FINGERPRINT_ENV, "").strip()
    if not fingerprint:
        raise CliConfigurationError(f"{DEPENDENCY_FINGERPRINT_ENV} is required")
    rules_version = environment.get(RULES_VERSION_ENV, "").strip()
    if not rules_version:
        raise CliConfigurationError(f"{RULES_VERSION_ENV} is required")
    return _load_coverage_evidence(environment), fingerprint, rules_version


def build_http_runtime(
    engine: Any,
    environment: dict[str, str] | None = None,
    *,
    source_engine: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PipelineRuntime:
    """Build an HTTP runtime around the reporting engine already validated by FastAPI."""
    values = dict(os.environ if environment is None else environment)
    if source_engine is not None and source_engine is not engine:
        fingerprint = values.get(DEPENDENCY_FINGERPRINT_ENV, "").strip()
        rules_version = values.get(RULES_VERSION_ENV, "").strip()
        if not fingerprint:
            raise CliConfigurationError(f"{DEPENDENCY_FINGERPRINT_ENV} is required")
        if not rules_version:
            raise CliConfigurationError(f"{RULES_VERSION_ENV} is required")
        coverage = CoverageEvidence(
            frozenset(), (), False, OPERATIONAL_COVERAGE_SOURCE
        )
    else:
        coverage, fingerprint, rules_version = _load_runtime_contract(values)
    return PipelineRuntime(
        engine=engine,
        coverage=coverage,
        dependency_fingerprint=fingerprint,
        source_engine=source_engine,
        rules_version=rules_version,
        trigger_type="http",
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )


def build_cli_runtime(
    environment: dict[str, str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> PipelineRuntime:
    """Build conservative runtime configuration without exposing connection data."""
    values = dict(os.environ if environment is None else environment)
    database_url = values.get(PIPELINE_DATABASE_URL_ENV)
    if not database_url:
        raise CliConfigurationError(f"{PIPELINE_DATABASE_URL_ENV} is required")
    if database_url == values.get("DATABASE_URL"):
        raise CliConfigurationError("refusing to reuse DATABASE_URL")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise CliConfigurationError("the dedicated pipeline database must be PostgreSQL")
    source_url = values.get(PIPELINE_SOURCE_DATABASE_URL_ENV)
    if source_url is not None:
        if source_url == database_url:
            raise CliConfigurationError("source and reporting databases must be distinct")
        if not source_url.startswith(("postgresql://", "postgresql+psycopg2://")):
            raise CliConfigurationError("the pipeline source database must be PostgreSQL")
    source_engine = (
        None
        if source_url is None
        else create_engine(sqlalchemy_postgresql_url(source_url), pool_pre_ping=True)
    )
    return replace(
        build_http_runtime(
            create_engine(sqlalchemy_postgresql_url(database_url), pool_pre_ping=True),
            values,
            source_engine=source_engine,
            clock=clock,
        ),
        trigger_type="cli",
    )


def _exit_code(status: str) -> int:
    if status in {"succeeded", "succeeded_noop"}:
        return EXIT_SUCCESS
    if status == "blocked_quality":
        return EXIT_BLOCKED
    if status in {"superseded", "skipped_in_progress"}:
        return EXIT_CONFLICT
    if status == "commit_uncertain":
        return EXIT_COMMIT_UNCERTAIN
    return EXIT_ERROR


def _result_payload(result: CoordinatorResult) -> dict[str, Any]:
    limitations = ["production_coverage_not_certified"]
    if result.evidence_inconclusive:
        limitations.append("commit_evidence_inconclusive")
    return {
        "run_id": None if result.run_id is None else str(result.run_id),
        "week_start": result.week_start.isoformat(),
        "status": result.status,
        "counts": {
            "inserted": result.inserted,
            "updated": result.updated,
            "unchanged": result.unchanged,
        },
        "limitations": limitations,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipelines.pipeline",
        description="Run TrackFlow's weekly Prefect pipeline for one closed ISO UTC week.",
    )
    parser.add_argument(
        "--week-start",
        metavar="YYYY-MM-DD",
        help="ISO Monday of a fully closed UTC week; defaults to the latest closed week.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    runtime_factory: Callable[[], PipelineRuntime] = build_cli_runtime,
    runner: Callable[[PipelineRuntime, date], CoordinatorResult] = run_weekly_pipeline,
) -> int:
    args = _parser().parse_args(argv)
    clock = now or datetime.now(timezone.utc)
    try:
        week_start = parse_closed_week(args.week_start, now=clock)
    except ValueError as exc:
        print(json.dumps({"status": "invalid_request", "error": str(exc)}))
        return EXIT_ERROR
    try:
        runtime = runtime_factory()
        result = runner(runtime, week_start)
    except CliConfigurationError as exc:
        print(json.dumps({"status": "blocked_configuration", "error": str(exc)}))
        return EXIT_BLOCKED
    except Exception:
        print(json.dumps({"status": "error", "error": "pipeline_execution_failed"}))
        return EXIT_ERROR
    print(json.dumps(_result_payload(result), sort_keys=True))
    return _exit_code(result.status)


if __name__ == "__main__":
    raise SystemExit(main())
