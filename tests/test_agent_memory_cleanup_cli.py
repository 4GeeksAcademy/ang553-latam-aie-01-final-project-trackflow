"""Operational entry-point tests for bounded MEM-092 cleanup."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO

from scripts.agent_memory_cleanup import execute_cleanup, main


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _Session:
    def get_bind(self):
        return _Bind()


class _Store:
    def __init__(self, result=(2, 3), failure: Exception | None = None):
        self.result = result
        self.failure = failure
        self.calls = []

    def expire_and_clean(self, session, **kwargs):
        self.calls.append((session, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.result


def _factory(session):
    @contextmanager
    def factory():
        yield session

    return factory


def test_execute_cleanup_reuses_store_with_postgresql_and_capacity():
    session = _Session()
    store = _Store()
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

    result = execute_cleanup(
        session_factory=_factory(session),
        store_factory=lambda: store,
        max_active_per_scope_category=7,
        now=now,
    )

    assert result == (2, 3)
    assert store.calls == [
        (
            session,
            {"now": now, "max_active_per_scope_category": 7},
        )
    ]


def test_main_requires_explicit_execute_without_opening_database():
    opened = False

    @contextmanager
    def forbidden_factory():
        nonlocal opened
        opened = True
        yield _Session()

    stdout, stderr = StringIO(), StringIO()
    result = main(
        [],
        session_factory=forbidden_factory,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert opened is False
    assert stdout.getvalue() == ""
    assert "without --execute" in stderr.getvalue()


def test_main_emits_only_content_free_counts():
    stdout, stderr = StringIO(), StringIO()
    result = main(
        ["--execute", "--max-active-per-scope-category", "9"],
        session_factory=_factory(_Session()),
        store_factory=lambda: _Store((1, 4)),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    assert stdout.getvalue() == (
        '{"expired_entries":4,"expired_proposals":1,'
        '"max_active_per_scope_category":9,"status":"completed"}\n'
    )


def test_main_fails_closed_without_exception_or_database_detail():
    stdout, stderr = StringIO(), StringIO()
    result = main(
        ["--execute"],
        session_factory=_factory(_Session()),
        store_factory=lambda: _Store(
            failure=RuntimeError("postgresql://user:secret@example.test/private")
        ),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert "secret" not in stderr.getvalue()
    assert "private" not in stderr.getvalue()
    assert stderr.getvalue() == (
        "Agent memory cleanup failed; inspect sanitized server diagnostics.\n"
    )


def test_main_rejects_non_positive_capacity_before_opening_database():
    opened = False

    @contextmanager
    def forbidden_factory():
        nonlocal opened
        opened = True
        yield _Session()

    stdout, stderr = StringIO(), StringIO()
    result = main(
        ["--execute", "--max-active-per-scope-category", "0"],
        session_factory=forbidden_factory,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert opened is False
    assert stdout.getvalue() == ""
    assert "capacity must be positive" in stderr.getvalue()
