#!/usr/bin/env python3
"""Add and backfill the transitional ``sku.client_id`` column.

The migration is deliberately explicit and non-destructive. Unknown legacy
client names are reported and remain nullable for a later domain decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from services.api.inventory_clients import CLIENT_IDS_BY_NAME
from services.api.inventory_models import SKU

TABLE_NAME = "sku"
COLUMN_NAME = "client_id"


@dataclass(frozen=True)
class ClientIdMismatch:
    """A non-null legacy ID that conflicts with the canonical mapping."""

    sku_id: int
    client_name: str
    existing_client_id: str
    expected_client_id: str


@dataclass
class MigrationReport:
    """Non-sensitive operator report produced by the backfill."""

    unknown_clients: list[tuple[int, str]] = field(default_factory=list)
    mismatched_client_ids: list[ClientIdMismatch] = field(default_factory=list)

    @property
    def has_unresolved_rows(self) -> bool:
        """Whether operator intervention is required."""
        return bool(self.unknown_clients or self.mismatched_client_ids)


def ensure_client_id_column(engine) -> bool:
    """Add the nullable column when absent; return whether it was added."""
    inspector = inspect(engine)
    if not inspector.has_table(TABLE_NAME):
        raise RuntimeError("Table 'sku' does not exist; initialize inventory first.")
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if COLUMN_NAME in columns:
        return False
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE sku ADD COLUMN client_id VARCHAR NULL"))
    return True


def backfill_client_ids(engine) -> MigrationReport:
    """Backfill only null IDs and report unresolved legacy rows."""
    report = MigrationReport()
    with Session(engine) as session:
        rows = session.exec(select(SKU)).all()
        for sku in rows:
            expected_id = CLIENT_IDS_BY_NAME.get(sku.client_name)
            if expected_id is None:
                if sku.id is not None:
                    report.unknown_clients.append((sku.id, sku.client_name))
                continue
            if sku.client_id is None:
                sku.client_id = expected_id
                session.add(sku)
                continue
            if sku.client_id != expected_id and sku.id is not None:
                report.mismatched_client_ids.append(
                    ClientIdMismatch(
                        sku_id=sku.id,
                        client_name=sku.client_name,
                        existing_client_id=sku.client_id,
                        expected_client_id=expected_id,
                    )
                )
        session.commit()
    return report


def migrate(engine) -> MigrationReport:
    """Ensure the column and perform an idempotent canonical backfill."""
    added = ensure_client_id_column(engine)
    report = backfill_client_ids(engine)
    print(f"client_id column: {'added' if added else 'already present'}")
    print(f"Unknown legacy clients: {len(report.unknown_clients)}")
    for sku_id, client_name in report.unknown_clients:
        print(f"UNKNOWN LEGACY CLIENT: sku_id={sku_id}, client_name={client_name!r}")
    print(f"Client ID mismatches: {len(report.mismatched_client_ids)}")
    for mismatch in report.mismatched_client_ids:
        print(
            "CLIENT ID MISMATCH: "
            f"sku_id={mismatch.sku_id}, "
            f"client_name={mismatch.client_name!r}, "
            f"existing_client_id={mismatch.existing_client_id!r}, "
            f"expected_client_id={mismatch.expected_client_id!r}"
        )
    return report


def migration_exit_code(report: MigrationReport) -> int:
    """Return zero only when no unresolved legacy rows remain."""
    return 2 if report.has_unresolved_rows else 0


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL environment variable is required.")
    from services.api import inventory_models  # noqa: F401

    engine = create_engine(database_url)
    return migration_exit_code(migrate(engine))


if __name__ == "__main__":
    raise SystemExit(main())
