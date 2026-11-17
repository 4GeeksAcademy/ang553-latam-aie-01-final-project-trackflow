"""Validated runtime settings for RFP intake."""

from __future__ import annotations

import os
from dataclasses import dataclass
import math
from pathlib import Path


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a finite positive number")
    return value


@dataclass(frozen=True)
class RFPSettings:
    artifact_root: Path
    max_upload_bytes: int
    max_pdf_pages: int = 100
    pdf_conversion_timeout_seconds: float = 60.0
    max_attempts: int = 3
    lease_seconds: int = 600
    heartbeat_seconds: int = 60
    retry_backoff_seconds: tuple[int, int] = (30, 120)


def get_rfp_settings() -> RFPSettings:
    settings = RFPSettings(
        artifact_root=Path(os.getenv("RFP_ARTIFACT_ROOT", "data/raw/rfp_intake")),
        max_upload_bytes=_positive_int("RFP_MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
        max_pdf_pages=_positive_int("RFP_MAX_PDF_PAGES", 100),
        pdf_conversion_timeout_seconds=_positive_float(
            "RFP_PDF_CONVERSION_TIMEOUT_SECONDS", 60.0
        ),
        max_attempts=_positive_int("RFP_MAX_ATTEMPTS", 3),
        lease_seconds=_positive_int("RFP_LEASE_SECONDS", 600),
        heartbeat_seconds=_positive_int("RFP_HEARTBEAT_SECONDS", 60),
    )
    if settings.max_attempts != 3:
        raise RuntimeError("RFP_MAX_ATTEMPTS must remain 3 for rfp-intake-v1")
    if settings.heartbeat_seconds >= settings.lease_seconds:
        raise RuntimeError("RFP heartbeat must be shorter than its lease")
    return settings
