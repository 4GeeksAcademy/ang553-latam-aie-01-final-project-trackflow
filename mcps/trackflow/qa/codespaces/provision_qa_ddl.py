#!/usr/bin/env python3
"""Provision explicit operational DDL for QA environment.

Applies incident_query_security.sql to ensure incident management tables exist
before seeding. This must run after PostgreSQL is healthy but before seed_trackflow.py.

The script validates:
- QA environment marker is set
- Database is the isolated loopback QA instance
- DDL executes without error
- All four operational tables are created
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2 import sql

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Required environment variable is missing: {name}")
    return value


def main() -> None:
    if os.getenv("TRACKFLOW_MCP_QA") != "1":
        raise SystemExit("Refusing to provision without TRACKFLOW_MCP_QA=1")

    database_url = required("DATABASE_URL")
    parsed = urlparse(database_url.replace("postgresql+psycopg2", "postgresql", 1))

    if parsed.hostname != "127.0.0.1" or parsed.port != 55440:
        raise SystemExit("QA database must be the isolated loopback PostgreSQL instance")

    ddl_path = REPOSITORY_ROOT / "infra" / "sql" / "incident_query_security.sql"
    if not ddl_path.is_file():
        raise SystemExit(f"DDL file not found: {ddl_path}")

    ddl_content = ddl_path.read_text(encoding="utf-8")

    # Connect and apply DDL
    try:
        conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
        )
    except psycopg2.OperationalError as error:
        raise SystemExit(f"Failed to connect to QA database: {error}") from error

    try:
        with conn.cursor() as cursor:
            cursor.execute(ddl_content)
            conn.commit()
    except psycopg2.Error as error:
        conn.rollback()
        raise SystemExit(f"DDL execution failed: {error}") from error
    finally:
        conn.close()

    # Verify all four operational tables were created
    try:
        conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
        )
    except psycopg2.OperationalError as error:
        raise SystemExit(f"Failed to reconnect for verification: {error}") from error

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(%s)"
                ),
                [
                    [
                        "trackflow_client",
                        "user_client_membership",
                        "incident",
                        "incident_history",
                    ]
                ],
            )
            tables = {row[0] for row in cursor.fetchall()}

        expected = {
            "trackflow_client",
            "user_client_membership",
            "incident",
            "incident_history",
        }
        missing = expected - tables

        if missing:
            raise SystemExit(f"DDL verification failed; missing tables: {missing}")

    except psycopg2.Error as error:
        raise SystemExit(f"Table verification query failed: {error}") from error
    finally:
        conn.close()

    print("✓ DDL provisioning complete: 4/4 operational tables created")


if __name__ == "__main__":
    main()
