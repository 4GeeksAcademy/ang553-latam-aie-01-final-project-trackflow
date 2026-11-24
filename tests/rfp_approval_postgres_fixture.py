"""Shared fail-closed PostgreSQL fixture for arbitration and finalization."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from tests.test_rfp_approval_artifact_store_postgres_integration import (
    EXTENSION_MIGRATION,
    HISTORICAL_MIGRATIONS_AND_SEEDS,
)


PREFIX = "TRACKFLOW_RFP_APPROVAL_ARBITRATION_FINALIZATION_"
URL_ENV = PREFIX + "TEST_DATABASE_URL"
AUTHORIZED_ENV = PREFIX + "DDL_DML_AUTHORIZED"
EXPECTED_DATABASE_ENV = PREFIX + "EXPECTED_DATABASE"
EXPECTED_USER_ENV = PREFIX + "EXPECTED_USER"
EXPECTED_PORT_ENV = PREFIX + "EXPECTED_PORT"
EXPECTED_ENVIRONMENT_ENV = PREFIX + "EXPECTED_ENVIRONMENT"
VERIFIED_CONTAINER_ID_ENV = PREFIX + "VERIFIED_CONTAINER_ID"
CONTAINER_VERIFIED_ENV = PREFIX + "CONTAINER_VERIFIED"
SCHEMA_ISOLATION_ENV = PREFIX + "SCHEMA_ISOLATION_AUTHORIZED"
DATABASE_PREFIX = "trackflow_rfp_approval_arbitration_finalization_test_"
USER_PREFIX = "trackflow_rfp_approval_arb_final_"
SCHEMA_PREFIX = "rfp_arb_final_test_"
SKIP_REASON = "isolated arbitration/finalization PostgreSQL gate is not authorized"
MONTHLY_STORAGE_VOLUME_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "infra/sql/rfp_approval_monthly_storage_volume.sql"
)
INDICATIVE_VALIDATION_BUNDLE_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "infra/sql/rfp_approval_indicative_validation_bundle.sql"
)
LIFECYCLE_MIGRATIONS = tuple(
    migration.name for migration, _seed_function in HISTORICAL_MIGRATIONS_AND_SEEDS
) + (
    EXTENSION_MIGRATION.name,
    MONTHLY_STORAGE_VOLUME_MIGRATION.name,
    INDICATIVE_VALIDATION_BUNDLE_MIGRATION.name,
)


class SchemaIsolationError(RuntimeError):
    pass


def guardrail_reason(environment: dict[str, str] | None = None) -> str | None:
    values = os.environ if environment is None else environment
    url = values.get(URL_ENV)
    required = {
        AUTHORIZED_ENV: "YES",
        EXPECTED_ENVIRONMENT_ENV: "qa",
        CONTAINER_VERIFIED_ENV: "YES",
        SCHEMA_ISOLATION_ENV: "YES",
    }
    if not url or any(values.get(key) != expected for key, expected in required.items()):
        return SKIP_REASON
    expected_database = values.get(EXPECTED_DATABASE_ENV)
    expected_user = values.get(EXPECTED_USER_ENV)
    expected_port = values.get(EXPECTED_PORT_ENV)
    container_id = values.get(VERIFIED_CONTAINER_ID_ENV)
    if not all((expected_database, expected_user, expected_port, container_id)):
        return SKIP_REASON
    try:
        parsed = make_url(url)
        port = int(expected_port)
    except (ValueError, TypeError):
        return "invalid PostgreSQL guardrail configuration"
    if not parsed.drivername.startswith("postgresql"):
        return "test URL is not PostgreSQL"
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        return "PostgreSQL host is not loopback"
    if (parsed.database, parsed.username, parsed.port) != (
        expected_database,
        expected_user,
        port,
    ):
        return "PostgreSQL target identity mismatch"
    if not expected_database.startswith(DATABASE_PREFIX):
        return "database does not use the disposable gate prefix"
    if not expected_user.startswith(USER_PREFIX):
        return "user does not use the isolated QA prefix"
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        return "container identity was not externally verified"
    return None


def _quoted_schema(schema: str) -> str:
    if not re.fullmatch(rf"{SCHEMA_PREFIX}[a-f0-9]{{32}}", schema):
        raise SchemaIsolationError("unsafe schema name")
    return f'"{schema}"'


def _schema_exists(admin: Engine, schema: str) -> bool:
    with admin.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace "
                    "WHERE nspname=:schema)"
                ),
                {"schema": schema},
            ).scalar_one()
        )


def _create_schema(admin: Engine, schema: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {_quoted_schema(schema)}")


def _run_migration(admin: Engine, schema: str, path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SchemaIsolationError(f"missing migration: {path.name}")
    with admin.connect().execution_options(
        isolation_level="AUTOCOMMIT", no_parameters=True
    ) as connection:
        connection.exec_driver_sql(f"SET search_path TO {_quoted_schema(schema)}")
        if connection.exec_driver_sql("SELECT current_schema()").scalar_one() != schema:
            raise SchemaIsolationError("exclusive search_path was not selected")
        connection.exec_driver_sql(path.read_text(encoding="utf-8"))
        if connection.exec_driver_sql("SELECT current_schema()").scalar_one() != schema:
            raise SchemaIsolationError("migration changed the exclusive search_path")


def _seed(engine: Engine, schema: str, seed) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if connection.execute(text("SELECT current_schema()")).scalar_one() != schema:
            raise SchemaIsolationError("seed connection selected another schema")
        seed(connection.connection.driver_connection)
        if connection.execute(text("SELECT current_schema()")).scalar_one() != schema:
            raise SchemaIsolationError("seed changed the exclusive search_path")


def _drop_schema(admin: Engine, schema: str) -> None:
    with admin.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql(f"DROP SCHEMA {_quoted_schema(schema)} CASCADE")
    if _schema_exists(admin, schema):
        raise SchemaIsolationError(f"schema cleanup was not confirmed: {schema}")


@contextmanager
def _lifecycle(admin: Engine, engine: Engine, schema: str) -> Iterator[None]:
    confirmed = False
    try:
        _create_schema(admin, schema)
        if not _schema_exists(admin, schema):
            raise SchemaIsolationError("schema creation was not confirmed")
        confirmed = True
        for migration, seed in HISTORICAL_MIGRATIONS_AND_SEEDS:
            _run_migration(admin, schema, migration)
            _seed(engine, schema, seed)
        _run_migration(admin, schema, EXTENSION_MIGRATION)
        _run_migration(admin, schema, MONTHLY_STORAGE_VOLUME_MIGRATION)
        _run_migration(admin, schema, INDICATIVE_VALIDATION_BUNDLE_MIGRATION)
        yield
    except Exception as primary:
        if confirmed:
            try:
                _drop_schema(admin, schema)
            except Exception as cleanup:
                raise ExceptionGroup(
                    "fixture lifecycle and targeted cleanup failed",
                    [primary, cleanup],
                ) from None
        raise
    else:
        if confirmed:
            _drop_schema(admin, schema)


@pytest.fixture
def isolated_engine() -> Iterator[Engine]:
    reason = guardrail_reason()
    if reason is not None:
        pytest.skip(reason)
    url = os.environ[URL_ENV]
    expected_database = os.environ[EXPECTED_DATABASE_ENV]
    expected_user = os.environ[EXPECTED_USER_ENV]
    admin = create_engine(url, pool_pre_ping=True)
    with admin.connect() as connection:
        database, user, user_tables, residual_schemas = connection.execute(
            text(
                "SELECT current_database(),current_user,"
                "(SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
                "('pg_catalog','information_schema')),"
                "(SELECT count(*) FROM pg_namespace WHERE nspname LIKE :prefix)"
            ),
            {"prefix": f"{SCHEMA_PREFIX}%"},
        ).one()
        assert (database, user, user_tables, residual_schemas) == (
            expected_database,
            expected_user,
            0,
            0,
        )
    schema = f"{SCHEMA_PREFIX}{uuid4().hex}"
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with _lifecycle(admin, engine, schema):
            with engine.connect() as connection:
                assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
            yield engine
    finally:
        engine.dispose()
        admin.dispose()
