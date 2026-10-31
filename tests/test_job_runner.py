from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from services.job_runner import service


NOW = datetime(2026, 10, 31, 7, 15, tzinfo=timezone.utc)
TARGET = date(2026, 10, 30)


def _result(row):
    result = Mock()
    result.mappings.return_value.first.return_value = row
    return result


def _run_row(status, *, run_id=None):
    return {
        "id": run_id or uuid4(),
        "job_name": "nightly_export",
        "target_date": TARGET,
        "status": status,
        "started_at": NOW if status != service.STATUS_PENDING else None,
        "finished_at": None,
        "error_message": None,
        "created_at": NOW,
    }


def _successful_acquisition_connection(*, commit_error=None, close_error=None):
    run_id = uuid4()
    transaction = Mock()
    transaction.is_active = True
    if commit_error is not None:
        transaction.commit.side_effect = commit_error
    connection = Mock()
    connection.begin.return_value = transaction
    connection.execute.side_effect = [
        _result(_run_row(service.STATUS_PENDING, run_id=run_id)),
        _result(_run_row(service.STATUS_PROCESSING, run_id=run_id)),
    ]
    if close_error is not None:
        connection.close.side_effect = close_error
    return connection, transaction


def test_ddl_defines_states_shape_and_both_partial_unique_indexes():
    ddl = Path("infra/sql/job_runs.sql").read_text(encoding="utf-8")
    normalized = " ".join(ddl.lower().split())

    assert "status in ('pending', 'processing', 'completed', 'failed')" in normalized
    assert "job_runs_one_processing_per_job_idx" in ddl
    assert "where status = 'processing'" in normalized
    assert "job_runs_target_claim_idx" in ddl
    assert "where status in ('processing', 'completed')" in normalized
    assert "job_runs_history_idx" in ddl
    assert "job_runs_started_after_created" in ddl
    assert "lease" not in normalized
    assert "advisory" not in normalized


def test_job_runner_is_included_in_project_package_discovery():
    configuration = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    includes = configuration["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "services.job_runner*" in includes


@pytest.mark.parametrize(
    "value",
    ["", " nightly_export", "nightly_export ", "x" * 101, None],
)
def test_job_name_validation_rejects_invalid_values(value):
    with pytest.raises(service.JobRunValidationError):
        service._job_name(value)


def test_target_and_timestamp_validation_and_utc_normalization():
    assert service._target_date(TARGET) == TARGET
    assert service._utc_time(datetime(2026, 10, 30, 23, 15, tzinfo=timezone(timedelta(hours=-7)))) == datetime(
        2026, 10, 31, 6, 15, tzinfo=timezone.utc
    )
    with pytest.raises(service.JobRunValidationError):
        service._target_date(NOW)
    with pytest.raises(service.JobRunValidationError):
        service._utc_time(datetime(2026, 10, 31, 7, 15))


def test_non_sensitive_error_sanitization_is_bounded_and_single_line():
    message = service.sanitize_error_message("warehouse timeout\n" + "x" * 600)

    assert message.startswith("warehouse timeout ")
    assert len(message) == service.MAX_ERROR_MESSAGE_LENGTH


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Bearer fictional.header.signature", "fictional.header.signature"),
        ('{"token":"fictional-json-token","operation":"export"}', "fictional-json-token"),
        ('{"password":"fictional-json-password"}', "fictional-json-password"),
        ("connect postgresql://fictional:credential@example.invalid/db", "credential"),
    ],
)
def test_sensitive_error_formats_use_conservative_persisted_message(message, secret):
    sanitized = service.sanitize_error_message(RuntimeError(message))

    assert secret not in sanitized
    assert "sensitive details redacted" in sanitized
    assert sanitized.startswith("RuntimeError failed")


def test_acquisition_wraps_connect_failure_with_original_cause():
    failure = OperationalError("connect", {}, RuntimeError("offline"))
    engine = Mock()
    engine.connect.side_effect = failure

    with pytest.raises(service.JobRunPersistenceError) as raised:
        service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert raised.value.__cause__ is failure


def test_acquisition_wraps_begin_failure_and_closes_open_connection():
    failure = OperationalError("begin", {}, RuntimeError("offline"))
    connection = Mock()
    connection.begin.side_effect = failure
    engine = Mock()
    engine.connect.return_value = connection

    with pytest.raises(service.JobRunPersistenceError) as raised:
        service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert raised.value.__cause__ is failure
    connection.close.assert_called_once_with()


def test_acquisition_preserves_sql_error_when_secondary_rollback_fails(caplog):
    primary = OperationalError(
        "INSERT Authorization: Bearer fictional-primary-secret",
        {},
        RuntimeError("offline"),
    )
    secondary = OperationalError(
        "ROLLBACK token=fictional-secondary-secret",
        {},
        RuntimeError("offline"),
    )
    transaction = Mock()
    transaction.is_active = True
    transaction.rollback.side_effect = secondary
    connection = Mock()
    connection.begin.return_value = transaction
    connection.execute.side_effect = primary
    engine = Mock()
    engine.connect.return_value = connection

    with pytest.raises(service.JobRunPersistenceError) as raised:
        service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert raised.value.__cause__ is primary
    assert "fictional-primary-secret" not in str(raised.value)
    assert "fictional-primary-secret" not in caplog.text
    assert "fictional-secondary-secret" not in caplog.text
    assert "operation=rollback" in caplog.text
    assert "commit_state=not_confirmed" in caplog.text
    assert "cleanup_error_type=OperationalError" in caplog.text
    transaction.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()
    engine.begin.assert_not_called()


