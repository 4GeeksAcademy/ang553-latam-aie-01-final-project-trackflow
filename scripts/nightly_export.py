"""Export one UTC day of telemetry and run TrackFlow's weekly pipeline.

The durable ``job_runs`` claim is acquired before any work starts.  CSV
publication uses a temporary file in the destination directory and an atomic
hard link, so an existing final artifact is never overwritten.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.exc import SQLAlchemyError

from services.job_runner import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    acquire_job_run,
    complete_job_run,
    fail_job_run,
)

JOB_NAME = "nightly_export"
TARGET_DATE_ENV = "TARGET_DATE"
DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_OUTPUT_DIRECTORY = Path("data/raw")
CSV_COLUMNS = (
    "id",
    "timestamp",
    "service",
    "event_type",
    "level",
    "value",
    "message",
    "tags",
)
PIPELINE_COMMAND = (sys.executable, "-m", "pipelines.pipeline")
PIPELINE_EXIT_MESSAGES = {
    1: "pipeline execution failed",
    2: "pipeline configuration or quality blocked execution",
    3: "pipeline was superseded or already in progress",
    4: "pipeline result is uncertain",
}
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_LOGGER = logging.getLogger("trackflow.nightly_export")

_SELECT_TELEMETRY = text(
    """
    SELECT id, timestamp, service, event_type, level, value, message, tags
    FROM telemetry_events
    WHERE timestamp >= :start_at AND timestamp < :end_at
    ORDER BY timestamp, id
    """
)


class NightlyExportError(RuntimeError):
    """Controlled failure with a safe operator-facing diagnostic."""


class NightlyConfigurationError(NightlyExportError):
    """Required environment or target-date configuration is invalid."""


class CsvArtifactError(NightlyExportError):
    """The daily CSV could not be safely created or reused."""


class PipelineProcessError(NightlyExportError):
    """The child pipeline did not report a confirmed success."""


class ControlledInterruption(NightlyExportError):
    """A controllable signal or keyboard interruption stopped the run."""


class ManualReconciliationRequired(NightlyExportError):
    """Child cleanup is uncertain, so the durable claim must remain processing."""


class TerminalStateError(NightlyExportError):
    """The acquired job could not be confirmed in its required terminal state."""


def resolve_target_date(
    raw_value: str | None,
    *,
    now: datetime | None = None,
) -> date:
    """Parse strict YYYY-MM-DD or return yesterday according to UTC."""
    if raw_value is not None:
        if not _DATE_PATTERN.fullmatch(raw_value):
            raise NightlyConfigurationError("TARGET_DATE must use YYYY-MM-DD")
        try:
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise NightlyConfigurationError("TARGET_DATE is not a valid date") from exc
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise NightlyConfigurationError("the application clock must be timezone-aware")
    return moment.astimezone(timezone.utc).date() - timedelta(days=1)


def daily_csv_path(output_directory: Path, target_date: date) -> Path:
    """Return the fixed DEV-53 artifact name for one UTC date."""
    return output_directory / f"telemetry_{target_date.isoformat()}.csv"


def export_telemetry_day(engine: Any, target_date: date, output_directory: Path) -> Path:
    """Read one UTC day and safely publish or reuse its validated CSV."""
    final_path = daily_csv_path(output_directory, target_date)
    if final_path.exists():
        validate_existing_csv(final_path, target_date)
        return final_path

    start_at = datetime.combine(target_date, datetime.min.time(), timezone.utc)
    end_at = start_at + timedelta(days=1)
    try:
        with engine.connect() as connection:
            rows = connection.execution_options(stream_results=True).execute(
                _SELECT_TELEMETRY,
                {"start_at": start_at, "end_at": end_at},
            ).mappings()
            return publish_csv_rows(rows, final_path, target_date)
    except CsvArtifactError:
        raise
    except SQLAlchemyError as exc:
        raise NightlyExportError("telemetry export query failed") from exc


def publish_csv_rows(
    rows: Iterable[Mapping[str, Any]],
    final_path: Path,
    target_date: date,
) -> Path:
    """Write, fsync and atomically hard-link a complete CSV without overwrite."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        validate_existing_csv(final_path, target_date)
        return final_path

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.",
        suffix=".tmp",
        dir=final_path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow(serialize_telemetry_row(row, target_date))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, final_path)
        except FileExistsError:
            validate_existing_csv(final_path, target_date)
        except OSError as exc:
            raise CsvArtifactError("atomic hard-link publication failed") from exc
        return final_path
    except (CsvArtifactError, OSError, TypeError, ValueError, csv.Error) as exc:
        if isinstance(exc, CsvArtifactError):
            raise
        raise CsvArtifactError("daily CSV creation failed") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def serialize_telemetry_row(row: Mapping[str, Any], target_date: date) -> dict[str, str]:
    """Serialize one database row with stable UTC and JSON representations."""
    try:
        event_id = str(UUID(str(row["id"])))
        timestamp = _utc_timestamp(row["timestamp"])
        if timestamp.date() != target_date:
            raise ValueError("row is outside the requested UTC day")
        tags = json.dumps(
            row["tags"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        value = "" if row["value"] is None else format(Decimal(row["value"]), "f")
        return {
            "id": event_id,
            "timestamp": _format_utc(timestamp),
            "service": str(row["service"]),
            "event_type": str(row["event_type"]),
            "level": "" if row["level"] is None else str(row["level"]),
            "value": value,
            "message": "" if row["message"] is None else str(row["message"]),
            "tags": tags,
        }
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CsvArtifactError("telemetry row could not be serialized") from exc


def validate_existing_csv(path: Path, target_date: date) -> int:
    """Validate the complete shape and parseability of a reusable artifact."""
    start_at = datetime.combine(target_date, datetime.min.time(), timezone.utc)
    end_at = start_at + timedelta(days=1)
    count = 0
    previous_key: tuple[datetime, int] | None = None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise CsvArtifactError("existing daily CSV has an invalid header")
            for row in reader:
                if None in row or set(row) != set(CSV_COLUMNS):
                    raise CsvArtifactError("existing daily CSV has an invalid row shape")
                event_id = UUID(row["id"])
                timestamp = _parse_csv_timestamp(row["timestamp"])
                if not start_at <= timestamp < end_at:
                    raise CsvArtifactError("existing daily CSV contains an out-of-range row")
                current_key = (timestamp, event_id.int)
                if previous_key is not None and current_key < previous_key:
                    raise CsvArtifactError("existing daily CSV rows are not ordered")
                previous_key = current_key
                if row["value"]:
                    Decimal(row["value"])
                json.loads(row["tags"])
                count += 1
    except CsvArtifactError:
        raise
    except (OSError, UnicodeError, ValueError, InvalidOperation, json.JSONDecodeError, csv.Error) as exc:
        raise CsvArtifactError("existing daily CSV is corrupt") from exc
    return count


def run_pipeline(
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    terminate_timeout: float = 30.0,
) -> None:
    """Run the exact approved module command and require exit code zero."""
    popen_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "start_new_session": os.name == "posix",
    }
    if os.name == "posix":
        popen_options["preexec_fn"] = _unblock_termination_signals_in_child
    process: Any | None = None
    cleanup_confirmed = False
    try:
        with _blocked_termination_signals() as creation_signals:
            process = popen_factory(
                list(PIPELINE_COMMAND),
                **popen_options,
            )
        if creation_signals.interrupted:
            raise ControlledInterruption(
                "pipeline execution was interrupted during child creation"
            )
        return_code = process.wait()
        with _blocked_termination_signals() as completion_signals:
            group_id = (
                getattr(process, "pid", None)
                if os.name == "posix"
                and isinstance(getattr(process, "pid", None), int)
                else None
            )
            if group_id is not None and _process_group_exists(group_id):
                # A reaped parent does not prove that Prefect descendants in
                # its session have exited. Do not release durable ownership
                # until the group is gone.
                _stop_child(process, terminate_timeout)
            cleanup_confirmed = True
        if completion_signals.interrupted:
            raise ControlledInterruption(
                "pipeline execution was interrupted after child completion"
            )
    except ManualReconciliationRequired:
        raise
    except (KeyboardInterrupt, ControlledInterruption) as exc:
        if process is not None and not cleanup_confirmed:
            try:
                _stop_child(process, terminate_timeout)
            except ManualReconciliationRequired as cleanup_error:
                raise cleanup_error from exc
        message = (
            str(exc)
            if isinstance(exc, ControlledInterruption) and str(exc)
            else "pipeline execution was interrupted"
        )
        raise ControlledInterruption(message) from exc
    if return_code != 0:
        message = PIPELINE_EXIT_MESSAGES.get(return_code, "pipeline exited unsuccessfully")
        raise PipelineProcessError(f"{message} (exit code {return_code})")


def execute_nightly_run(
    engine: Any,
    target_date: date,
    *,
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY,
    pipeline_runner: Callable[[], None] = run_pipeline,
) -> str:
    """Acquire, execute, and durably finalize one functional nightly run."""
    try:
        acquisition = acquire_job_run(engine, JOB_NAME, target_date)
    except Exception as exc:
        _log(
            logging.ERROR,
            target_date,
            None,
            stage="acquisition",
            action="acquire",
            result="failed",
            terminal="not_acquired",
            error=_safe_diagnostic(exc),
        )
        raise
    run_id = acquisition.run.id
    if not acquisition.acquired:
        _log(
            logging.INFO,
            target_date,
            run_id,
            stage="acquisition",
            action="acquire",
            result=acquisition.status,
            terminal=acquisition.run.status,
        )
        return acquisition.status

    _log(
        logging.INFO,
        target_date,
        run_id,
        stage="acquisition",
        action="acquire",
        result="acquired",
        terminal="processing",
    )
    try:
        with _controlled_signal_handlers():
            export_telemetry_day(engine, target_date, output_directory)
            _log(
                logging.INFO,
                target_date,
                run_id,
                stage="export",
                action="publish",
                result="ready",
                terminal="processing",
            )
            pipeline_runner()
            _log(
                logging.INFO,
                target_date,
                run_id,
                stage="pipeline",
                action="execute",
                result="succeeded",
                terminal="processing",
            )
            transition = complete_job_run(engine, run_id)
            if not transition.changed or transition.run.status != STATUS_COMPLETED:
                raise TerminalStateError("completed state was not confirmed")
    except (Exception, KeyboardInterrupt) as primary_error:
        diagnostic = _safe_diagnostic(primary_error)
        if isinstance(primary_error, ManualReconciliationRequired):
            _log(
                logging.CRITICAL,
                target_date,
                run_id,
                stage="terminal",
                action="reconciliation_required",
                result="cleanup_unconfirmed",
                terminal="processing",
                error=diagnostic,
            )
            raise
        try:
            transition = fail_job_run(engine, run_id, diagnostic)
            if not transition.changed or transition.run.status != STATUS_FAILED:
                raise TerminalStateError("failed state was not confirmed")
        except Exception as terminal_error:
            _log(
                logging.ERROR,
                target_date,
                run_id,
                stage="terminal",
                action="transition_failed",
                result="unconfirmed",
                terminal="unknown",
                error="failed state could not be confirmed",
            )
            raise TerminalStateError(
                "nightly work failed and the failed state could not be confirmed"
            ) from terminal_error
        _log(
            logging.ERROR,
            target_date,
            run_id,
            stage="terminal",
            action="transition_failed",
            result="failed",
            terminal="failed",
            error=diagnostic,
        )
        raise

    _log(
        logging.INFO,
        target_date,
        run_id,
        stage="terminal",
        action="transition_completed",
        result="completed",
        terminal="completed",
    )
    return STATUS_COMPLETED


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY,
) -> int:
    """CLI entry point configured only by DATABASE_URL and optional TARGET_DATE."""
    if argv:
        _log_configuration_failure("nightly_export accepts no command-line arguments")
        return 1
    values = os.environ if environment is None else environment
    try:
        target_date = resolve_target_date(values.get(TARGET_DATE_ENV), now=now)
        database_url = values.get(DATABASE_URL_ENV)
        if not database_url:
            raise NightlyConfigurationError("DATABASE_URL is required")
        try:
            parsed_url = make_url(database_url)
        except ArgumentError as exc:
            raise NightlyConfigurationError("DATABASE_URL is malformed") from exc
        if parsed_url.drivername not in {"postgresql", "postgresql+psycopg2"}:
            raise NightlyConfigurationError(
                "DATABASE_URL must use a supported PostgreSQL driver"
            )
        engine = create_engine(database_url, pool_pre_ping=True)
    except Exception as exc:
        _log_configuration_failure(_safe_diagnostic(exc))
        return 1

    try:
        execute_nightly_run(engine, target_date, output_directory=output_directory)
    except (Exception, KeyboardInterrupt):
        return 1
    finally:
        try:
            engine.dispose()
        except Exception:
            _LOGGER.warning("nightly_export engine disposal failed")
    return 0


