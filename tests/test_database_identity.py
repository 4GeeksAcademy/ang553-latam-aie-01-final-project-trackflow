"""Focused unit tests for effective PostgreSQL database isolation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipelines.database_identity import (
    DatabaseIsolationError,
    ensure_distinct_postgresql_databases,
)


class Result:
    def __init__(self, identity):
        self.identity = identity

    def mappings(self):
        return self

    def one(self):
        return self.identity


class Connection:
    def __init__(self, identity):
        self.identity = identity

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return Result(self.identity)


class Engine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, url, *, database_name, database_oid):
        self.url = url
        self.identity = {
            "server_address": "127.0.0.1",
            "server_port": 5432,
            "database_name": database_name,
            "database_oid": database_oid,
        }

    def connect(self):
        return Connection(self.identity)


def test_url_aliases_resolving_to_same_database_are_rejected():
    source = Engine(
        "postgresql://user@localhost/source-alias",
        database_name="trackflow",
        database_oid=16384,
    )
    reporting = Engine(
        "postgresql://other@127.0.0.1/reporting-alias",
        database_name="trackflow",
        database_oid=16384,
    )

    with pytest.raises(DatabaseIsolationError, match="must be distinct"):
        ensure_distinct_postgresql_databases(source, reporting)


def test_two_databases_on_same_postgresql_server_are_allowed():
    source = Engine(
        "postgresql://user@localhost/operational",
        database_name="operational",
        database_oid=16384,
    )
    reporting = Engine(
        "postgresql://user@localhost/reporting",
        database_name="reporting",
        database_oid=16385,
    )

    ensure_distinct_postgresql_databases(source, reporting)
