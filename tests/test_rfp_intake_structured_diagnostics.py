from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from data.pipelines.rfp_intake.analyzer import (
    FourGeeksRFPAnalyzer,
    StructuredOutputError,
    structured_generation_context,
)
from data.pipelines.rfp_intake.contracts import (
    DepartmentId,
    RFPMetadata,
    ServiceRequest,
    SourceReference,
)
from data.pipelines.rfp_intake.graph import RFPIntakeGraph
from services.rag.clients import FourGeeksHTTPClient
from services.rag.config import RAGSettings
from services.rag.contracts import RAGProviderError
from services.rfp_intake.tasks import (
    _log_provider_failure,
    _log_structured_output_failure,
)


JOB_ID = UUID("11111111-1111-4111-8111-111111111111")


class Gateway:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    def generate_structured(self, **_kwargs: object) -> str:
        return self.raw


def job(attempt: int) -> dict[str, object]:
    return {"job_id": JOB_ID, "attempt_count": attempt}


@pytest.mark.parametrize(
    ("raw", "category", "error_type", "path", "received_type"),
    [
        ("not-json", "json_invalid", "json_invalid", "root", "string"),
        (
            '{"is_valid_rfp":true,"evidence_refs":[{"line_start":1,"line_end":1}]}',
            "pydantic_validation",
            "missing",
            "reason_code",
            "object",
        ),
        (
            '{"is_valid_rfp":"yes","reason_code":"legitimate_rfp",'
            '"evidence_refs":[{"line_start":1,"line_end":1}]}',
            "pydantic_validation",
            "bool_type",
            "is_valid_rfp",
            "string",
        ),
        (
            '{"is_valid_rfp":true,"reason_code":"legitimate_rfp",'
            '"evidence_refs":[{"line_start":1,"line_end":1}],"private":"value"}',
            "pydantic_validation",
            "extra_forbidden",
            "private",
            "string",
        ),
        (
            '{"is_valid_rfp":true,"reason_code":"vendor_pitch",'
            '"evidence_refs":[{"line_start":1,"line_end":1}]}',
            "pydantic_validation",
            "value_error",
            "root",
            "object",
        ),
    ],
)
def test_classification_diagnostic_identifies_json_and_pydantic_failure_without_values(
    raw: str,
    category: str,
    error_type: str,
    path: str,
    received_type: str,
) -> None:
    with pytest.raises(StructuredOutputError) as captured:
        FourGeeksRFPAnalyzer(Gateway(raw)).classify("private prompt")

    diagnostic = captured.value.diagnostic
    assert diagnostic.operation == "trackflow_rfp_classification"
    assert diagnostic.category == category
    assert diagnostic.error_type == error_type
    assert diagnostic.path == path
    assert diagnostic.received_type == received_type
    assert raw not in repr(diagnostic)
    assert "private prompt" not in repr(diagnostic)


def test_ambiguous_classification_is_a_distinct_semantic_diagnostic() -> None:
    raw = json.dumps(
        {
            "is_valid_rfp": False,
            "reason_code": "ambiguous",
            "evidence_refs": [{"line_start": 1, "line_end": 1}],
        }
    )

    with pytest.raises(StructuredOutputError) as captured:
        FourGeeksRFPAnalyzer(Gateway(raw)).classify("one line")

    diagnostic = captured.value.diagnostic
    assert diagnostic.category == "semantic_validation"
    assert diagnostic.error_type == "ambiguous_classification"
    assert diagnostic.path == "reason_code"
    assert diagnostic.violated_constraint == "classification_must_be_decisive"


def test_metadata_failure_retains_the_exact_operation() -> None:
    with pytest.raises(StructuredOutputError) as captured:
        FourGeeksRFPAnalyzer(Gateway("{}")).extract_metadata("private metadata")

    diagnostic = captured.value.diagnostic
    assert diagnostic.operation == "trackflow_rfp_metadata"
    assert diagnostic.category == "pydantic_validation"
    assert diagnostic.error_type == "missing"
    assert diagnostic.path == "rfp_id"
    assert "private metadata" not in repr(diagnostic)


