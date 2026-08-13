"""
TrackFlow Incident Analysis — Business rules and constants.

Centralizes all validation constants, error codes, carrier mappings,
and valid value sets for TrackFlow incident records.
"""

from __future__ import annotations

import re
from typing import Any

# ── Field names ──────────────────────────────────────────────────────────────

FIELD_INCIDENT_ID = "incident_id"
FIELD_DATE = "date"
FIELD_COUNTRY = "country"
FIELD_CUSTOMER_TYPE = "customer_type"
FIELD_TRACKING_NUMBER = "tracking_number"
FIELD_CARRIER = "carrier"
FIELD_CATEGORY = "category"
FIELD_DESCRIPTION = "description"
FIELD_STATUS = "status"
FIELD_CUSTOMER_EMAIL = "customer_email"
FIELD_SATISFACTION_SCORE = "satisfaction_score"

ALL_FIELDS = [
    FIELD_INCIDENT_ID,
    FIELD_DATE,
    FIELD_COUNTRY,
    FIELD_CUSTOMER_TYPE,
    FIELD_TRACKING_NUMBER,
    FIELD_CARRIER,
    FIELD_CATEGORY,
    FIELD_DESCRIPTION,
    FIELD_STATUS,
    FIELD_CUSTOMER_EMAIL,
    FIELD_SATISFACTION_SCORE,
]

# ── Regular expressions ──────────────────────────────────────────────────────

# Incident ID: TRF- followed by exactly 6 digits
INCIDENT_ID_PATTERN = re.compile(r"^TRF-\d{6}$")

# Date: YYYY-MM-DD
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── Valid values ─────────────────────────────────────────────────────────────

VALID_COUNTRIES: frozenset[str] = frozenset({"US", "ES"})

VALID_CUSTOMER_TYPES: frozenset[str] = frozenset({"B2B", "B2C"})

VALID_CATEGORIES: frozenset[str] = frozenset({
    "LOST_PARCEL",
    "DELAYED_DELIVERY",
    "WRONG_ADDRESS",
    "RETURN_REQUEST",
    "DAMAGE",
})

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_DISCARDED = "DISCARDED"

VALID_STATUSES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_CLOSED, STATUS_DISCARDED})

# ── Carrier mappings by country ──────────────────────────────────────────────

CARRIERS_BY_COUNTRY: dict[str, frozenset[str]] = {
    "US": frozenset({"UPS", "FEDEX", "DHL_US"}),
    "ES": frozenset({"MRW", "SEUR", "DHL_ES", "LOCAL_ES"}),
}

ALL_CARRIERS: frozenset[str] = frozenset().union(*CARRIERS_BY_COUNTRY.values())

# ── Validation limits ────────────────────────────────────────────────────────

MIN_TRACKING_LENGTH = 8
MIN_DESCRIPTION_LENGTH = 5

MIN_SATISFACTION_SCORE = 1
MAX_SATISFACTION_SCORE = 5

# ── Error codes ──────────────────────────────────────────────────────────────

# incident_id
MISSING_INCIDENT_ID = "MISSING_INCIDENT_ID"
INVALID_INCIDENT_ID = "INVALID_INCIDENT_ID"

# date
MISSING_DATE = "MISSING_DATE"
INVALID_DATE = "INVALID_DATE"

# country
MISSING_COUNTRY = "MISSING_COUNTRY"
INVALID_COUNTRY = "INVALID_COUNTRY"

# customer_type
MISSING_CUSTOMER_TYPE = "MISSING_CUSTOMER_TYPE"
INVALID_CUSTOMER_TYPE = "INVALID_CUSTOMER_TYPE"

# tracking_number
MISSING_TRACKING_NUMBER = "MISSING_TRACKING_NUMBER"
SHORT_TRACKING_NUMBER = "SHORT_TRACKING_NUMBER"

# carrier
MISSING_CARRIER = "MISSING_CARRIER"
INVALID_CARRIER_FOR_COUNTRY = "INVALID_CARRIER_FOR_COUNTRY"

# category
MISSING_CATEGORY = "MISSING_CATEGORY"
INVALID_CATEGORY = "INVALID_CATEGORY"

# description
MISSING_DESCRIPTION = "MISSING_DESCRIPTION"
SHORT_DESCRIPTION = "SHORT_DESCRIPTION"

# status
MISSING_STATUS = "MISSING_STATUS"
INVALID_STATUS = "INVALID_STATUS"

# customer_email
MISSING_CUSTOMER_EMAIL = "MISSING_CUSTOMER_EMAIL"
INVALID_CUSTOMER_EMAIL = "INVALID_CUSTOMER_EMAIL"

# satisfaction_score
MISSING_SATISFACTION_SCORE_FOR_CLOSED = "MISSING_SATISFACTION_SCORE_FOR_CLOSED"
INVALID_SATISFACTION_SCORE = "INVALID_SATISFACTION_SCORE"


# ── Helper: check if a value is missing ──────────────────────────────────────


def is_missing(value: Any) -> bool:
    """Return True when the value is None or an empty/whitespace string."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False