"""Run bounded MEM-092 cleanup against the configured PostgreSQL database.

This command is intentionally an operator-triggered maintenance entry point.
It does not provision tables, start a scheduler, or inspect memory content.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TextIO

from sqlmodel import Session

from services.api.agent_memory.service import (
    DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY,
    MemoryStore,
)
from services.api.database import open_db_session


SessionFactory = Callable[[], Session]
StoreFactory = Callable[[], MemoryStore]


class CleanupConfigurationError(RuntimeError):
    """Raised when the maintenance target is not the required PostgreSQL store."""


def execute_cleanup(
    *,
    session_factory: SessionFactory = open_db_session,
    store_factory: StoreFactory = MemoryStore,
    max_active_per_scope_category: int = DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Execute one transactional cleanup pass and return content-free counts."""
    with session_factory() as session:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            raise CleanupConfigurationError(
                "agent memory cleanup requires a PostgreSQL DATABASE_URL"
            )
        return store_factory().expire_and_clean(
            session,
            now=now,
            max_active_per_scope_category=max_active_per_scope_category,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expire stale MEM-092 proposals and entries and enforce the active "
            "entry capacity without deleting audit history."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required acknowledgement that this command mutates the configured database",
    )
    parser.add_argument(
        "--max-active-per-scope-category",
        type=int,
        default=DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY,
        metavar="COUNT",
        help=(
            "maximum active entries per scope/client/category bucket "
            f"(default: {DEFAULT_MAX_ACTIVE_PER_SCOPE_CATEGORY})"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory = open_db_session,
    store_factory: StoreFactory = MemoryStore,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print(
            "Refusing to mutate agent memory without --execute.",
            file=stderr,
        )
        return 2
    if args.max_active_per_scope_category <= 0:
        print(
            "Invalid cleanup configuration: capacity must be positive.",
            file=stderr,
        )
        return 2
    try:
        expired_proposals, expired_entries = execute_cleanup(
            session_factory=session_factory,
            store_factory=store_factory,
            max_active_per_scope_category=args.max_active_per_scope_category,
        )
    except CleanupConfigurationError:
        print(
            "Agent memory cleanup configuration is invalid; no cleanup completed.",
            file=stderr,
        )
        return 2
    except Exception:
        print(
            "Agent memory cleanup failed; inspect sanitized server diagnostics.",
            file=stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "expired_proposals": expired_proposals,
                "expired_entries": expired_entries,
                "max_active_per_scope_category": args.max_active_per_scope_category,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