def _metadata_payload() -> dict[str, object]:
    return {
        "rfp_id": None,
        "client_name": "private-client",
        "client_country_code": "ES",
        "client_country_original": "private-country",
        "operation_country_codes": ["ES"],
        "services_requested": [
            {
                "service_id": "warehouse",
                "requested": True,
                "source_ref": {"line_start": 1, "line_end": 1},
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
    }


@pytest.mark.parametrize(
    ("mutation", "expected_error_type"),
    [
        (
            {
                "services_requested": [
                    {
                        "service_id": "warehouse",
                        "requested": True,
                        "source_ref": {"line_start": 1, "line_end": 1},
                    },
                    {
                        "service_id": "warehouse",
                        "requested": False,
                        "source_ref": {"line_start": 2, "line_end": 2},
                    },
                ]
            },
            "duplicate_service_ids",
        ),
        (
            {"operation_country_codes": ["ES", "ES"]},
            "duplicate_operation_countries",
        ),
        (
            {
                "monthly_volume_value": 20000,
                "monthly_volume_unit": "m3/month",
            },
            "optional_value_reference_mismatch",
        ),
    ],
)
def test_metadata_root_invariants_have_distinct_content_free_diagnostics(
    mutation: dict[str, object],
    expected_error_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {**_metadata_payload(), **mutation}
    raw = json.dumps(payload)

    with pytest.raises(StructuredOutputError) as captured:
        FourGeeksRFPAnalyzer(Gateway(raw)).extract_metadata("private metadata")

    diagnostic = captured.value.diagnostic
    assert diagnostic.operation == "trackflow_rfp_metadata"
    assert diagnostic.category == "pydantic_validation"
    assert diagnostic.error_type == expected_error_type
    assert diagnostic.path == "root"
    assert diagnostic.received_type == "object"
    assert diagnostic.violated_constraint == "pydantic_schema"
    assert raw not in repr(diagnostic)
    assert "private-client" not in repr(diagnostic)
    assert "private-country" not in repr(diagnostic)
    assert "private metadata" not in repr(diagnostic)

    with caplog.at_level(logging.WARNING, logger="trackflow.rfp_intake.structured_output"):
        _log_structured_output_failure(job(3), captured.value)
    log = caplog.text
    assert f"error_type={expected_error_type}" in log
    assert "operation=trackflow_rfp_metadata" in log
    assert "path=root" in log
    for sensitive in (raw, "private-client", "private-country", "private metadata"):
        assert sensitive not in log


def test_department_mismatch_is_an_explicit_semantic_diagnostic() -> None:
    raw = json.dumps(
        {
            "department_id": "reverse",
            "key_aspects": [
                {
                    "fact": "Grounded fact",
                    "source_ref": {"line_start": 1, "line_end": 1},
                    "requested_action": "Confirm",
                }
            ],
            "open_questions": [],
            "source_references": [{"line_start": 1, "line_end": 1}],
            "detected_conflicts": [],
        }
    )
    metadata = RFPMetadata(
        rfp_id=None,
        client_name=None,
        client_country_code="ES",
        client_country_original=None,
        operation_country_codes=["ES"],
        services_requested=[
            ServiceRequest(
                service_id=DepartmentId.warehouse,
                requested=True,
                source_ref=SourceReference(line_start=1, line_end=1),
            )
        ],
        monthly_volume_value=None,
        monthly_volume_unit=None,
        monthly_volume_source_ref=None,
        deadline=None,
        deadline_source_ref=None,
        onboarding_target=None,
        onboarding_source_ref=None,
        budget_range=None,
        open_questions=[],
        currency="EUR",
        departments_needed=[DepartmentId.warehouse],
    )

    with pytest.raises(StructuredOutputError) as captured:
        FourGeeksRFPAnalyzer(Gateway(raw)).analyze_department(
            metadata=metadata,
            department_id=DepartmentId.warehouse,
            relevant_extract="one line",
        )

    diagnostic = captured.value.diagnostic
    assert diagnostic.operation == "trackflow_rfp_warehouse_section"
    assert diagnostic.category == "semantic_validation"
    assert diagnostic.error_type == "department_mismatch"
    assert diagnostic.path == "department_id"
    assert diagnostic.violated_constraint == "assigned_department_match"


def test_reference_out_of_bounds_has_graph_stage_diagnostic() -> None:
    raw = json.dumps(
        {
            "is_valid_rfp": True,
            "reason_code": "legitimate_rfp",
            "evidence_refs": [{"line_start": 9, "line_end": 9}],
        }
    )
    analyzer = FourGeeksRFPAnalyzer(Gateway(raw))
    runtime = SimpleNamespace(context=SimpleNamespace(analyzer=analyzer))

    with pytest.raises(StructuredOutputError) as captured:
        RFPIntakeGraph._classify({"markdown": "one line"}, runtime)

    diagnostic = captured.value.diagnostic
    assert diagnostic.operation == "trackflow_rfp_classification"
    assert diagnostic.category == "reference_bounds"
    assert diagnostic.error_type == "source_reference_out_of_bounds"
    assert diagnostic.path == "evidence_refs"
    assert diagnostic.received_type == "integer"


def _client(handler: httpx.MockTransport) -> FourGeeksHTTPClient:
    return FourGeeksHTTPClient(
        RAGSettings(
            base_url="https://provider.invalid/v1",
            api_key="private-api-key",
            embedding_model="embedding-model",
            generation_model="generation-model",
            qdrant_url="http://unused.invalid",
            min_request_interval_seconds=0,
        ),
        httpx.Client(transport=handler),
    )


def test_http_200_observation_and_usage_are_logged_without_response_or_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_response = "postgresql://private:password@example.invalid/db Bearer private-jwt"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "safe-request-1"},
            json={
                "choices": [
                    {
                        "message": {"content": private_response},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    analyzer = FourGeeksRFPAnalyzer(_client(httpx.MockTransport(handler)))
    with pytest.raises(StructuredOutputError) as captured:
        analyzer.classify("private PDF content")

    diagnostic = captured.value.diagnostic
    assert diagnostic.provider.http_status == 200
    assert diagnostic.provider.finish_reason == "length"
    assert diagnostic.provider.content_value_type == "string"
    assert diagnostic.provider.content_length == len(private_response)
    assert diagnostic.provider.prompt_tokens == 11
    assert diagnostic.provider.completion_tokens == 7
    assert diagnostic.provider.total_tokens == 18
    assert diagnostic.provider.request_id == "safe-request-1"

    with caplog.at_level(logging.WARNING, logger="trackflow.rfp_intake.structured_output"):
        _log_structured_output_failure(job(1), captured.value)
    log = caplog.text
    assert "operation=trackflow_rfp_classification" in log
    assert "validation_stage=json" in log
    assert "http_status=200" in log
    assert "finish_reason=length" in log
    assert "prompt_tokens=11" in log
    assert "safe-request-1" in log
    for sensitive in (
        private_response,
        "private PDF content",
        "private-api-key",
        "private-jwt",
        "password",
    ):
        assert sensitive not in log


def test_invalid_http_envelope_and_timeout_keep_operation_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    envelope_client = _client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"choices": []}))
    )
    with pytest.raises(RAGProviderError) as invalid_envelope:
        FourGeeksRFPAnalyzer(envelope_client).classify("private prompt")
    envelope_context = structured_generation_context(invalid_envelope.value)
    assert envelope_context is not None
    assert envelope_context.operation == "trackflow_rfp_classification"
    assert envelope_context.provider.http_status == 200

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    with pytest.raises(RAGProviderError) as timed_out:
        FourGeeksRFPAnalyzer(_client(httpx.MockTransport(timeout))).extract_metadata(
            "private metadata prompt"
        )
    timeout_context = structured_generation_context(timed_out.value)
    assert timeout_context is not None
    assert timeout_context.operation == "trackflow_rfp_metadata"

    with caplog.at_level(logging.WARNING, logger="trackflow.rfp_intake.structured_output"):
        _log_provider_failure(job(1), invalid_envelope.value)
        _log_provider_failure(job(2), timed_out.value)
    log = caplog.text
    assert "attempt=1 operation=trackflow_rfp_classification" in log
    assert "validation_stage=provider_envelope category=validation_error" in log
    assert "attempt=2 operation=trackflow_rfp_metadata" in log
    assert "validation_stage=provider_http category=timeout" in log
    assert "private prompt" not in log
    assert "private metadata prompt" not in log
    assert "private timeout detail" not in log


def test_multiple_attempts_keep_distinct_local_invocations_and_content_free_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    errors: list[StructuredOutputError] = []
    for _attempt in (1, 2):
        with pytest.raises(StructuredOutputError) as captured:
            FourGeeksRFPAnalyzer(Gateway("not-json-private-value")).classify(
                "private PDF prompt"
            )
        errors.append(captured.value)

    assert errors[0].diagnostic.invocation_id != errors[1].diagnostic.invocation_id
    with caplog.at_level(logging.WARNING, logger="trackflow.rfp_intake.structured_output"):
        _log_structured_output_failure(job(1), errors[0])
        _log_structured_output_failure(job(2), errors[1])

    lines = [line for line in caplog.messages if "rfp_intake_structured_failure" in line]
    assert len(lines) == 2
    assert "attempt=1" in lines[0] and "attempt=2" in lines[1]
    assert str(JOB_ID) in lines[0] and str(JOB_ID) in lines[1]
    serialized = "\n".join(lines)
    assert "not-json-private-value" not in serialized
    assert "private PDF prompt" not in serialized
