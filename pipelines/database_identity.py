"""Effective PostgreSQL database identity checks for dual-engine execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


class DatabaseIsolationError(RuntimeError):
    """Safe failure raised when source/reporting isolation cannot be proven."""


@dataclass(frozen=True)
class PostgreSQLDatabaseIdentity:
    server_address: str
    server_port: int
    database_name: str
    database_oid: int


_IDENTITY_SQL = text(
    "SELECT COALESCE(inet_server_addr()::text, 'local-socket') AS server_address, "
    "inet_server_port() AS server_port, current_database() AS database_name, "
    "(SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid"
)


def _dialect_name(engine: Any) -> str | None:
    return getattr(getattr(engine, "dialect", None), "name", None)


def postgresql_database_identity(engine: Any) -> PostgreSQLDatabaseIdentity:
    """Read identity from the connected server, never from a connection URL."""
    if _dialect_name(engine) != "postgresql":
        raise DatabaseIsolationError("database identity requires PostgreSQL")
    try:
        with engine.connect() as connection:
            row = connection.execute(_IDENTITY_SQL).mappings().one()
        return PostgreSQLDatabaseIdentity(
            server_address=str(row["server_address"]),
            server_port=int(row["server_port"]),
            database_name=str(row["database_name"]),
            database_oid=int(row["database_oid"]),
        )
    except DatabaseIsolationError:
        raise
    except Exception as exc:
        raise DatabaseIsolationError(
            "effective PostgreSQL database identity could not be verified"
        ) from exc


def ensure_distinct_postgresql_databases(source_engine: Any, reporting_engine: Any) -> None:
    """Reject engines that resolve to the same effective PostgreSQL database.

    Lightweight non-SQLAlchemy fakes retain object/URL guards for unit tests.
    Every real PostgreSQL SQLAlchemy engine must pass the live identity query.
    """
    if source_engine is reporting_engine:
        raise DatabaseIsolationError("source and reporting databases must be distinct")

    source_dialect = _dialect_name(source_engine)
    reporting_dialect = _dialect_name(reporting_engine)
    if source_dialect is not None or reporting_dialect is not None:
        if source_dialect != "postgresql" or reporting_dialect != "postgresql":
            raise DatabaseIsolationError("source and reporting databases must be PostgreSQL")
        if postgresql_database_identity(source_engine) == postgresql_database_identity(
            reporting_engine
        ):
            raise DatabaseIsolationError(
                "source and reporting databases must be distinct"
            )
        return

    source_url = getattr(source_engine, "url", None)
    reporting_url = getattr(reporting_engine, "url", None)
    if source_url is not None and reporting_url is not None:
        if str(source_url) == str(reporting_url):
            raise DatabaseIsolationError(
                "source and reporting databases must be distinct"
            )
