from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import nightly_export


TARGET = date(2026, 10, 30)
NOW = datetime(2026, 10, 31, 0, 5, tzinfo=timezone.utc)


def telemetry_row(**changes):
    row = {
        "id": uuid4(),
        "timestamp": datetime(2026, 10, 30, 23, 59, 1, 123, tzinfo=timezone.utc),
        "service": "api",
        "event_type": "api_request_failed",
        "level": "error",
        "value": Decimal("1.2300"),
        "message": "controlled, message\nwith newline",
        "tags": {"z": 2, "a": "ñ"},
    }
    row.update(changes)
    return row


def test_target_date_is_strict_and_defaults_to_yesterday_utc():
    assert nightly_export.resolve_target_date(None, now=NOW) == TARGET
    assert nightly_export.resolve_target_date("2024-02-29") == date(2024, 2, 29)
    for invalid in ("2026-1-02", " 2026-01-02", "2026-01-02 ", "2026-02-30"):
        with pytest.raises(nightly_export.NightlyConfigurationError):
            nightly_export.resolve_target_date(invalid)


def test_documented_invocation_uses_supported_module_contract():
    readme = Path("scripts/README.md").read_text(encoding="utf-8")
    assert "python -m scripts.nightly_export" in readme
    assert "python scripts/nightly_export.py" not in readme


