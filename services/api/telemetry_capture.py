"""Best-effort internal telemetry capture sink."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from services.api.telemetry_schemas import TelemetryEvent

logger = logging.getLogger("services.api.routes.telemetry")
logger.setLevel(logging.INFO)
if not logger.handlers:
    telemetry_handler = logging.StreamHandler()
    telemetry_handler.setFormatter(
        logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    )
    logger.addHandler(telemetry_handler)
logger.propagate = False


def capture_telemetry_events(events: Sequence[TelemetryEvent]) -> None:
    """Capture validated telemetry events without network or persistence."""
    logger.info("Received telemetry batch with %d events", len(events))
    for event in events:
        logger.info("Received telemetry event_type=%s", event.event_type)
