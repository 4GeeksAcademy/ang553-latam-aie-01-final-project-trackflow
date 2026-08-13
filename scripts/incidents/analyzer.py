"""
TrackFlow Incident Analyzer — Record validation.

Provides the public function `validate_record` that validates a single
incident record against all business rules defined in the `constants` module.
"""

from __future__ import annotations

from typing import Any

from . import (
    ALL_CARRIERS,
    CARRIERS_BY_COUNTRY,
    DATE_PATTERN,
    FIELD_CARRIER,
    FIELD_CATEGORY,
    FIELD_COUNTRY,
    FIELD_CUSTOMER_EMAIL,
    FIELD_CUSTOMER_TYPE,
    FIELD_DATE,
    FIELD_DESCRIPTION,
    FIELD_INCIDENT_ID,
    FIELD_SATISFACTION_SCORE,
    FIELD_STATUS,
    FIELD_TRACKING_NUMBER,
    INCIDENT_ID_PATTERN,
    INVALID_CARRIER_FOR_COUNTRY,
    INVALID_CATEGORY,
    INVALID_COUNTRY,
    INVALID_CUSTOMER_EMAIL,
    INVALID_CUSTOMER_TYPE,
    INVALID_DATE,
    INVALID_INCIDENT_ID,
    INVALID_SATISFACTION_SCORE,
    INVALID_STATUS,
    MAX_SATISFACTION_SCORE,
    MIN_DESCRIPTION_LENGTH,
    MIN_SATISFACTION_SCORE,
    MIN_TRACKING_LENGTH,
    MISSING_CARRIER,
    MISSING_CATEGORY,
    MISSING_COUNTRY,
    MISSING_CUSTOMER_EMAIL,
    MISSING_CUSTOMER_TYPE,
    MISSING_DATE,
    MISSING_DESCRIPTION,
    MISSING_INCIDENT_ID,
    MISSING_SATISFACTION_SCORE_FOR_CLOSED,
    MISSING_STATUS,
    MISSING_TRACKING_NUMBER,
    SHORT_DESCRIPTION,
    SHORT_TRACKING_NUMBER,
    VALID_CATEGORIES,
    VALID_COUNTRIES,
    VALID_CUSTOMER_TYPES,
    VALID_STATUSES,
    is_missing,
)

__all__ = ["validate_record"]


def _validate_incident_id(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_INCIDENT_ID)
    if is_missing(value):
        errors.append(MISSING_INCIDENT_ID)
        return
    if not INCIDENT_ID_PATTERN.match(str(value)):
        errors.append(INVALID_INCIDENT_ID)


def _validate_date(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_DATE)
    if is_missing(value):
        errors.append(MISSING_DATE)
        return
    if not DATE_PATTERN.match(str(value)):
        errors.append(INVALID_DATE)


def _validate_country(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_COUNTRY)
    if is_missing(value):
        errors.append(MISSING_COUNTRY)
        return
    if value not in VALID_COUNTRIES:
        errors.append(INVALID_COUNTRY)


def _validate_customer_type(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_CUSTOMER_TYPE)
    if is_missing(value):
        errors.append(MISSING_CUSTOMER_TYPE)
        return
    if value not in VALID_CUSTOMER_TYPES:
        errors.append(INVALID_CUSTOMER_TYPE)


def _validate_tracking_number(
    record: dict[str, Any], errors: list[str]
) -> None:
    value = record.get(FIELD_TRACKING_NUMBER)
    if is_missing(value):
        errors.append(MISSING_TRACKING_NUMBER)
        return
    if len(str(value)) < MIN_TRACKING_LENGTH:
        errors.append(SHORT_TRACKING_NUMBER)


def _validate_carrier(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_CARRIER)
    if is_missing(value):
        errors.append(MISSING_CARRIER)
        return

    country = record.get(FIELD_COUNTRY)
    # Only validate carrier-vs-country if we have a valid country
    if country in CARRIERS_BY_COUNTRY:
        valid_carriers = CARRIERS_BY_COUNTRY[country]
        if value not in valid_carriers:
            errors.append(INVALID_CARRIER_FOR_COUNTRY)
    else:
        # Country is missing or invalid — check carrier against all known
        if value not in ALL_CARRIERS:
            errors.append(INVALID_CARRIER_FOR_COUNTRY)


def _validate_category(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_CATEGORY)
    if is_missing(value):
        errors.append(MISSING_CATEGORY)
        return
    if value not in VALID_CATEGORIES:
        errors.append(INVALID_CATEGORY)


def _validate_description(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_DESCRIPTION)
    if is_missing(value):
        errors.append(MISSING_DESCRIPTION)
        return
    if len(str(value)) < MIN_DESCRIPTION_LENGTH:
        errors.append(SHORT_DESCRIPTION)


def _validate_status(record: dict[str, Any], errors: list[str]) -> None:
    value = record.get(FIELD_STATUS)
    if is_missing(value):
        errors.append(MISSING_STATUS)
        return
    if value not in VALID_STATUSES:
        errors.append(INVALID_STATUS)


def _validate_customer_email(
    record: dict[str, Any], errors: list[str]
) -> None:
    value = record.get(FIELD_CUSTOMER_EMAIL)
    if is_missing(value):
        errors.append(MISSING_CUSTOMER_EMAIL)
        return
    if "@" not in str(value):
        errors.append(INVALID_CUSTOMER_EMAIL)


def _validate_satisfaction_score(
    record: dict[str, Any], errors: list[str]
) -> None:
    value = record.get(FIELD_SATISFACTION_SCORE)
    status = record.get(FIELD_STATUS)

    # CLOSED status requires a score
    if status == "CLOSED" and is_missing(value):
        errors.append(MISSING_SATISFACTION_SCORE_FOR_CLOSED)
        return

    # If present, validate range (even if status is not CLOSED)
    if not is_missing(value):
        try:
            score = int(value)
            if score < MIN_SATISFACTION_SCORE or score > MAX_SATISFACTION_SCORE:
                errors.append(INVALID_SATISFACTION_SCORE)
        except (ValueError, TypeError):
            errors.append(INVALID_SATISFACTION_SCORE)


# ── Public API ───────────────────────────────────────────────────────────────


def validate_record(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validate a single TrackFlow incident record against all business rules.

    Parameters
    ----------
    record : dict[str, Any]
        A dictionary representing one incident row. Keys should match the
        field constants (e.g. ``"incident_id"``, ``"date"``, …).

    Returns
    -------
    tuple[bool, list[str]]
        A tuple of ``(is_valid, error_codes)`` where:
        - ``is_valid`` is ``True`` when no errors were found.
        - ``error_codes`` is a list of string constants identifying each
          validation failure. Empty list when the record is valid.

    Notes
    -----
    - Multiple errors can be returned for a single record.
    - Sensitive fields (e.g. ``customer_email``) are never included in
      error messages — only abstract error codes are returned.
    """
    errors: list[str] = []

    _validate_incident_id(record, errors)
    _validate_date(record, errors)
    _validate_country(record, errors)
    _validate_customer_type(record, errors)
    _validate_tracking_number(record, errors)
    _validate_carrier(record, errors)
    _validate_category(record, errors)
    _validate_description(record, errors)
    _validate_status(record, errors)
    _validate_customer_email(record, errors)
    _validate_satisfaction_score(record, errors)

    return (len(errors) == 0, errors)