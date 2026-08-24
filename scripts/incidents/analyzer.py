"""
TrackFlow Incident Analyzer — Record validation.

Provides the public function `validate_record` that validates a single
incident record against all business rules defined in the `constants` module.
"""

from __future__ import annotations

from typing import Any

import csv
import os
import tempfile

from . import (
    ALL_CARRIERS,
    ALL_FIELDS,
    CARRIERS_BY_COUNTRY,
    CsvLoadError,
    DATE_PATTERN,
    ERROR_LABELS,
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
    STATUS_CLOSED,
    VALID_CATEGORIES,
    VALID_COUNTRIES,
    VALID_CUSTOMER_TYPES,
    VALID_STATUSES,
    is_missing,
)

__all__ = ["validate_record", "analyze_records", "load_csv", "export_results_csv"]


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


# ── Aggregate analysis ───────────────────────────────────────────────────────


def _build_breakdown(
    records: list[dict[str, Any]],
    field: str,
    valid_values: frozenset[str],
) -> dict[str, int]:
    """Count occurrences of each valid value for *field* across *records*."""
    breakdown: dict[str, int] = {v: 0 for v in valid_values}
    for rec in records:
        val = rec.get(field)
        if val in breakdown:
            breakdown[val] += 1
    return breakdown


def _build_score_distribution(records: list[dict[str, Any]]) -> dict[int, int]:
    """Count how many records have each satisfaction score (1-5)."""
    dist: dict[int, int] = {s: 0 for s in range(1, 6)}
    for rec in records:
        raw = rec.get(FIELD_SATISFACTION_SCORE)
        if raw is not None and str(raw).strip() != "":
            try:
                score = int(raw)
                if score >= 1 and score <= 5:
                    dist[score] += 1
            except (ValueError, TypeError):
                pass
    return dist


