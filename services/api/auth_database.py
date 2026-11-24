"""
TrackFlow Auth TinyDB storage.

Separate database from suppliers — lives at ``services/api/data/auth.json``.

Exposes:
    ``auth_db`` — the TinyDB instance for authentication data
    ``users`` — the ``users`` table
    ``profiles`` — the ``profiles`` table
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from json import JSONDecodeError
from pathlib import Path
from threading import RLock

from tinydb import TinyDB


class AuthStoreUnavailable(RuntimeError):
    """The auth store could not be read or written."""


_auth_lock = RLock()


@contextmanager
def auth_storage_access() -> Iterator[None]:
    """Serialize auth transactions that share a JSONStorage handle in this process.

    The lock is reentrant for nested auth operations. Use one API process per
    auth file and keep offline writers separate.
    """
    with _auth_lock:
        try:
            yield
        except (OSError, JSONDecodeError, UnicodeError):
            raise AuthStoreUnavailable("Authentication storage is temporarily unavailable") from None

# ── Database path ────────────────────────────────────────────────────────────
# Same data directory as suppliers, but different file.

_DEFAULT_AUTH_DB_PATH = Path(__file__).resolve().parent / "data" / "auth.json"
_AUTH_DB_PATH = Path(
    os.getenv("TRACKFLOW_AUTH_DATABASE_PATH", str(_DEFAULT_AUTH_DB_PATH))
).expanduser().resolve()
_AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── TinyDB instance & tables ─────────────────────────────────────────────────

auth_db = TinyDB(str(_AUTH_DB_PATH))

users = auth_db.table("users")
profiles = auth_db.table("profiles")

# ── Password-reset tokens table ─────────────────────────────────────────────
password_reset_tokens = auth_db.table("password_reset_tokens")