def test_csv_with_zero_rows_has_exact_header(tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    nightly_export.publish_csv_rows([], final, TARGET)

    assert final.read_text(encoding="utf-8").splitlines() == [
        ",".join(nightly_export.CSV_COLUMNS)
    ]
    assert nightly_export.validate_existing_csv(final, TARGET) == 0


def test_csv_serialization_is_stable_for_utc_decimal_json_and_quoting(tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    nightly_export.publish_csv_rows([telemetry_row()], final, TARGET)

    with final.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == nightly_export.CSV_COLUMNS
    assert rows[0]["timestamp"] == "2026-10-30T23:59:01.000123Z"
    assert rows[0]["value"] == "1.2300"
    assert rows[0]["message"] == "controlled, message\nwith newline"
    assert rows[0]["tags"] == '{"a":"ñ","z":2}'
    assert nightly_export.validate_existing_csv(final, TARGET) == 1


@pytest.mark.parametrize(
    ("tags", "serialized"),
    [
        (None, "null"),
        ([{"z": 2, "a": 1}], '[{"a":1,"z":2}]'),
    ],
)
def test_nullable_and_non_object_jsonb_round_trip(tags, serialized, tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    nightly_export.publish_csv_rows(
        [telemetry_row(tags=tags)],
        final,
        TARGET,
    )

    with final.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["tags"] == serialized
    assert nightly_export.validate_existing_csv(final, TARGET) == 1


def test_valid_existing_csv_is_reused_without_consuming_new_rows(tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    nightly_export.publish_csv_rows([telemetry_row()], final, TARGET)
    original = final.read_bytes()

    def unexpected_rows():
        raise AssertionError("rows must not be read")
        yield

    assert nightly_export.publish_csv_rows(unexpected_rows(), final, TARGET) == final
    assert final.read_bytes() == original


def test_corrupt_existing_csv_fails_without_overwrite(tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    corrupt = b"wrong,header\nvalue,row\n"
    final.write_bytes(corrupt)

    with pytest.raises(nightly_export.CsvArtifactError):
        nightly_export.publish_csv_rows([telemetry_row()], final, TARGET)
    assert final.read_bytes() == corrupt


def test_existing_csv_rejects_unordered_rows(tmp_path):
    final = tmp_path / "telemetry_2026-10-30.csv"
    later = telemetry_row(timestamp=datetime(2026, 10, 30, 2, tzinfo=timezone.utc))
    earlier = telemetry_row(timestamp=datetime(2026, 10, 30, 1, tzinfo=timezone.utc))
    with final.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=nightly_export.CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(nightly_export.serialize_telemetry_row(later, TARGET))
        writer.writerow(nightly_export.serialize_telemetry_row(earlier, TARGET))

    with pytest.raises(nightly_export.CsvArtifactError, match="ordered"):
        nightly_export.validate_existing_csv(final, TARGET)


def test_publication_uses_same_directory_hard_link_and_leaves_no_temp(
    tmp_path, monkeypatch
):
    final = tmp_path / "telemetry_2026-10-30.csv"
    observed = {}
    real_link = os.link

    def link(source, destination):
        observed.update(source=Path(source), destination=Path(destination))
        return real_link(source, destination)

    monkeypatch.setattr(nightly_export.os, "link", link)
    nightly_export.publish_csv_rows([telemetry_row()], final, TARGET)

    assert observed["source"].parent == final.parent
    assert observed["destination"] == final
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("return_code", [1, 2, 3, 4, 17])
def test_pipeline_nonzero_exit_codes_fail_and_four_is_uncertain(return_code):
    commands = []

    class Process:
        def wait(self, **_kwargs):
            return return_code

    with pytest.raises(nightly_export.PipelineProcessError) as raised:
        nightly_export.run_pipeline(
            popen_factory=lambda command, **_kwargs: commands.append(command) or Process()
        )
    assert commands == [[sys.executable, "-m", "pipelines.pipeline"]]
    if return_code == 4:
        assert "uncertain" in str(raised.value)


def test_pipeline_zero_is_success_and_command_has_no_week_start():
    observed = {}

    class Process:
        def wait(self, **_kwargs):
            return 0

    def popen(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return Process()

    nightly_export.run_pipeline(popen_factory=popen)
    assert observed["command"] == [sys.executable, "-m", "pipelines.pipeline"]
    assert "--week-start" not in observed["command"]
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    assert observed["kwargs"]["start_new_session"] is (os.name == "posix")
    if os.name == "posix":
        assert observed["kwargs"]["preexec_fn"] is nightly_export._unblock_termination_signals_in_child
    else:
        assert "preexec_fn" not in observed["kwargs"]
    assert "stdout" not in observed["kwargs"]
    assert "stderr" not in observed["kwargs"]


def test_finished_parent_with_live_group_requires_confirmed_cleanup(monkeypatch):
    process = SimpleNamespace(pid=43210, wait=lambda **_kwargs: 0)
    monkeypatch.setattr(nightly_export.os, "name", "posix")
    monkeypatch.setattr(nightly_export, "_process_group_exists", lambda _pid: True)
    monkeypatch.setattr(
        nightly_export,
        "_stop_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            nightly_export.ManualReconciliationRequired(
                "pipeline process group is still active; manual reconciliation required"
            )
        ),
    )

    with pytest.raises(nightly_export.ManualReconciliationRequired):
        nightly_export.run_pipeline(popen_factory=lambda *_args, **_kwargs: process)


def test_interrupt_during_post_wait_group_check_stops_child(monkeypatch):
    process = SimpleNamespace(pid=43211, wait=lambda **_kwargs: 0)
    cleanups = []
    monkeypatch.setattr(nightly_export.os, "name", "posix")
    monkeypatch.setattr(
        nightly_export,
        "_process_group_exists",
        lambda _pid: (_ for _ in ()).throw(
            nightly_export.ControlledInterruption("signal during group check")
        ),
    )
    monkeypatch.setattr(
        nightly_export,
        "_stop_child",
        lambda child, timeout: cleanups.append((child, timeout)),
    )

    with pytest.raises(
        nightly_export.ControlledInterruption, match="signal during group check"
    ):
        nightly_export.run_pipeline(
            popen_factory=lambda *_args, **_kwargs: process,
            terminate_timeout=9.0,
        )

    assert cleanups == [(process, 9.0)]


def test_interrupt_after_wait_before_group_check_stops_child_once(monkeypatch):
    process = SimpleNamespace(pid=43213, wait=lambda **_kwargs: 0)
    guard_entries = 0
    cleanups = []

    @contextmanager
    def interrupt_on_completion_guard():
        nonlocal guard_entries
        guard_entries += 1
        if guard_entries == 2:
            raise nightly_export.ControlledInterruption(
                "signal before group check"
            )
        yield SimpleNamespace(interrupted=False)

    monkeypatch.setattr(nightly_export.os, "name", "posix")
    monkeypatch.setattr(
        nightly_export,
        "_blocked_termination_signals",
        interrupt_on_completion_guard,
    )
    monkeypatch.setattr(
        nightly_export,
        "_process_group_exists",
        lambda _pid: pytest.fail("group check must not start"),
    )
    monkeypatch.setattr(
        nightly_export,
        "_stop_child",
        lambda child, timeout: cleanups.append((child, timeout)),
    )

    with pytest.raises(
        nightly_export.ControlledInterruption, match="signal before group check"
    ):
        nightly_export.run_pipeline(
            popen_factory=lambda *_args, **_kwargs: process,
            terminate_timeout=11.0,
        )

    assert guard_entries == 2
    assert cleanups == [(process, 11.0)]


def test_pending_signal_during_creation_stops_child_before_interrupt(monkeypatch):
    events = []
    process = SimpleNamespace()

    @contextmanager
    def pending_creation_signal():
        state = SimpleNamespace(interrupted=False)
        yield state
        state.interrupted = True

    monkeypatch.setattr(
        nightly_export, "_blocked_termination_signals", pending_creation_signal
    )
    monkeypatch.setattr(
        nightly_export,
        "_stop_child",
        lambda child, timeout: events.append((child, timeout)),
    )

    with pytest.raises(
        nightly_export.ControlledInterruption, match="during child creation"
    ):
        nightly_export.run_pipeline(
            popen_factory=lambda *_args, **_kwargs: process,
            terminate_timeout=7.0,
        )

    assert events == [(process, 7.0)]


def test_keyboard_interrupt_terminates_and_waits_for_child():
    events = []

    class Process:
        waits = 0

        def wait(self, **kwargs):
            self.waits += 1
            events.append(("wait", kwargs))
            if self.waits == 1:
                raise KeyboardInterrupt
            return -15

        def poll(self):
            events.append(("poll", {}))
            return None if self.waits < 2 else -15

        def terminate(self):
            events.append(("terminate", {}))

    with pytest.raises(nightly_export.ControlledInterruption):
        nightly_export.run_pipeline(popen_factory=lambda *_args, **_kwargs: Process())
    assert [event for event, _ in events] == [
        "wait",
        "poll",
        "terminate",
        "wait",
        "poll",
    ]


def test_repeated_interrupt_during_cleanup_is_deferred_until_child_exits():
    events = []

    class Process:
        waits = 0

        def wait(self, **kwargs):
            self.waits += 1
            events.append(("wait", kwargs))
            if self.waits <= 2:
                raise KeyboardInterrupt
            return -15

        def poll(self):
            events.append(("poll", {}))
            return None if self.waits < 3 else -15

        def terminate(self):
            events.append(("terminate", {}))

    with pytest.raises(nightly_export.ControlledInterruption):
        nightly_export.run_pipeline(popen_factory=lambda *_args, **_kwargs: Process())

    assert [event for event, _ in events].count("terminate") == 1
    assert [event for event, _ in events].count("wait") == 3


def test_cleanup_timeout_requires_manual_reconciliation():
    class Process:
        def wait(self, **kwargs):
            if kwargs:
                raise subprocess.TimeoutExpired("pipeline", kwargs["timeout"])
            raise KeyboardInterrupt

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

    with pytest.raises(
        nightly_export.ManualReconciliationRequired,
        match="manual reconciliation required",
    ):
        nightly_export.run_pipeline(
            popen_factory=lambda *_args, **_kwargs: Process(),
            terminate_timeout=0.01,
        )


def job_record(status="processing"):
    return SimpleNamespace(id=uuid4(), status=status)


def test_skipped_acquisition_does_no_work(monkeypatch, tmp_path):
    record = job_record("completed")
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            acquired=False, status="skipped_completed", run=record
        ),
    )
    monkeypatch.setattr(
        nightly_export,
        "export_telemetry_day",
        lambda *_args, **_kwargs: pytest.fail("must not export"),
    )
    assert nightly_export.execute_nightly_run(object(), TARGET, output_directory=tmp_path) == "skipped_completed"


def test_success_completes_only_after_export_and_pipeline(monkeypatch, tmp_path):
    record = job_record()
    events = []
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(acquired=True, status="acquired", run=record),
    )
    monkeypatch.setattr(
        nightly_export,
        "export_telemetry_day",
        lambda *_args, **_kwargs: events.append("export"),
    )
    monkeypatch.setattr(
        nightly_export,
        "complete_job_run",
        lambda *_args, **_kwargs: events.append("complete")
        or SimpleNamespace(changed=True, run=job_record("completed")),
    )
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda *_args, **_kwargs: pytest.fail("must not fail"),
    )

    result = nightly_export.execute_nightly_run(
        object(),
        TARGET,
        output_directory=tmp_path,
        pipeline_runner=lambda: events.append("pipeline"),
    )
    assert result == "completed"
    assert events == ["export", "pipeline", "complete"]