def _utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_csv_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc_timestamp(parsed)


def _stop_child(process: Any, timeout: float) -> None:
    """Stop and confirm the child/session without yielding to repeated signals."""
    if timeout <= 0:
        raise ManualReconciliationRequired(
            "child cleanup could not be confirmed; manual reconciliation required"
        )
    group_id = (
        getattr(process, "pid", None)
        if os.name == "posix" and isinstance(getattr(process, "pid", None), int)
        else None
    )
    with _blocked_termination_signals():
        try:
            if process.poll() is None:
                _signal_child(process, group_id, signal.SIGTERM)
                try:
                    _wait_ignoring_interruptions(process, timeout)
                except subprocess.TimeoutExpired:
                    _signal_child(
                        process,
                        group_id,
                        getattr(signal, "SIGKILL", signal.SIGTERM),
                        force=True,
                    )
                    _wait_ignoring_interruptions(process, timeout)
            else:
                _wait_ignoring_interruptions(process, timeout)
            if process.poll() is None:
                raise ManualReconciliationRequired(
                    "child cleanup could not be confirmed; manual reconciliation required"
                )
            if group_id is not None and _process_group_exists(group_id):
                _signal_child(
                    process,
                    group_id,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                    force=True,
                )
                _wait_for_process_group_exit(group_id, timeout)
        except ManualReconciliationRequired:
            raise
        except (KeyboardInterrupt, ControlledInterruption):
            raise ManualReconciliationRequired(
                "child cleanup was interrupted; manual reconciliation required"
            )
        except BaseException as exc:
            raise ManualReconciliationRequired(
                "child cleanup could not be confirmed; manual reconciliation required"
            ) from exc


