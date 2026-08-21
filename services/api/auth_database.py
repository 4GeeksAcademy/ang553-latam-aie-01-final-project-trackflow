"""
TrackFlow Auth TinyDB storage.

Separate database from suppliers — lives at ``services/api/data/auth.json``.

Exposes:
    ``auth_db`` — the TinyDB instance for authentication data
    ``users`` — the ``users`` table
    ``profiles`` — the ``profiles`` table
"""

from __future__ import annotations

from pathlib import Path

from tinydb import TinyDB

# ── Database path ────────────────────────────────────────────────────────────
# Same data directory as suppliers, but different file.

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_AUTH_DB_PATH = _DATA_DIR / "auth.json"

# ── TinyDB instance & tables ─────────────────────────────────────────────────

auth_db = TinyDB(str(_AUTH_DB_PATH))

users = auth_db.table("users")
profiles = auth_db.table("profiles")

# ── Password-reset tokens table ─────────────────────────────────────────────
password_reset_tokens = auth_db.table("password_reset_tokens")