def test_pipeline_failure_preserves_csv_and_marks_failed(monkeypatch, tmp_path):
    record = job_record()
    final = nightly_export.daily_csv_path(tmp_path, TARGET)
    diagnostics = []
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(acquired=True, status="acquired", run=record),
    )
    monkeypatch.setattr(
        nightly_export,
        "export_telemetry_day",
        lambda *_args, **_kwargs: nightly_export.publish_csv_rows([], final, TARGET),
    )
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda _engine, _run_id, error: diagnostics.append(error)
        or SimpleNamespace(changed=True, run=job_record("failed")),
    )

    with pytest.raises(nightly_export.PipelineProcessError):
        nightly_export.execute_nightly_run(
            object(),
            TARGET,
            output_directory=tmp_path,
            pipeline_runner=lambda: (_ for _ in ()).throw(
                nightly_export.PipelineProcessError("pipeline execution failed (exit code 1)")
            ),
        )
    assert final.exists()
    assert diagnostics == ["pipeline execution failed (exit code 1)"]


def test_failed_run_retries_same_date_with_validated_immutable_csv(
    monkeypatch, tmp_path
):
    """A failed attempt releases the date while preserving its published CSV."""
    first = job_record()
    retry = job_record()
    acquisitions = iter((first, retry))
    final = nightly_export.daily_csv_path(tmp_path, TARGET)
    with final.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=nightly_export.CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(nightly_export.serialize_telemetry_row(telemetry_row(), TARGET))
    original = final.read_bytes()
    transitions = []

    class NoTelemetryQueryEngine:
        def connect(self):
            pytest.fail("a validated existing CSV must not query telemetry again")

    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            acquired=True,
            status="acquired",
            run=next(acquisitions),
        ),
    )
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda _engine, run_id, error: transitions.append(("failed", run_id, error))
        or SimpleNamespace(changed=True, run=job_record("failed")),
    )
    monkeypatch.setattr(
        nightly_export,
        "complete_job_run",
        lambda _engine, run_id: transitions.append(("completed", run_id, None))
        or SimpleNamespace(changed=True, run=job_record("completed")),
    )

    with pytest.raises(nightly_export.PipelineProcessError):
        nightly_export.execute_nightly_run(
            NoTelemetryQueryEngine(),
            TARGET,
            output_directory=tmp_path,
            pipeline_runner=lambda: (_ for _ in ()).throw(
                nightly_export.PipelineProcessError(
                    "pipeline configuration or quality blocked execution (exit code 2)"
                )
            ),
        )

    assert final.read_bytes() == original
    assert nightly_export.execute_nightly_run(
        NoTelemetryQueryEngine(),
        TARGET,
        output_directory=tmp_path,
        pipeline_runner=lambda: None,
    ) == "completed"
    assert final.read_bytes() == original
    assert transitions == [
        (
            "failed",
            first.id,
            "pipeline configuration or quality blocked execution (exit code 2)",
        ),
        ("completed", retry.id, None),
    ]
    assert first.id != retry.id