def analyze_records(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Aggregate-analysis over a collection of TrackFlow incident records.

    Parameters
    ----------
    records : list[dict[str, Any]]
        A list of incident record dictionaries.

    Returns
    -------
    dict[str, Any]
        A serializable structure with the following keys:

        - ``total_records`` — total number of records received.
        - ``valid_records`` — number of records that passed validation.
        - ``invalid_records`` — number of records with at least one error.
        - ``invalid_breakdown`` — ``{error_code: count}`` across all errors
          found in invalid records.
        - ``category_breakdown`` — ``{category: count}`` for valid records.
        - ``status_breakdown`` — ``{status: count}`` for valid records.
        - ``country_breakdown`` — ``{country: count}`` for valid records.
        - ``closed_scored`` — number of valid CLOSED records with a score.
        - ``score_distribution`` — ``{score: count}`` for valid CLOSED records.
        - ``average_satisfaction`` — mean score of valid CLOSED records (2
          decimal places), or ``0.0`` when there are no scored CLOSED records.
    """
    total_records = len(records)
    valid: list[dict[str, Any]] = []
    invalid_count = 0
    invalid_breakdown: dict[str, int] = {}

    for rec in records:
        is_valid, errors = validate_record(rec)
        if is_valid:
            valid.append(rec)
        else:
            invalid_count += 1
            for err in errors:
                invalid_breakdown[err] = invalid_breakdown.get(err, 0) + 1

    # ── Breakdowns on valid records only ──
    category_breakdown = _build_breakdown(valid, FIELD_CATEGORY, VALID_CATEGORIES)
    status_breakdown = _build_breakdown(valid, FIELD_STATUS, VALID_STATUSES)
    country_breakdown = _build_breakdown(valid, FIELD_COUNTRY, VALID_COUNTRIES)

    # ── Satisfaction on valid CLOSED records only ──
    closed_scored_records = [
        rec
        for rec in valid
        if rec.get(FIELD_STATUS) == STATUS_CLOSED
        and not is_missing(rec.get(FIELD_SATISFACTION_SCORE))
    ]

    closed_scored = len(closed_scored_records)
    score_distribution = _build_score_distribution(closed_scored_records)

    if closed_scored > 0:
        total_score = sum(
            int(rec[FIELD_SATISFACTION_SCORE])
            for rec in closed_scored_records
        )
        average_satisfaction = round(total_score / closed_scored, 2)
    else:
        average_satisfaction = 0.0

    return {
        "total_records": total_records,
        "valid_records": len(valid),
        "invalid_records": invalid_count,
        "invalid_breakdown": invalid_breakdown,
        "category_breakdown": category_breakdown,
        "status_breakdown": status_breakdown,
        "country_breakdown": country_breakdown,
        "closed_scored": closed_scored,
        "score_distribution": score_distribution,
        "average_satisfaction": average_satisfaction,
    }


# ── CSV loading ──────────────────────────────────────────────────────────────


def load_csv(file_path: str) -> list[dict[str, str]]:
    """
    Load a TrackFlow CSV file and return its records as a list of dicts.

    The file must be UTF-8 encoded, comma-separated, and include a header
    row containing all required column names (see ``ALL_FIELDS``).

    Parameters
    ----------
    file_path : str
        Path to the CSV file.

    Returns
    -------
    list[dict[str, str]]
        A list of dictionaries, one per data row, with keys matching the
        CSV header columns.

    Raises
    ------
    CsvLoadError
        If the file does not exist, is empty, or is missing required columns.
    """
    if not os.path.isfile(file_path):
        raise CsvLoadError(f"File not found: {file_path}")

    if os.path.getsize(file_path) == 0:
        raise CsvLoadError(f"File is empty: {file_path}")

    try:
        with open(file_path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)

            if reader.fieldnames is None or len(reader.fieldnames) == 0:
                raise CsvLoadError(
                    f"CSV file has no valid header row: {file_path}"
                )

            header = list(reader.fieldnames)
            missing = [col for col in ALL_FIELDS if col not in header]

            if missing:
                raise CsvLoadError(
                    f"Missing required columns: {', '.join(missing)}"
                )

            records = list(reader)
    except UnicodeDecodeError as exc:
        raise CsvLoadError(
            "CSV file is not valid UTF-8. Please ensure the file is encoded as UTF-8."
        ) from exc

    # csv.DictReader yields empty list for an otherwise valid file with no
    # data rows, which is acceptable — return an empty list.
    return records


# ── Results export ───────────────────────────────────────────────────────────


def export_results_csv(result: dict[str, Any], output_path: str) -> None:
    """
    Export aggregate analysis results to a CSV file (one row per metric).

    Parameters
    ----------
    result : dict[str, Any]
        The dictionary returned by ``analyze_records()``.
    output_path : str
        Path where the CSV file will be written.

    Notes
    -----
    The output file contains three columns: ``section``, ``metric``, ``value``.
    No individual records, PII, or sensitive data are ever exported.
    """
    rows: list[tuple[str, str, str]] = []

    # ── General ──
    rows.append(("general", "total_records", str(result["total_records"])))
    rows.append(("general", "valid_records", str(result["valid_records"])))
    rows.append(("general", "invalid_records", str(result["invalid_records"])))

    # ── Invalid breakdown ──
    for code, count in sorted(result["invalid_breakdown"].items()):
        label = ERROR_LABELS.get(code, code)
        rows.append(("invalid", label, str(count)))

    # ── Category ──
    for cat, count in sorted(result["category_breakdown"].items()):
        rows.append(("category", cat, str(count)))

    # ── Status ──
    for st, count in sorted(result["status_breakdown"].items()):
        rows.append(("status", st, str(count)))

    # ── Country ──
    for co, count in sorted(result["country_breakdown"].items()):
        rows.append(("country", co, str(count)))

    # ── Satisfaction ──
    rows.append(("satisfaction", "closed_scored", str(result["closed_scored"])))
    for score in range(1, 6):
        metric = f"score_{score}"
        value = str(result["score_distribution"].get(score, 0))
        rows.append(("satisfaction", metric, value))
    rows.append(
        ("satisfaction", "average_satisfaction", str(result["average_satisfaction"]))
    )

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=os.path.dirname(output_path) or ".",
            prefix=os.path.basename(output_path) + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = f.name
            writer = csv.writer(f)
            writer.writerow(["section", "metric", "value"])
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, output_path)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except (FileNotFoundError, OSError):
                # Cleanup is best-effort; never mask the original failure.
                pass