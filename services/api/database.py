"""
TrackFlow TinyDB storage.

Database lives at ``services/api/data/suppliers.json``.

Exposes:
    ``db`` — the TinyDB instance
    ``suppliers`` — the ``suppliers`` table (``tinydb.table.Table``)
"""

from __future__ import annotations

from pathlib import Path

from tinydb import TinyDB

# ── Database path ────────────────────────────────────────────────────────────
# Resolved relative to this file so it works regardless of the working directory.

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DB_PATH = _DATA_DIR / "suppliers.json"

# ── TinyDB instance & table ──────────────────────────────────────────────────

db = TinyDB(str(_DB_PATH))

suppliers = db.table("suppliers")