def test_unconfirmed_child_cleanup_keeps_processing_and_skips_failed_cas(
    monkeypatch, tmp_path, caplog
):
    record = job_record()
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            acquired=True, status="acquired", run=record
        ),
    )
    monkeypatch.setattr(nightly_export, "export_telemetry_day", lambda *_a, **_k: None)
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda *_args, **_kwargs: pytest.fail("processing ownership must be preserved"),
    )
    caplog.set_level("CRITICAL", logger="trackflow.nightly_export")

    with pytest.raises(nightly_export.ManualReconciliationRequired):
        nightly_export.execute_nightly_run(
            object(),
            TARGET,
            output_directory=tmp_path,
            pipeline_runner=lambda: (_ for _ in ()).throw(
                nightly_export.ManualReconciliationRequired(
                    "child cleanup could not be confirmed; manual reconciliation required"
                )
            ),
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["job_run_id"] == str(record.id)
    assert payload["action"] == "reconciliation_required"
    assert payload["result"] == "cleanup_unconfirmed"
    assert payload["terminal"] == "processing"


def test_unconfirmed_cleanup_before_post_wait_group_check_keeps_processing(
    monkeypatch, tmp_path
):
    record = job_record()
    process = SimpleNamespace(pid=43212, wait=lambda **_kwargs: 0)
    guard_entries = 0

    @contextmanager
    def interrupt_on_completion_guard():
        nonlocal guard_entries
        guard_entries += 1
        if guard_entries == 2:
            raise nightly_export.ControlledInterruption(
                "signal before group check"
            )
        yield SimpleNamespace(interrupted=False)

    monkeypatch.setattr(nightly_export.os, "name", "posix")
    monkeypatch.setattr(
        nightly_export,
        "_blocked_termination_signals",
        interrupt_on_completion_guard,
    )
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            acquired=True, status="acquired", run=record
        ),
    )
    monkeypatch.setattr(nightly_export, "export_telemetry_day", lambda *_a, **_k: None)
    monkeypatch.setattr(
        nightly_export,
        "_process_group_exists",
        lambda _pid: pytest.fail("group check must not start"),
    )
    monkeypatch.setattr(
        nightly_export,
        "_stop_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            nightly_export.ManualReconciliationRequired(
                "child cleanup could not be confirmed; manual reconciliation required"
            )
        ),
    )
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda *_args, **_kwargs: pytest.fail("processing must remain owned"),
    )

    with pytest.raises(nightly_export.ManualReconciliationRequired):
        nightly_export.execute_nightly_run(
            object(),
            TARGET,
            output_directory=tmp_path,
            pipeline_runner=lambda: nightly_export.run_pipeline(
                popen_factory=lambda *_args, **_kwargs: process
            ),
        )
    assert guard_entries == 2


