"""
TrackFlow Auth configuration — loaded from environment variables.

Reads JWT settings, password-reset token settings, and email-provider
settings from ``os.environ``.

Required variables (will raise ``RuntimeError`` if missing):
    JWT_SECRET_KEY

Optional with sensible defaults:
    JWT_ALGORITHM (default: HS256)
    JWT_EXPIRE_MINUTES (default: 60)
    RESET_TOKEN_EXPIRE_MINUTES (default: 30)

Required for email sending (will warn at import if missing):
    RESEND_API_KEY
    FRONTEND_URL
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

# ── Password-reset token ─────────────────────────────────────────────────────
# Duration in minutes.  The bootcamp requirement is 15–60 minutes.
# 30 minutes is a balanced default.

_RESET_TOKEN_EXPIRE_DEFAULT = 30

try:
    RESET_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("RESET_TOKEN_EXPIRE_MINUTES", str(_RESET_TOKEN_EXPIRE_DEFAULT))
    )
except (TypeError, ValueError):
    RESET_TOKEN_EXPIRE_MINUTES = _RESET_TOKEN_EXPIRE_DEFAULT

# ── Resend (email provider) ──────────────────────────────────────────────────
# These are optional at import time — the app can start without them,
# but email sending will fail at runtime.

RESEND_API_KEY: str | None = os.getenv("RESEND_API_KEY")
FRONTEND_URL: str | None = os.getenv("FRONTEND_URL")