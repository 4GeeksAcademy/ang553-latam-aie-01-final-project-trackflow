"""One-request QA diagnostic for the real FourGeeks RFP intake boundary.

The command is fail-closed and content-free.  Without ``--execute`` it only
validates configuration, the official ModaViva artifacts and a fresh durable
one-slot budget.  It never creates tickets, starts Celery or writes business
data.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

from data.pipelines.rfp_intake.analyzer import (
    FourGeeksRFPAnalyzer,
    ProviderResponseDiagnostic,
    StructuredOutputError,
    structured_generation_context,
)
from data.pipelines.rfp_intake.graph import RFPIntakeGraph
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import (
    DEFAULT_BASE_URL,
    DEFAULT_GENERATION_MODEL,
    RAGSettings,
    load_settings,
)
from services.rag.contracts import RAGProviderError
from services.rfp_intake.fourgeeks_budget import (
    BudgetedIntakeFourGeeksGateway,
    IntakeFourGeeksBudgetError,
    IntakeFourGeeksRequestLedger,
    load_intake_fourgeeks_budget_settings,
)


OFFICIAL_PDF_SHA256 = "6e5ccfb2bc31ee846a8a33d853076e79824b09e387b4a8c98530626b59e2f37e"
OFFICIAL_MARKDOWN_SHA256 = "74946a77830c808b7b20983aeb6941dee5f32be5f2c22fa583b5078a529b84d9"
_DIAGNOSTIC_AUTHORIZATION = "TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_EXTERNAL_AUTHORIZED"
_DIAGNOSTIC_ENVIRONMENT = "TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_ENVIRONMENT"
Operation = Literal["classification", "metadata"]
GatewayFactory = Callable[[RAGSettings], Any]


class DiagnosticConfigurationError(RuntimeError):
    """The isolated diagnostic is not safe to run."""


@dataclass(frozen=True, slots=True)
class DiagnosticConfig:
    operation: Operation
    markdown_path: Path
    pdf_path: Path
    ledger_root: Path
    budget_id: str
    job_id: UUID
    ticket_id: UUID


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _budget_values(config: DiagnosticConfig) -> dict[str, str]:
    return {
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED": "YES",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT": "qa",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID": config.budget_id,
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID": str(config.job_id),
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256": OFFICIAL_PDF_SHA256,
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_MAX_REQUESTS": "1",
        "RFP_RESPONSE_LLM_MODE": "deterministic",
    }


def preflight(
    config: DiagnosticConfig,
    values: Mapping[str, str],
    *,
    require_execution_authorization: bool,
) -> tuple[RAGSettings, Any]:
    """Validate exact artifacts/configuration without creating a ledger."""

    if require_execution_authorization:
        if values.get(_DIAGNOSTIC_AUTHORIZATION, "").strip() != "YES":
            raise DiagnosticConfigurationError("diagnostic external authorization is absent")
        if values.get(_DIAGNOSTIC_ENVIRONMENT, "").strip().casefold() != "qa":
            raise DiagnosticConfigurationError("diagnostic execution requires QA")
    if values.get("RFP_RESPONSE_LLM_MODE", "").strip().casefold() != "deterministic":
        raise DiagnosticConfigurationError("Parte 2 must remain deterministic")
    if not config.pdf_path.is_file() or not config.markdown_path.is_file():
        raise DiagnosticConfigurationError("official diagnostic artifacts are unavailable")
    if _sha256(config.pdf_path) != OFFICIAL_PDF_SHA256:
        raise DiagnosticConfigurationError("official PDF hash mismatch")
    if _sha256(config.markdown_path) != OFFICIAL_MARKDOWN_SHA256:
        raise DiagnosticConfigurationError("official Markdown hash mismatch")
    if not config.ledger_root.is_dir() or config.ledger_root.is_symlink():
        raise DiagnosticConfigurationError("diagnostic ledger root is invalid")
    budget_directory = config.ledger_root / ".qa-fourgeeks-budgets" / config.budget_id
    if budget_directory.exists() or budget_directory.is_symlink():
        raise DiagnosticConfigurationError("diagnostic budget id has already been used")

    settings = load_settings(values)
    if settings.base_url != DEFAULT_BASE_URL:
        raise DiagnosticConfigurationError("unexpected FourGeeks endpoint")
    if settings.generation_model != DEFAULT_GENERATION_MODEL:
        raise DiagnosticConfigurationError("unexpected FourGeeks generation model")
    if settings.generation_timeout_seconds != 45:
        raise DiagnosticConfigurationError("diagnostic generation timeout must be 45 seconds")
    if settings.total_budget_seconds != 80:
        raise DiagnosticConfigurationError("diagnostic total budget must be 80 seconds")
    if settings.min_request_interval_seconds != 2:
        raise DiagnosticConfigurationError("diagnostic request pacing must be 2 seconds")

    budget = load_intake_fourgeeks_budget_settings(
        artifact_root=config.ledger_root,
        values=_budget_values(config),
    )
    if budget is None or budget.maximum_requests != 1:
        raise DiagnosticConfigurationError("diagnostic one-request budget is unavailable")
    return settings, budget


def _provider_payload(provider: ProviderResponseDiagnostic) -> dict[str, object]:
    return asdict(provider)


def _run_operation(
    config: DiagnosticConfig,
    analyzer: FourGeeksRFPAnalyzer,
    markdown: str,
) -> None:
    runtime = SimpleNamespace(context=SimpleNamespace(analyzer=analyzer))
    if config.operation == "classification":
        RFPIntakeGraph._classify({"markdown": markdown}, runtime)
    else:
        RFPIntakeGraph._extract_metadata({"markdown": markdown}, runtime)


def execute_diagnostic(
    config: DiagnosticConfig,
    values: Mapping[str, str],
    *,
    gateway_factory: GatewayFactory = FourGeeksHTTPClient,
) -> dict[str, object]:
    """Execute exactly one production structured-generation operation."""

    settings, budget = preflight(
        config, values, require_execution_authorization=True
    )
    ledger = IntakeFourGeeksRequestLedger(
        budget,
        job_id=config.job_id,
        ticket_id=config.ticket_id,
        actual_pdf_sha256=OFFICIAL_PDF_SHA256,
    )
    gateway = gateway_factory(settings)
    analyzer = FourGeeksRFPAnalyzer(
        BudgetedIntakeFourGeeksGateway(gateway, ledger),
        timeout_seconds=settings.generation_timeout_seconds,
    )
    result: dict[str, object]
    try:
        markdown = config.markdown_path.read_text(encoding="utf-8")
        _run_operation(config, analyzer, markdown)
        context = analyzer.diagnostic_context(
            "trackflow_rfp_classification"
            if config.operation == "classification"
            else "trackflow_rfp_metadata"
        )
        result = {
            "outcome": "valid",
            "operation": config.operation,
            "invocation_id": str(context.invocation_id) if context else None,
            "provider": _provider_payload(
                context.provider if context else ProviderResponseDiagnostic()
            ),
        }
    except StructuredOutputError as error:
        diagnostic = error.diagnostic
        result = {
            "outcome": "structured_output_invalid",
            "operation": config.operation,
            "invocation_id": str(diagnostic.invocation_id),
            "category": diagnostic.category,
            "error_type": diagnostic.error_type,
            "path": diagnostic.path,
            "received_type": diagnostic.received_type,
            "violated_constraint": diagnostic.violated_constraint,
            "provider": _provider_payload(diagnostic.provider),
        }
    except RAGProviderError as error:
        context = structured_generation_context(error)
        result = {
            "outcome": "provider_error",
            "operation": config.operation,
            "category": error.category,
            "http_status": error.status_code,
            "invocation_id": str(context.invocation_id) if context else None,
            "provider": _provider_payload(
                context.provider if context else ProviderResponseDiagnostic()
            ),
        }
    finally:
        close = getattr(gateway, "close", None)
        if callable(close):
            close()

    slots = sorted(
        (config.ledger_root / ".qa-fourgeeks-budgets" / config.budget_id).glob(
            "request-*.json"
        )
    )
    if len(slots) != 1:
        raise DiagnosticConfigurationError("diagnostic did not consume exactly one slot")
    result["requests_reserved"] = 1
    result["maximum_requests"] = 1
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("classification", "metadata"), required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--ledger-root", type=Path, required=True)
    parser.add_argument("--budget-id", required=True)
    parser.add_argument("--job-id", type=UUID, required=True)
    parser.add_argument("--ticket-id", type=UUID, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = DiagnosticConfig(
        operation=args.operation,
        markdown_path=args.markdown.resolve(),
        pdf_path=args.pdf.resolve(),
        ledger_root=args.ledger_root.resolve(),
        budget_id=args.budget_id,
        job_id=args.job_id,
        ticket_id=args.ticket_id,
    )
    try:
        preflight(
            config,
            os.environ,
            require_execution_authorization=args.execute,
        )
        if not args.execute:
            print(json.dumps({
                "outcome": "preflight_only",
                "operation": config.operation,
                "artifacts_verified": True,
                "maximum_requests": 1,
                "external_request_performed": False,
            }, sort_keys=True))
            return 0
        result = execute_diagnostic(config, os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["outcome"] == "valid" else 2
    except (DiagnosticConfigurationError, IntakeFourGeeksBudgetError) as error:
        print(json.dumps({
            "outcome": "configuration_rejected",
            "error_type": type(error).__name__,
            "external_request_performed": False,
        }, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
