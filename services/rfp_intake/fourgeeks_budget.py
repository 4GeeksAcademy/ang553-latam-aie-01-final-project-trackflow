"""QA-only durable request budget for the official RFP intake E2E.

The ledger uses atomic, exclusive file creation on the already-persistent RFP
artifact volume.  A slot is consumed before the provider is invoked and is
never returned, including when the network outcome is uncertain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import UUID


_AUTHORIZATION_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED"
_ENVIRONMENT_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT"
_BUDGET_ID_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID"
_JOB_ID_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID"
_PDF_SHA256_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256"
_MAX_REQUESTS_ENV = "TRACKFLOW_RFP_INTAKE_FOURGEEKS_MAX_REQUESTS"
_DEFAULT_MAX_REQUESTS = 5
_ABSOLUTE_MAX_REQUESTS = 10
_MANIFEST_NAME = "budget.json"
_BUDGET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")


class IntakeFourGeeksBudgetError(RuntimeError):
    """The isolated QA budget is invalid, mismatched, or exhausted."""


@dataclass(frozen=True, slots=True)
class IntakeFourGeeksBudgetSettings:
    budget_id: str
    allowed_job_id: UUID
    expected_pdf_sha256: str
    artifact_root: Path
    maximum_requests: int = _DEFAULT_MAX_REQUESTS


def load_intake_fourgeeks_budget_settings(
    *,
    artifact_root: Path,
    values: Mapping[str, str] | None = None,
) -> IntakeFourGeeksBudgetSettings | None:
    """Load the explicit QA authorization; absence leaves ordinary runs unchanged."""

    source = os.environ if values is None else values
    authorization = source.get(_AUTHORIZATION_ENV, "").strip()
    if not authorization:
        return None
    if authorization != "YES":
        raise IntakeFourGeeksBudgetError("intake request budget authorization is invalid")
    if source.get(_ENVIRONMENT_ENV, "").strip().casefold() != "qa":
        raise IntakeFourGeeksBudgetError("intake request budget requires QA")
    if source.get("RFP_RESPONSE_LLM_MODE", "deterministic").strip().casefold() != "deterministic":
        raise IntakeFourGeeksBudgetError("Parte 2 must remain deterministic")

    budget_id = source.get(_BUDGET_ID_ENV, "").strip().casefold()
    if not _BUDGET_ID_PATTERN.fullmatch(budget_id):
        raise IntakeFourGeeksBudgetError("intake request budget id is invalid")
    try:
        allowed_job_id = UUID(source.get(_JOB_ID_ENV, "").strip())
    except (ValueError, AttributeError) as error:
        raise IntakeFourGeeksBudgetError("intake request budget job id is invalid") from error
    expected_pdf_sha256 = source.get(_PDF_SHA256_ENV, "").strip().casefold()
    if not _SHA256_PATTERN.fullmatch(expected_pdf_sha256):
        raise IntakeFourGeeksBudgetError("intake request budget PDF hash is invalid")
    if _MAX_REQUESTS_ENV not in source:
        maximum_requests = _DEFAULT_MAX_REQUESTS
    else:
        raw_maximum = source.get(_MAX_REQUESTS_ENV, "").strip()
        if not _POSITIVE_INTEGER_PATTERN.fullmatch(raw_maximum):
            raise IntakeFourGeeksBudgetError(
                "intake request budget maximum is invalid"
            )
        maximum_requests = int(raw_maximum)
        if maximum_requests > _ABSOLUTE_MAX_REQUESTS:
            raise IntakeFourGeeksBudgetError(
                "intake request budget maximum exceeds the QA limit"
            )
    return IntakeFourGeeksBudgetSettings(
        budget_id=budget_id,
        allowed_job_id=allowed_job_id,
        expected_pdf_sha256=expected_pdf_sha256,
        artifact_root=artifact_root,
        maximum_requests=maximum_requests,
    )


class IntakeFourGeeksRequestLedger:
    """Consume a bounded number of durable slots for one authorized job and PDF."""

    def __init__(
        self,
        settings: IntakeFourGeeksBudgetSettings,
        *,
        job_id: UUID,
        ticket_id: UUID,
        actual_pdf_sha256: str,
    ) -> None:
        actual_hash = actual_pdf_sha256.strip().casefold()
        if (
            isinstance(settings.maximum_requests, bool)
            or not isinstance(settings.maximum_requests, int)
            or not 1 <= settings.maximum_requests <= _ABSOLUTE_MAX_REQUESTS
        ):
            raise IntakeFourGeeksBudgetError(
                "intake request budget maximum is invalid"
            )
        if job_id != settings.allowed_job_id:
            raise IntakeFourGeeksBudgetError("intake job is not authorized for this budget")
        if actual_hash != settings.expected_pdf_sha256:
            raise IntakeFourGeeksBudgetError("intake PDF is not authorized for this budget")
        self._settings = settings
        self._job_id = job_id
        self._ticket_id = ticket_id
        self._pdf_sha256 = actual_hash
        self._directory, self._maximum_requests = self._resolve_directory()

    def _manifest_record(self) -> dict[str, object]:
        return {
            "contract_version": "rfp-intake-fourgeeks-budget-v2",
            "budget_id": self._settings.budget_id,
            "job_id": str(self._job_id),
            "pdf_sha256": self._pdf_sha256,
            "maximum_requests": self._settings.maximum_requests,
        }

    def _read_manifest(self, manifest: Path) -> int:
        if manifest.is_symlink():
            raise IntakeFourGeeksBudgetError("intake request budget manifest is unsafe")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise IntakeFourGeeksBudgetError(
                "intake request budget manifest is invalid"
            ) from error
        expected = self._manifest_record()
        if not isinstance(payload, dict) or set(payload) != set(expected):
            raise IntakeFourGeeksBudgetError(
                "intake request budget manifest is invalid"
            )
        if payload != expected:
            raise IntakeFourGeeksBudgetError(
                "intake request budget binding cannot be changed"
            )
        maximum = payload.get("maximum_requests")
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise IntakeFourGeeksBudgetError(
                "intake request budget manifest is invalid"
            )
        return maximum

    def _create_or_verify_manifest(self, manifest: Path) -> int:
        record = json.dumps(
            self._manifest_record(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(manifest, flags, 0o600)
        except FileExistsError:
            return self._read_manifest(manifest)
        try:
            os.write(descriptor, record)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return self._settings.maximum_requests

    def _resolve_directory(self) -> tuple[Path, int]:
        root = self._settings.artifact_root.resolve()
        budget_root = root / ".qa-fourgeeks-budgets"
        directory = budget_root / self._settings.budget_id
        budget_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if budget_root.is_symlink() or directory.is_symlink():
            raise IntakeFourGeeksBudgetError("intake request budget path is unsafe")
        directory.mkdir(mode=0o700, exist_ok=True)
        resolved = directory.resolve()
        if root not in resolved.parents:
            raise IntakeFourGeeksBudgetError("intake request budget path escaped artifact root")
        manifest = resolved / _MANIFEST_NAME
        if manifest.exists() or manifest.is_symlink():
            return resolved, self._read_manifest(manifest)

        # V1 ledgers predate immutable manifests. Preserve them byte-for-byte and
        # permanently retain their original five-request ceiling.
        if any(resolved.glob("request-*.json")):
            if self._settings.maximum_requests != _DEFAULT_MAX_REQUESTS:
                raise IntakeFourGeeksBudgetError(
                    "legacy intake request budget cannot be expanded"
                )
            return resolved, _DEFAULT_MAX_REQUESTS
        return resolved, self._create_or_verify_manifest(manifest)

    def reserve(self, *, operation: str) -> int:
        """Durably consume one slot before any external request is attempted."""

        safe_operation = re.sub(r"[^a-zA-Z0-9_.-]", "_", operation)[:100]
        record = json.dumps(
            {
                "contract_version": "rfp-intake-fourgeeks-budget-v1",
                "budget_id": self._settings.budget_id,
                "job_id": str(self._job_id),
                "ticket_id": str(self._ticket_id),
                "pdf_sha256": self._pdf_sha256,
                "operation": safe_operation,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        for index in range(1, self._maximum_requests + 1):
            slot = self._directory / f"request-{index:02d}.json"
            try:
                descriptor = os.open(
                    slot,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            try:
                os.write(descriptor, record)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return index
        raise IntakeFourGeeksBudgetError("intake FourGeeks request budget exhausted")


class BudgetedIntakeFourGeeksGateway:
    """Reserve a durable request slot immediately before gateway delegation."""

    def __init__(self, delegate: Any, ledger: IntakeFourGeeksRequestLedger) -> None:
        self._delegate = delegate
        self._ledger = ledger

    @property
    def last_generation_response_observation(self) -> Any:
        return getattr(self._delegate, "last_generation_response_observation", None)

    @property
    def last_generation_request_id(self) -> str | None:
        return getattr(self._delegate, "last_generation_request_id", None)

    @property
    def last_generation_usage(self) -> Mapping[str, int | float]:
        value = getattr(self._delegate, "last_generation_usage", None)
        return value if isinstance(value, Mapping) else {}

    def generate_structured(self, **kwargs: Any) -> str:
        self._ledger.reserve(operation=str(kwargs.get("schema_name", "structured_generation")))
        return self._delegate.generate_structured(**kwargs)
