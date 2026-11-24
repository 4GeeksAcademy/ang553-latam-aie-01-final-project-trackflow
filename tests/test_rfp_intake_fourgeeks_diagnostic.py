from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from scripts import rfp_intake_fourgeeks_diagnostic as diagnostic
from services.rag.contracts import RAGProviderError
from services.rfp_intake.fourgeeks_budget import (
    IntakeFourGeeksBudgetError,
    IntakeFourGeeksRequestLedger,
    load_intake_fourgeeks_budget_settings,
)


class Gateway:
    def __init__(self, settings: object, raw: str) -> None:
        self.settings = settings
        self.raw = raw
        self.calls = 0
        self.closed = False
        self.last_generation_response_observation = None
        self.last_generation_request_id = None
        self.last_generation_usage: dict[str, int] = {}

    def generate_structured(self, **_kwargs: object) -> str:
        self.calls += 1
        return self.raw

    def close(self) -> None:
        self.closed = True


class TimeoutGateway(Gateway):
    def generate_structured(self, **_kwargs: object) -> str:
        self.calls += 1
        raise RAGProviderError("sanitized", category="timeout")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation: diagnostic.Operation = "classification",
) -> tuple[diagnostic.DiagnosticConfig, dict[str, str]]:
    pdf = tmp_path / "trackflow-request-1.pdf"
    markdown = tmp_path / "source.md"
    ledger = tmp_path / "ledger"
    pdf.write_bytes(b"official-pdf-fixture")
    markdown.write_text("# ModaViva\nTrackFlow proposal request", encoding="utf-8")
    ledger.mkdir()
    monkeypatch.setattr(diagnostic, "OFFICIAL_PDF_SHA256", _hash(pdf))
    monkeypatch.setattr(diagnostic, "OFFICIAL_MARKDOWN_SHA256", _hash(markdown))
    config = diagnostic.DiagnosticConfig(
        operation=operation,
        markdown_path=markdown,
        pdf_path=pdf,
        ledger_root=ledger,
        budget_id=f"diagnostic-{operation}-{uuid4().hex[:12]}",
        job_id=uuid4(),
        ticket_id=uuid4(),
    )
    values = {
        "FOUR_GEEKS_API_KEY": "private-test-secret",
        "QDRANT_URL": "http://unused.invalid",
        "RAG_GENERATION_TIMEOUT_SECONDS": "45",
        "RAG_TOTAL_BUDGET_SECONDS": "80",
        "RAG_REQUEST_INTERVAL_SECONDS": "2",
        "RFP_RESPONSE_LLM_MODE": "deterministic",
        "TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_EXTERNAL_AUTHORIZED": "YES",
        "TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_ENVIRONMENT": "qa",
    }
    return config, values


def test_preflight_does_not_create_ledger_or_call_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)

    _, budget = diagnostic.preflight(
        config, values, require_execution_authorization=False
    )

    assert budget.maximum_requests == 1
    assert not (config.ledger_root / ".qa-fourgeeks-budgets").exists()


def test_classification_uses_one_durable_request_and_second_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)
    raw = json.dumps({
        "is_valid_rfp": True,
        "reason_code": "legitimate_rfp",
        "evidence_refs": [{"line_start": 1, "line_end": 2}],
    })
    created: list[Gateway] = []

    def factory(settings: object) -> Gateway:
        gateway = Gateway(settings, raw)
        created.append(gateway)
        return gateway

    result = diagnostic.execute_diagnostic(config, values, gateway_factory=factory)

    assert result["outcome"] == "valid"
    assert result["requests_reserved"] == 1
    assert result["maximum_requests"] == 1
    assert created[0].calls == 1
    assert created[0].closed is True

    settings = load_intake_fourgeeks_budget_settings(
        artifact_root=config.ledger_root,
        values=diagnostic._budget_values(config),
    )
    assert settings is not None
    ledger = IntakeFourGeeksRequestLedger(
        settings,
        job_id=config.job_id,
        ticket_id=config.ticket_id,
        actual_pdf_sha256=diagnostic.OFFICIAL_PDF_SHA256,
    )
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        ledger.reserve(operation="second-call")


def test_metadata_invokes_current_schema_and_reference_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch, operation="metadata")
    raw = json.dumps({
        "rfp_id": None,
        "client_name": "ModaViva",
        "client_country_code": "ES",
        "client_country_original": "Spain",
        "operation_country_codes": ["ES"],
        "services_requested": [
            {
                "service_id": "warehouse",
                "requested": True,
                "source_ref": {"line_start": 1, "line_end": 2},
            }
        ],
        "monthly_volume_value": None,
        "monthly_volume_unit": None,
        "monthly_volume_source_ref": None,
        "deadline": None,
        "deadline_source_ref": None,
        "onboarding_target": None,
        "onboarding_source_ref": None,
        "budget_range": None,
        "open_questions": [],
    })
    gateway: Gateway | None = None

    def factory(settings: object) -> Gateway:
        nonlocal gateway
        gateway = Gateway(settings, raw)
        return gateway

    result = diagnostic.execute_diagnostic(config, values, gateway_factory=factory)

    assert result["outcome"] == "valid"
    assert gateway is not None and gateway.calls == 1


def test_invalid_structured_output_reports_only_content_free_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)
    sensitive_raw = '{"private":"customer-value"}'

    result = diagnostic.execute_diagnostic(
        config,
        values,
        gateway_factory=lambda settings: Gateway(settings, sensitive_raw),
    )
    encoded = json.dumps(result, sort_keys=True)

    assert result["outcome"] == "structured_output_invalid"
    assert result["category"] == "pydantic_validation"
    assert result["path"] == "private"
    assert result["error_type"] == "extra_forbidden"
    assert "customer-value" not in encoded
    assert "private-test-secret" not in encoded
    assert sensitive_raw not in encoded


def test_timeout_is_distinguished_without_response_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)

    result = diagnostic.execute_diagnostic(
        config,
        values,
        gateway_factory=lambda settings: TimeoutGateway(settings, ""),
    )

    assert result["outcome"] == "provider_error"
    assert result["category"] == "timeout"
    assert result["requests_reserved"] == 1


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_EXTERNAL_AUTHORIZED", ""),
        ("TRACKFLOW_RFP_INTAKE_DIAGNOSTIC_ENVIRONMENT", "prod"),
        ("RFP_RESPONSE_LLM_MODE", "fourgeeks"),
    ],
)
def test_execution_preflight_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    config, values = _case(tmp_path, monkeypatch)
    values[name] = value

    with pytest.raises(diagnostic.DiagnosticConfigurationError):
        diagnostic.preflight(
            config, values, require_execution_authorization=True
        )
    assert not (config.ledger_root / ".qa-fourgeeks-budgets").exists()


def test_reused_budget_id_is_rejected_before_gateway_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)
    directory = (
        config.ledger_root / ".qa-fourgeeks-budgets" / config.budget_id
    )
    directory.mkdir(parents=True)

    with pytest.raises(
        diagnostic.DiagnosticConfigurationError, match="already been used"
    ):
        diagnostic.preflight(
            config, values, require_execution_authorization=True
        )


def test_official_hash_mismatch_fails_before_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _case(tmp_path, monkeypatch)
    config.markdown_path.write_text("changed", encoding="utf-8")

    with pytest.raises(diagnostic.DiagnosticConfigurationError, match="hash mismatch"):
        diagnostic.preflight(
            config, values, require_execution_authorization=True
        )
    assert not (config.ledger_root / ".qa-fourgeeks-budgets").exists()
