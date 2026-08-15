"""
TrackFlow Auth JWT configuration — loaded from environment variables.

Reads ``JWT_SECRET_KEY``, ``JWT_ALGORITHM``, and ``JWT_EXPIRE_MINUTES``
from ``os.environ``.  ``JWT_SECRET_KEY`` **must** be set — there is no
hardcoded fallback.  The application will raise ``RuntimeError`` on import
if the variable is missing or empty.
"""

from __future__ import annotations

import os

# ── JWT_SECRET_KEY (required) ────────────────────────────────────────────────
# Must be set in the environment.  No default — fail immediately if missing.

_JWT_SECRET_KEY: str | None = os.getenv("JWT_SECRET_KEY")

if not _JWT_SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY environment variable is required but was not set. "
        "Export it before starting the server, e.g.:\n\n"
        '    export JWT_SECRET_KEY="your-secret-here"\n'
    )

JWT_SECRET_KEY: str = _JWT_SECRET_KEY

# ── JWT_ALGORITHM ────────────────────────────────────────────────────────────

JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

# ── JWT_EXPIRE_MINUTES ───────────────────────────────────────────────────────

_JWT_EXPIRE_DEFAULT = 60

try:
    JWT_EXPIRE_MINUTES: int = int(
        os.getenv("JWT_EXPIRE_MINUTES", str(_JWT_EXPIRE_DEFAULT))
    )
except (TypeError, ValueError):
    JWT_EXPIRE_MINUTES = _JWT_EXPIRE_DEFAULT