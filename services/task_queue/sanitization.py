"""Bounded diagnostics safe for logs, Redis results, and PostgreSQL."""

from __future__ import annotations

import re
from typing import Any

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\b\s*[:=]\s*[^\s,;]+"
)


def safe_code(value: Any, default: str = "internal_error") -> str:
    """Return a stable diagnostic identifier, never arbitrary exception text."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else default


def sanitize_message(value: Any, *, limit: int = 500) -> str:
    """Redact common credential shapes and bound stored/logged text."""
    text_value = str(value).replace("\r", " ").replace("\n", " ")
    text_value = _URL_CREDENTIALS.sub(r"\1[redacted]@", text_value)
    text_value = _BEARER.sub("Bearer [redacted]", text_value)
    text_value = _JWT.sub("[redacted-jwt]", text_value)
    text_value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", text_value)
    return text_value[:limit]


def public_exception(exc: BaseException) -> tuple[str, str]:
    """Classify an exception without exposing its original message."""
    code = safe_code(getattr(exc, "code", None), type(exc).__name__.lower())
    return code, f"operation failed ({type(exc).__name__})"