def test_terminal_failure_is_not_hidden(monkeypatch, tmp_path):
    record = job_record()
    monkeypatch.setattr(
        nightly_export,
        "acquire_job_run",
        lambda *_args, **_kwargs: SimpleNamespace(acquired=True, status="acquired", run=record),
    )
    monkeypatch.setattr(nightly_export, "export_telemetry_day", lambda *_a, **_k: None)
    monkeypatch.setattr(
        nightly_export,
        "complete_job_run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        nightly_export,
        "fail_job_run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("terminal unavailable")),
    )

    with pytest.raises(nightly_export.TerminalStateError):
        nightly_export.execute_nightly_run(
            object(), TARGET, output_directory=tmp_path, pipeline_runner=lambda: None
        )


def test_safe_diagnostic_never_exposes_unexpected_exception_text():
    secret = "postgresql://user:password@example.invalid/db"
    assert secret not in nightly_export._safe_diagnostic(RuntimeError(secret))
    assert nightly_export._safe_diagnostic(RuntimeError(secret)) == "RuntimeError during nightly execution"


def test_structured_log_is_utc_correlated_and_contains_no_secret(caplog):
    run_id = uuid4()
    secret = "postgresql://user:password@example.invalid/db"
    caplog.set_level("INFO", logger="trackflow.nightly_export")

    nightly_export._log(
        20,
        TARGET,
        run_id,
        stage="pipeline",
        action="execute",
        result="failed",
        terminal="processing",
        error=nightly_export._safe_diagnostic(RuntimeError(secret)),
    )

    payload = json.loads(caplog.records[-1].message)
    assert payload["timestamp"].endswith("Z")
    assert payload["job_name"] == "nightly_export"
    assert payload["target_date"] == TARGET.isoformat()
    assert payload["job_run_id"] == str(run_id)
    assert payload["stage"] == "pipeline"
    assert payload["action"] == "execute"
    assert payload["result"] == "failed"
    assert payload["terminal"] == "processing"
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "database_url",
    [None, "not a database URL", "sqlite://", "mysql://user:secret@host/db"],
)
def test_main_rejects_missing_malformed_or_non_postgres_before_engine(
    database_url, monkeypatch, caplog
):
    environment = {} if database_url is None else {"DATABASE_URL": database_url}
    monkeypatch.setattr(
        nightly_export,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("engine must not be created"),
    )
    monkeypatch.setattr(
        nightly_export,
        "execute_nightly_run",
        lambda *_args, **_kwargs: pytest.fail("run must not be acquired"),
    )
    caplog.set_level("ERROR", logger="trackflow.nightly_export")

    assert nightly_export.main([], environment=environment, now=NOW) == 1
    if database_url:
        assert database_url not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "database_url",
    ["postgresql://user:pw@localhost/db", "postgresql+psycopg2://user:pw@localhost/db"],
)
def test_main_accepts_supported_postgres_drivers(
    database_url, monkeypatch, tmp_path
):
    engine = SimpleNamespace(dispose=lambda: None)
    observed = {}
    monkeypatch.setattr(
        nightly_export,
        "create_engine",
        lambda url, **kwargs: observed.update(url=url, kwargs=kwargs) or engine,
    )
    monkeypatch.setattr(
        nightly_export,
        "execute_nightly_run",
        lambda actual_engine, target_date, **kwargs: observed.update(
            engine=actual_engine, target_date=target_date, run_kwargs=kwargs
        ),
    )

    assert nightly_export.main(
        [],
        environment={"DATABASE_URL": database_url},
        now=NOW,
        output_directory=tmp_path,
    ) == 0
    assert observed["url"] == database_url
    assert observed["kwargs"] == {"pool_pre_ping": True}
    assert observed["engine"] is engine
    assert observed["target_date"] == TARGET
