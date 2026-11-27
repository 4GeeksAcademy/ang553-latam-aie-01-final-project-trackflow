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
import re

_HMAC_KEY_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
_PLACEHOLDER_MARKERS = (
    "changeme",
    "replaceme",
    "yoursecrethere",
    "placeholder",
    "defaultsecret",
)


def _load_jwt_secret(secret: str | None, algorithm: str) -> str:
    if algorithm not in _HMAC_KEY_BYTES:
        raise RuntimeError("JWT_ALGORITHM must be HS256, HS384, or HS512 for local tokens.")
    minimum = _HMAC_KEY_BYTES[algorithm]
    if not secret or secret != secret.strip() or len(secret.encode("utf-8")) < minimum:
        raise RuntimeError(f"JWT_SECRET_KEY must contain at least {minimum} bytes for {algorithm}.")
    normalized = re.sub(r"[^a-z0-9]", "", secret.lower())
    if (
        any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
        or len(set(secret)) < 8
        or secret in (secret + secret)[1:-1]
    ):
        raise RuntimeError("JWT_SECRET_KEY must be a generated secret, not a placeholder.")
    return secret


def _positive_minutes(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a positive integer.") from None
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer.")
    return value


JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_SECRET_KEY = _load_jwt_secret(os.getenv("JWT_SECRET_KEY"), JWT_ALGORITHM)
JWT_EXPIRE_MINUTES = _positive_minutes("JWT_EXPIRE_MINUTES", 60)
RESET_TOKEN_EXPIRE_MINUTES = _positive_minutes("RESET_TOKEN_EXPIRE_MINUTES", 30)

# ── Resend (email provider) ──────────────────────────────────────────────────
# These are optional at import time — the app can start without them,
# but email sending will fail at runtime.

RESEND_API_KEY: str | None = os.getenv("RESEND_API_KEY")
FRONTEND_URL: str | None = os.getenv("FRONTEND_URL")