def _signal_child(
    process: Any,
    group_id: int | None,
    signum: signal.Signals,
    *,
    force: bool = False,
) -> None:
    try:
        if group_id is not None:
            os.killpg(group_id, signum)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        return


def _wait_ignoring_interruptions(process: Any, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(PIPELINE_COMMAND, timeout)
        try:
            return process.wait(timeout=remaining)
        except (KeyboardInterrupt, ControlledInterruption):
            continue


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(group_id: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _process_group_exists(group_id):
        if time.monotonic() >= deadline:
            raise ManualReconciliationRequired(
                "pipeline process group is still active; manual reconciliation required"
            )
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


class _BlockedSignalState:
    interrupted = False


@contextmanager
def _blocked_termination_signals() -> Iterator[_BlockedSignalState]:
    """Block and consume repeated termination signals around critical windows."""
    state = _BlockedSignalState()
    if not all(
        hasattr(signal, name)
        for name in ("pthread_sigmask", "sigpending", "sigwait", "SIG_BLOCK", "SIG_SETMASK")
    ):
        yield state
        return
    watched = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    try:
        yield state
    finally:
        pending = set(signal.sigpending()).intersection(watched)
        state.interrupted = bool(pending)
        for signum in pending:
            signal.sigwait({signum})
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _unblock_termination_signals_in_child() -> None:
    """Undo the scheduler's short critical-window mask before POSIX exec.

    This hook is deliberately limited to the Linux scheduler's single-threaded
    launch path; it must not grow into application work in the pre-exec child.
    """
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})