def test_acquisition_rolls_back_successfully_after_sql_error():
    failure = OperationalError("INSERT", {}, RuntimeError("offline"))
    transaction = Mock()
    transaction.is_active = True
    connection = Mock()
    connection.begin.return_value = transaction
    connection.execute.side_effect = failure
    engine = Mock()
    engine.connect.return_value = connection

    with pytest.raises(service.JobRunPersistenceError) as raised:
        service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert raised.value.__cause__ is failure
    transaction.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_acquisition_returns_acquired_after_confirmed_commit_and_successful_close():
    connection, transaction = _successful_acquisition_connection()
    engine = Mock()
    engine.connect.return_value = connection

    result = service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert result.acquired is True
    assert result.run.status == service.STATUS_PROCESSING
    transaction.commit.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_acquisition_returns_acquired_when_close_fails_after_confirmed_commit(caplog):
    close_error = OperationalError(
        "CLOSE password=fictional-close-secret",
        {},
        RuntimeError("offline"),
    )
    connection, transaction = _successful_acquisition_connection(close_error=close_error)
    engine = Mock()
    engine.connect.return_value = connection

    result = service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert result.acquired is True
    assert result.run.status == service.STATUS_PROCESSING
    transaction.commit.assert_called_once_with()
    connection.close.assert_called_once_with()
    connection.invalidate.assert_called_once_with()
    assert "fictional-close-secret" not in caplog.text
    assert "operation=close" in caplog.text
    assert "commit_state=confirmed" in caplog.text
    assert "cleanup_error_type=OperationalError" in caplog.text


def test_acquisition_does_not_assume_success_when_commit_result_is_uncertain():
    commit_error = OperationalError(
        "COMMIT connection lost",
        {},
        RuntimeError("connection lost"),
    )
    connection, transaction = _successful_acquisition_connection(
        commit_error=commit_error
    )
    engine = Mock()
    engine.connect.return_value = connection

    with pytest.raises(service.JobRunPersistenceError) as raised:
        service.acquire_job_run(engine, "nightly_export", TARGET, now=NOW)

    assert raised.value.__cause__ is commit_error
    transaction.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize(
    "lookup",
    [
        lambda engine: service.get_job_run(engine, uuid4()),
        lambda engine: service.get_latest_job_run(engine, "nightly_export", TARGET),
    ],
)
def test_read_operations_wrap_connection_failures(lookup):
    failure = OperationalError("connect", {}, RuntimeError("offline"))
    engine = Mock()
    engine.connect.side_effect = failure

    with pytest.raises(service.JobRunPersistenceError) as raised:
        lookup(engine)

    assert raised.value.__cause__ is failure


def test_expected_conflicts_are_recognized_only_by_approved_index_name():
    def conflict(name):
        original = SimpleNamespace(diag=SimpleNamespace(constraint_name=name))
        return IntegrityError("update", {}, original)

    assert service._expected_conflict(conflict(service.PROCESSING_CONSTRAINT))
    assert service._expected_conflict(conflict(service.TARGET_CONSTRAINT))
    assert not service._expected_conflict(conflict("job_runs_pkey"))


def test_record_rejects_unknown_persisted_status():
    with pytest.raises(service.JobRunPersistenceError):
        service._record(
            {
                "id": uuid4(),
                "job_name": "nightly_export",
                "target_date": TARGET,
                "status": "zombie",
                "started_at": NOW,
                "finished_at": None,
                "error_message": None,
                "created_at": NOW,
            }
        )


def test_acquisition_result_and_terminal_contracts_are_explicit():
    record = service.JobRunRecord(
        uuid4(), "nightly_export", TARGET, service.STATUS_PROCESSING, NOW, None, None, NOW
    )
    assert service.AcquisitionResult(service.ACQUIRED, record).acquired is True
    assert service.AcquisitionResult(service.SKIPPED_PROCESSING, record).acquired is False
    assert service.TERMINAL_STATUSES == {service.STATUS_COMPLETED, service.STATUS_FAILED}


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    [
        (service.STATUS_PENDING, service.STATUS_PROCESSING),
        (service.STATUS_PROCESSING, service.STATUS_COMPLETED),
        (service.STATUS_PROCESSING, service.STATUS_FAILED),
    ],
)
def test_valid_status_transitions(current_status, next_status):
    assert service.is_valid_status_transition(current_status, next_status)


@pytest.mark.parametrize(
    ("current_status", "next_status"),
    [
        (service.STATUS_PENDING, service.STATUS_COMPLETED),
        (service.STATUS_PENDING, service.STATUS_FAILED),
        (service.STATUS_COMPLETED, service.STATUS_PROCESSING),
        (service.STATUS_FAILED, service.STATUS_PROCESSING),
        (service.STATUS_PROCESSING, service.STATUS_PROCESSING),
        ("unknown", service.STATUS_PROCESSING),
    ],
)
def test_invalid_status_transitions(current_status, next_status):
    assert not service.is_valid_status_transition(current_status, next_status)
