from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

import pipelines.pipeline as pipeline
from pipelines.coordinator import CoordinatorResult

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def test_latest_closed_week_and_explicit_closed_monday():
    assert pipeline.latest_closed_iso_week(NOW) == date(2026, 10, 19)
    assert pipeline.parse_closed_week(None, now=NOW) == date(2026, 10, 19)
    assert pipeline.parse_closed_week("2026-10-12", now=NOW) == date(2026, 10, 12)


@pytest.mark.parametrize(
    "value,message",
    [
        ("not-a-date", "YYYY-MM-DD"),
        ("2026-10-20", "Monday"),
        ("2026-10-26", "fully closed"),
        ("2026-11-02", "fully closed"),
    ],
)
def test_invalid_week_is_rejected_before_runtime(capsys, value, message):
    called = False

    def runtime_factory():
        nonlocal called
        called = True
        raise AssertionError("must not connect")

    code = pipeline.main(["--week-start", value], now=NOW, runtime_factory=runtime_factory)
    payload = json.loads(capsys.readouterr().out)
    assert code == pipeline.EXIT_ERROR
    assert message in payload["error"]
    assert called is False


def test_help_documents_module_command_and_only_safe_option(capsys):
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["--help"], now=NOW)
    output = capsys.readouterr().out
    assert raised.value.code == 0
    assert "python -m pipelines.pipeline" in output
    assert "--week-start" in output
    assert "bypass" not in output.lower()


@pytest.mark.parametrize(
    "status,code",
    [
        ("succeeded", pipeline.EXIT_SUCCESS),
        ("succeeded_noop", pipeline.EXIT_SUCCESS),
        ("blocked_quality", pipeline.EXIT_BLOCKED),
        ("superseded", pipeline.EXIT_CONFLICT),
        ("skipped_in_progress", pipeline.EXIT_CONFLICT),
        ("commit_uncertain", pipeline.EXIT_COMMIT_UNCERTAIN),
        ("failed_retryable", pipeline.EXIT_ERROR),
    ],
)
def test_main_delegates_and_maps_structured_safe_results(capsys, status, code):
    observed = {}
    run_id = uuid4()
    result = CoordinatorResult(
        date(2026, 10, 19),
        run_id,
        status,
        inserted=1,
        updated=2,
        unchanged=3,
    )

    def runner(runtime, week_start):
        observed.update(runtime=runtime, week_start=week_start)
        return result

    runtime = object()
    actual = pipeline.main(
        [], now=NOW, runtime_factory=lambda: runtime, runner=runner
    )
    payload = json.loads(capsys.readouterr().out)
    assert actual == code
    assert observed == {"runtime": runtime, "week_start": date(2026, 10, 19)}
    assert payload == {
        "counts": {"inserted": 1, "unchanged": 3, "updated": 2},
        "limitations": ["production_coverage_not_certified"],
        "run_id": str(run_id),
        "status": status,
        "week_start": "2026-10-19",
    }


def test_configuration_blocks_without_leaking_database_url(capsys):
    secret = "postgresql+psycopg2://user:secret@example.invalid/db"
    code = pipeline.main(
        [],
        now=NOW,
        runtime_factory=lambda: pipeline.build_cli_runtime(
            {pipeline.PIPELINE_DATABASE_URL_ENV: secret}
        ),
    )
    output = capsys.readouterr().out
    assert code == pipeline.EXIT_BLOCKED
    assert secret not in output and "user:secret" not in output
    assert pipeline.DEPENDENCY_FINGERPRINT_ENV in output


def test_build_runtime_rejects_database_url_reuse_before_engine(monkeypatch):
    secret = "postgresql://user:secret@example.invalid/db"
    monkeypatch.setattr(
        pipeline,
        "create_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no engine")),
    )
    with pytest.raises(pipeline.CliConfigurationError, match="refusing"):
        pipeline.build_cli_runtime(
            {
                pipeline.PIPELINE_DATABASE_URL_ENV: secret,
                "DATABASE_URL": secret,
            }
        )