@contextmanager
def _controlled_signal_handlers() -> Iterator[None]:
    previous: dict[signal.Signals, Any] = {}

    def interrupt(signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        raise ControlledInterruption(f"nightly execution interrupted by {name}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _safe_diagnostic(error: BaseException) -> str:
    if isinstance(error, NightlyExportError):
        return str(error)[:500]
    return f"{type(error).__name__} during nightly execution"[:500]


def _log(
    level: int,
    target_date: date,
    run_id: UUID | None,
    *,
    stage: str,
    action: str,
    result: str,
    terminal: str,
    error: str | None = None,
) -> None:
    payload = {
        "timestamp": _format_utc(datetime.now(timezone.utc)),
        "job_name": JOB_NAME,
        "target_date": target_date.isoformat(),
        "job_run_id": None if run_id is None else str(run_id),
        "stage": stage,
        "action": action,
        "result": result,
        "terminal": terminal,
    }
    if error is not None:
        payload["error"] = error
    _LOGGER.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _log_configuration_failure(error: str) -> None:
    payload = {
        "timestamp": _format_utc(datetime.now(timezone.utc)),
        "job_name": JOB_NAME,
        "stage": "configuration",
        "action": "validate",
        "result": "failed",
        "terminal": "not_acquired",
        "error": error,
    }
    _LOGGER.error(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main(sys.argv[1:]))
