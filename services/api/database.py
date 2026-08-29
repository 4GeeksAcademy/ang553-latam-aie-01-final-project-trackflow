"""
TrackFlow storage layer.

Coexists:
    * TinyDB for authentication and existing supplier data.
    * SQLModel engine for future PostgreSQL/Supabase inventory.

TinyDB — ``services/api/data/suppliers.json`` and ``services/api/data/auth.json``.

SQLModel engine — created lazily on first access so that importing this
module does **not** require a ``DATABASE_URL`` to be set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

from sqlmodel import SQLModel, Session, create_engine
from tinydb import TinyDB

# ── TinyDB path ──────────────────────────────────────────────────────────────
# Resolved relative to this file so it works regardless of the working directory.

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DB_PATH = _DATA_DIR / "suppliers.json"

# ── TinyDB instance & table ──────────────────────────────────────────────────

db = TinyDB(str(_DB_PATH))

suppliers = db.table("suppliers")

# ── SQLModel engine (lazy) ───────────────────────────────────────────────────
# The engine is not created at import time.  ``_get_engine()`` builds it on
# first call and caches the result.  This lets the rest of the application
# start without requiring a PostgreSQL ``DATABASE_URL`` — the error is
# deferred to the code path that actually needs the engine.

_engine = None


def _get_engine():
    """Return the cached SQLModel engine, creating it if necessary.

    Raises
    ------
    RuntimeError
        If ``DATABASE_URL`` is not set in the environment.
    """
    global _engine

    if _engine is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL environment variable is required for "
                "PostgreSQL/SQLModel operations but was not set."
            )
        _engine = create_engine(database_url)

    return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a SQLModel ``Session``.

    Usage in a router::

        from services.api.database import get_db
        from sqlmodel import Session

        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...

    One session is created per request and closed automatically when the
    request finishes.
    """
    with Session(_get_engine()) as session:
        yield session


def create_db_and_tables() -> None:
    """Create all registered SQLModel metadata tables.

    Call this explicitly once, before the application serves traffic,
    after all SQLModel models have been imported::

        from services.api.database import create_db_and_tables

        create_db_and_tables()

    Inventory models (``services.api.inventory_models``) are imported
    here explicitly to guarantee they are registered in
    ``SQLModel.metadata`` before ``create_all`` runs.
    """
    # Import inventory models so their tables are registered in metadata
    # before create_all() is called.  This deferred import avoids requiring
    # DATABASE_URL at module-import time.
    import services.api.inventory_models  # noqa: F401

    SQLModel.metadata.create_all(_get_engine())