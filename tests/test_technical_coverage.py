from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import create_engine

from pipelines.publication import blocking_quality_issues
from pipelines.technical_coverage import (
    ControlledTestDatabaseCapability,
    TechnicalCoverageEvidence,
    apply_verified_technical_coverage,
    authorize_controlled_test_database,
    verify_controlled_test_coverage,
)
from pipelines.weekly_performance import QualityIssue, QualityReport, WeeklyTransformationResult
from tests.test_pipeline_postgres_integration import _aggregate


WEEK = date(2024, 1, 1)


def _quality(*, issues=(), accepted=("event-1",), coverage=("pending",)):
    return QualityReport(
        accepted_event_ids=accepted,
        rejected_event_ids=(),
        duplicate_event_ids_consolidated=(),
        duplicate_movement_keys_consolidated=(),
        duplicate_movement_event_ids_consolidated=(),
        conflict_movement_keys=(),
        issues=issues,
        cross_week_verification_pending=(),
        coverage_pending=coverage,
    )


def test_absent_technical_evidence_keeps_default_block():
    transformed = WeeklyTransformationResult((), _quality())
    assert apply_verified_technical_coverage(
        None,
        transformed,
        None,
        week_start=WEEK,
        dependency_fingerprint="fingerprint",
        rules_version="rules",
    ) is transformed
    assert {issue.code for issue in blocking_quality_issues(transformed.quality)} == {
        "coverage_insufficient"
    }


def test_forged_or_mismatched_attestation_is_rejected():
    transformed = WeeklyTransformationResult((), _quality())
    capability = ControlledTestDatabaseCapability(1, "container", "server", 1, object())
    forged = TechnicalCoverageEvidence(
        week_start=WEEK,
        dependency_fingerprint="fingerprint",
        rules_version="rules",
        event_ids=("event-1",),
        event_types=frozenset(),
        outbound_order_client_pairs=(("order", "client"),),
        aggregate_keys=(),
        provenance="isolated_postgresql_controlled_test_fixture",
        scope="technical_test_only_no_business_certification",
        identity="fake",
        content_fingerprint="fake-content",
        server_identity="fake-server",
        _attestation=object(),
        _database_capability=capability,
    )
    with pytest.raises(ValueError, match="technical coverage"):
        apply_verified_technical_coverage(
            None,
            transformed,
            forged,
            week_start=WEEK,
            dependency_fingerprint="fingerprint",
            rules_version="rules",
        )
    assert forged.production_certified is False


def test_unauthorized_database_identity_cannot_attest():
    class Result:
        def mappings(self):
            return self

        def one(self):
            return {"database_name": "production", "user_name": "app"}

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, *_args): return Result()

    class Engine:
        def connect(self): return Connection()

    engine = Engine()
    with pytest.raises(ValueError, match="capability"):
        capability = ControlledTestDatabaseCapability(
            id(engine), "container", "server", 1, object()
        )
        verify_controlled_test_coverage(
            engine, None, None,
            week_start=WEEK,
            dependency_fingerprint="fingerprint",
            rules_version="rules",
            database_capability=capability,
        )


def test_different_container_name_is_rejected_before_capability_issue(monkeypatch):
    url = "postgresql+psycopg2://trackflow_test:local-only@127.0.0.1:51323/trackflow_test"
    monkeypatch.setenv("TRACKFLOW_PUBLICATION_TEST_DATABASE_URL", url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    engine = create_engine(url)
    try:
        with pytest.raises(ValueError, match="container is not authorized"):
            authorize_controlled_test_database(engine, container_name="some-other-container")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "code",
    ["invalid_row_id", "event_validation_failed", "telemetry_row_id_conflict"],
)
def test_partial_rejections_are_blocking_quality_codes(code):
    quality = _quality(issues=(QualityIssue(code, "invalid fixture row"),), coverage=())
    assert [issue.code for issue in blocking_quality_issues(quality)] == ["quality_blocked"]


def test_conflicting_quality_cannot_be_treated_as_technical_coverage():
    transformed = WeeklyTransformationResult(
        (),
        replace(_quality(), conflict_movement_keys=(("outbound_order_created", "1"),)),
    )
    assert "identity_conflict" in {
        issue.code for issue in blocking_quality_issues(transformed.quality)
    }


def test_reporting_fallback_orders_by_greatest_valid_week():
    from services.reporting.store import LATEST_VALID_WEEK_SQL

    sql = str(LATEST_VALID_WEEK_SQL).upper()
    assert "ORDER BY WEEK_START DESC" in sql
    assert "ORDER BY LAST_VALID_PUBLISHED_AT" not in sql