def test_normal_cli_configuration_cannot_activate_technical_coverage(monkeypatch, tmp_path):
    evidence = tmp_path / "coverage.json"
    evidence.write_text(
        json.dumps({
            "confirmed_event_types": [
                "stock_threshold_triggered",
                "inventory_discrepancy_detected",
            ],
            "dispatch_order_ids": [["movement", "order"]],
            "lookup_complete": True,
            "source": "synthetic_fixture",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "create_engine", lambda *_args, **_kwargs: object())
    configured = pipeline.build_cli_runtime({
        pipeline.PIPELINE_DATABASE_URL_ENV: "postgresql://isolated.invalid/test",
        pipeline.DEPENDENCY_FINGERPRINT_ENV: "telemetry=a;movements=b;stockexit=c;rules=d",
        pipeline.RULES_VERSION_ENV: "rules-v1",
        pipeline.COVERAGE_EVIDENCE_FILE_ENV: str(evidence),
    })
    assert configured.technical_coverage is None


def test_cli_optional_source_builds_distinct_dual_engine_runtime(monkeypatch, tmp_path):
    engines = []

    def create(url, **_kwargs):
        engine = object()
        engines.append((url, engine))
        return engine

    monkeypatch.setattr(pipeline, "create_engine", create)
    configured = pipeline.build_cli_runtime({
        pipeline.PIPELINE_DATABASE_URL_ENV: "postgresql://reporting.invalid/db",
        pipeline.PIPELINE_SOURCE_DATABASE_URL_ENV: "postgresql://source.invalid/db",
        pipeline.DEPENDENCY_FINGERPRINT_ENV: "telemetry=a;movements=b;stockexit=c;rules=d",
        pipeline.RULES_VERSION_ENV: "rules-v1",
    })

    assert configured.engine is engines[1][1]
    assert configured.source_engine is engines[0][1]
    assert [url for url, _engine in engines] == [
        "postgresql://source.invalid/db",
        "postgresql://reporting.invalid/db",
    ]
    assert configured.coverage.source == "operational_checkpoints"
    assert configured.coverage.lookup_complete is False


def test_cli_rejects_same_source_and_reporting_url_before_engine(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("engine must not be created"),
    )
    with pytest.raises(pipeline.CliConfigurationError, match="must be distinct"):
        pipeline.build_cli_runtime({
            pipeline.PIPELINE_DATABASE_URL_ENV: "postgresql://same.invalid/db",
            pipeline.PIPELINE_SOURCE_DATABASE_URL_ENV: "postgresql://same.invalid/db",
        })


def test_http_runtime_reuses_engine_and_keeps_conservative_configuration(tmp_path):
    evidence = tmp_path / "coverage.json"
    evidence.write_text(
        json.dumps({
            "confirmed_event_types": [
                "stock_threshold_triggered",
                "inventory_discrepancy_detected",
            ],
            "dispatch_order_ids": [["movement", "order"]],
            "lookup_complete": True,
            "source": "synthetic_fixture",
        }),
        encoding="utf-8",
    )
    engine = object()
    configured = pipeline.build_http_runtime(engine, {
        pipeline.DEPENDENCY_FINGERPRINT_ENV: "telemetry=a;movements=b;stockexit=c;rules=d",
        pipeline.RULES_VERSION_ENV: "rules-v1",
        pipeline.COVERAGE_EVIDENCE_FILE_ENV: str(evidence),
    })
    assert configured.engine is engine
    assert configured.trigger_type == "http"
    assert configured.technical_coverage is None


def test_http_dual_runtime_uses_operational_audit_without_json_file():
    reporting = object()
    source = object()
    configured = pipeline.build_http_runtime(
        reporting,
        {
            pipeline.DEPENDENCY_FINGERPRINT_ENV: "telemetry=a;movements=b;stockexit=c;rules=d",
            pipeline.RULES_VERSION_ENV: "rules-v1",
        },
        source_engine=source,
    )
    assert configured.engine is reporting
    assert configured.source_engine is source
    assert configured.coverage.source == "operational_checkpoints"


@pytest.mark.parametrize(
    "missing",
    [
        pipeline.DEPENDENCY_FINGERPRINT_ENV,
        pipeline.RULES_VERSION_ENV,
        pipeline.COVERAGE_EVIDENCE_FILE_ENV,
    ],
)
def test_http_runtime_rejects_incomplete_conservative_configuration(tmp_path, missing):
    evidence = tmp_path / "coverage.json"
    evidence.write_text(
        json.dumps({
            "confirmed_event_types": [],
            "dispatch_order_ids": [],
            "lookup_complete": False,
            "source": "synthetic_fixture",
        }),
        encoding="utf-8",
    )
    environment = {
        pipeline.DEPENDENCY_FINGERPRINT_ENV: "fingerprint",
        pipeline.RULES_VERSION_ENV: "rules-v1",
        pipeline.COVERAGE_EVIDENCE_FILE_ENV: str(evidence),
    }
    environment.pop(missing)
    with pytest.raises(pipeline.CliConfigurationError, match=missing):
        pipeline.build_http_runtime(object(), environment)
