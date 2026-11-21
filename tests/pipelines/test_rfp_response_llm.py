from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from data.pipelines.rfp_intake.contracts import DepartmentId
from data.pipelines.rfp_response.contracts import RequestedAspect, RFPResponseStart
from data.pipelines.rfp_response.evaluators import inspect_department_safety
from services.rag.clients import FourGeeksHTTPClient
from services.rag.contracts import RAGProviderError
from services.rfp_response.llm import (
    FourGeeksRFPProvider, GenerationGatewaySettings, LLMCallBudget,
    RFPResponseLLMBudgetExceeded, RFPResponseLLMConfigurationError,
    RFPResponseLLMOutputError, RFPResponseLLMSettings,
    load_rfp_response_llm_settings,
)


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
HASH = "a" * 64


def request(*, action: str = "Provide a warehouse response.",
            department: DepartmentId = DepartmentId.warehouse,
            customer_managed_carrier: bool | None = None) -> RFPResponseStart:
    return RFPResponseStart(
        ticket_id=uuid4(), run_id=uuid4(), source_handoff_hash=HASH,
        summary_for_sales="Prepare the requested TrackFlow service response.",
        key_aspects=[RequestedAspect(
            aspect_id=f"{department.value}:01:1-2", department=department,
            fact=f"Client requests {department.value} service in scope.",
            requested_action=action, line_start=1, line_end=2,
        )], selected_departments=[department],
        client_country_code="US", operation_countries=["US"],
        proposal_currency="USD", average_monthly_volume_m3=20,
        customer_managed_carrier=customer_managed_carrier, language="en",
    )


def settings(**changes) -> RFPResponseLLMSettings:
    values = dict(
        mode="fourgeeks", base_url="https://gateway.example/v1",
        api_key="test-only", model="test-model", timeout_seconds=1,
        request_interval_seconds=0, max_calls=20, max_input_tokens=20_000,
        max_output_tokens=500, max_concurrency=2,
    )
    values.update(changes)
    return RFPResponseLLMSettings(**values)


class FakeGateway:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.last_generation_usage = {}
        self.last_generation_request_id = None

    def generate_structured(self, **kwargs) -> str:
        self.last_generation_usage = {
            "prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26,
        }
        self.last_generation_request_id = "fake-request-1"
        return self.responder(kwargs)

    def close(self) -> None:
        return None


def generation_plan(kwargs, *, reverse: bool = False) -> str:
    payload = json.loads(kwargs["user"])
    claim_ids = [item["claim_id"] for item in payload["authorized_claims"]]
    if reverse:
        claim_ids.reverse()
    return json.dumps({
        "response_segments": [{
            "text": next(item["statement"] for item in payload["authorized_claims"]
                         if item["claim_id"] == claim_id),
            "claim_ids": [claim_id],
        } for claim_id in claim_ids],
        "aspect_plans": [{"aspect_id": item["aspect_id"],
                          "claim_ids": claim_ids[:1]}
                         for item in payload["key_aspects"]],
        "pending_codes": [item["code"]
                          for item in payload["pending_commercial_items"]],
    })


def provider(responder, *, records=None, configured=None) -> FourGeeksRFPProvider:
    selected = configured or settings()
    captured = records if records is not None else []
    return FourGeeksRFPProvider(
        settings=selected, gateway_factory=lambda: FakeGateway(responder),
        budget=LLMCallBudget(selected), invocation_sink=captured.append,
    )


def test_modes_are_explicit_and_fourgeeks_fails_closed_without_key() -> None:
    deterministic = load_rfp_response_llm_settings({})
    assert deterministic.mode == "deterministic"
    assert deterministic.max_output_tokens == 3000
    with pytest.raises(RFPResponseLLMConfigurationError):
        load_rfp_response_llm_settings({"RFP_RESPONSE_LLM_MODE": "fourgeeks"})
    with pytest.raises(RFPResponseLLMConfigurationError):
        load_rfp_response_llm_settings({
            "RFP_RESPONSE_LLM_MODE": "fourgeeks", "FOUR_GEEKS_API_KEY": "test",
            "FOUR_GEEKS_BASE_URL": "http://unsafe",
        })


def test_fourgeeks_default_output_budget_matches_validated_real_runs() -> None:
    configured = load_rfp_response_llm_settings({
        "RFP_RESPONSE_LLM_MODE": "fourgeeks",
        "FOUR_GEEKS_API_KEY": "test-only",
    })
    assert configured.max_input_tokens == 2500
    assert configured.max_output_tokens == 3000
    assert configured.max_calls == 18
    assert configured.max_concurrency == 2


def test_published_output_budget_is_consistent_with_runbook() -> None:
    root = Path(__file__).parents[2]
    environment = (root / ".env.example").read_text(encoding="utf-8")
    runbook = (root / "docs/operations/rfp-response-generation.md").read_text(
        encoding="utf-8")
    task_readme = (root / "services/task_queue/README.md").read_text(
        encoding="utf-8")
    assert "RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS=3000" in environment
    assert "RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS=3000" in runbook
    assert "RFP_RESPONSE_LLM_MAX_OUTPUT_TOKENS=3000" in task_readme
    assert "Maximum output budget per request. Default: `3000`." in runbook
    assert "It is a ceiling passed to the" in runbook
    assert "600" not in runbook


def test_real_adapter_generates_only_authorized_claims_and_records_metadata() -> None:
    records = []
    adapter = provider(generation_plan, records=records)
    item_request = request()
    draft = adapter.generate_department(
        item_request, DepartmentId.warehouse, 1, (), NOW)
    assert draft.generated_content
    assert all(claim.statement in draft.generated_content
               for claim in draft.supported_claims)
    assert len(records) == 1
    record = records[0]
    assert record.outcome == "succeeded"
    assert record.provider_request_id == "fake-request-1"
    assert record.total_tokens == 26
    serialized = record.model_dump_json()
    assert draft.generated_content not in serialized
    assert "authorized_claims" not in serialized
    inspection = inspect_department_safety(item_request, draft, inspected_at=NOW)
    assert inspection.status == "verified_safe"


@pytest.mark.parametrize("department", list(DepartmentId))
def test_real_adapter_supports_every_authorized_department(department: DepartmentId) -> None:
    item_request = request(
        department=department, action=f"Provide a {department.value} response.")
    draft = provider(generation_plan).generate_department(
        item_request, department, 1, (), NOW)
    assert draft.department == department
    assert draft.generated_content
    assert inspect_department_safety(
        item_request, draft, inspected_at=NOW).status == "verified_safe"


def test_actual_http_client_adapter_uses_strict_schema_and_output_limit() -> None:
    seen: list[dict] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        seen.append(body)
        payload = json.loads(body["messages"][1]["content"])
        return httpx.Response(
            200, headers={"x-request-id": "safe-request-42"},
            json={"choices": [{"message": {"content": generation_plan(
                {"user": json.dumps(payload)})}}],
                  "usage": {"prompt_tokens": 20, "completion_tokens": 10,
                            "total_tokens": 30}},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    configured = settings(max_output_tokens=321)
    gateway_settings = GenerationGatewaySettings(
        base_url=configured.base_url, api_key=configured.api_key,
        generation_model=configured.model,
        request_timeout_seconds=configured.timeout_seconds,
    )
    records = []
    adapter = FourGeeksRFPProvider(
        settings=configured,
        gateway_factory=lambda: FourGeeksHTTPClient(gateway_settings, http_client),
        budget=LLMCallBudget(configured), invocation_sink=records.append,
    )
    adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert len(seen) == 1
    assert seen[0]["response_format"]["json_schema"]["strict"] is True
    assert seen[0]["max_tokens"] == 321
    assert seen[0]["temperature"] == 0
    assert records[0].provider_request_id == "safe-request-42"
    assert records[0].total_tokens == 30


def test_grounded_llm_paraphrase_passes_the_deterministic_preview_gate() -> None:
    def paraphrase(kwargs) -> str:
        payload = json.loads(kwargs["user"])
        claim_id = payload["authorized_claims"][0]["claim_id"]
        return json.dumps({
            "response_segments": [{
                "text": "Storage in Los Angeles costs 18 USD per cubic meter each month.",
                "claim_ids": [claim_id],
            }],
            "aspect_plans": [{"aspect_id": payload["key_aspects"][0]["aspect_id"],
                              "claim_ids": [claim_id]}],
            "pending_codes": [item["code"]
                              for item in payload["pending_commercial_items"]],
        })

    item_request = request()
    draft = provider(paraphrase).generate_department(
        item_request, DepartmentId.warehouse, 1, (), NOW)
    assert draft.generated_content.startswith("Storage in Los Angeles costs")
    inspection = inspect_department_safety(item_request, draft, inspected_at=NOW)
    assert inspection.status == "verified_safe"


@pytest.mark.parametrize(("bad_response", "category", "path", "constraint"), [
    ("not json", "pydantic_validation", "root", "pydantic_schema"),
    (json.dumps({"ordered_claim_ids": [], "aspect_plans": [],
                 "response_segments": [], "pending_codes": []}),
     "pydantic_validation", "ordered_claim_ids", "pydantic_schema"),
    (json.dumps({"response_segments": [{"text": "Invented price is 999 USD.",
                                        "claim_ids": ["claim-invented"]}],
                 "aspect_plans": [{"aspect_id": "warehouse:01:1-2",
                                   "claim_ids": []}], "pending_codes": []}),
     "deterministic_validation", "response_segments.0.claim_ids",
     "authorized_claim_ids"),
])
def test_malformed_or_invented_generation_fails_closed(
    bad_response: str, category: str, path: str, constraint: str,
) -> None:
    records = []
    adapter = provider(lambda _kwargs: bad_response, records=records)
    with pytest.raises(RFPResponseLLMOutputError):
        adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert records[0].outcome == "invalid_output"
    assert records[0].error_category == category
    assert records[0].validation_path == path
    assert records[0].validation_error_type
    assert records[0].validation_received_type
    assert records[0].violated_constraint == constraint


def test_trackflow_managed_lastmile_schema_requires_the_pending_rate_code() -> None:
    observed: dict = {}

    def responder(kwargs) -> str:
        observed.update(kwargs)
        return generation_plan(kwargs)

    item_request = request(
        department=DepartmentId.lastmile,
        customer_managed_carrier=False,
        action="Provide documented service and keep an unknown rate pending.",
    )
    draft = provider(responder).generate_department(
        item_request, DepartmentId.lastmile, 1, (), NOW)
    payload = json.loads(observed["user"])
    pending_schema = observed["schema"]["properties"]["pending_codes"]
    assert payload["required_pending_codes"] == ["LAST_MILE_RATE_REQUIRED"]
    assert pending_schema["minItems"] == pending_schema["maxItems"] == 1
    assert pending_schema["items"]["enum"] == ["LAST_MILE_RATE_REQUIRED"]
    assert [item.code for item in draft.pending_commercial_items] == [
        "LAST_MILE_RATE_REQUIRED"]
    assert "LAST_MILE_RATE_REQUIRED" in draft.generated_content


def test_lastmile_missing_required_pending_code_has_safe_exact_diagnostic() -> None:
    records = []

    def omit_pending(kwargs) -> str:
        value = json.loads(generation_plan(kwargs))
        value["pending_codes"] = []
        return json.dumps(value)

    item_request = request(
        department=DepartmentId.lastmile,
        customer_managed_carrier=False,
    )
    with pytest.raises(RFPResponseLLMOutputError):
        provider(omit_pending, records=records).generate_department(
            item_request, DepartmentId.lastmile, 1, (), NOW)
    record = records[0]
    assert record.error_category == "deterministic_validation"
    assert record.validation_path == "pending_codes"
    assert record.validation_error_type == "value_error"
    assert record.validation_received_type == "array"
    assert record.violated_constraint == "exact_pending_codes"
    assert "LAST_MILE_RATE_REQUIRED" not in record.model_dump_json()


@pytest.mark.parametrize(("carrier", "expected"), [
    (None, ["CARRIER_MODEL_REQUIRED"]),
    (True, []),
])
def test_lastmile_carrier_variants_specialize_pending_schema_without_prices(
    carrier: bool | None, expected: list[str],
) -> None:
    observed: dict = {}

    def responder(kwargs) -> str:
        observed.update(kwargs)
        return generation_plan(kwargs)

    item_request = request(
        department=DepartmentId.lastmile,
        customer_managed_carrier=carrier,
    )
    draft = provider(responder).generate_department(
        item_request, DepartmentId.lastmile, 1, (), NOW)
    pending_schema = observed["schema"]["properties"]["pending_codes"]
    assert pending_schema["minItems"] == pending_schema["maxItems"] == len(expected)
    if expected:
        assert pending_schema["items"]["enum"] == expected
    else:
        assert "enum" not in pending_schema["items"]
    assert [item.code for item in draft.pending_commercial_items] == expected
    pending_terms = [item for item in draft.commercial_terms if item.status == "pending"]
    for term in pending_terms:
        assert term.value is None and term.currency is None


def test_semantic_relevance_reports_missing_and_actionable_feedback() -> None:
    calls = 0

    def responder(kwargs) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return generation_plan(kwargs)
        payload = json.loads(kwargs["user"])
        return json.dumps({"aspects": [{
            "aspect_id": payload["key_aspects"][0]["aspect_id"],
            "status": "missing", "explanation": "No onboarding calendar is provided.",
            "supporting_claim_ids": [],
            "feedback": "Add a documented onboarding calendar or mark it pending.",
        }]})

    adapter = provider(responder)
    item_request = request(action="Provide a dated onboarding calendar.")
    draft = adapter.generate_department(item_request, DepartmentId.warehouse, 1, (), NOW)
    result = adapter.evaluate_relevance(item_request, draft)
    assert result.passed is False
    assert result.aspect_results[0].status == "missing"
    assert result.feedback == ["Add a documented onboarding calendar or mark it pending."]


def test_relevance_rejects_unknown_supporting_claim() -> None:
    calls = 0

    def responder(kwargs) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return generation_plan(kwargs)
        payload = json.loads(kwargs["user"])
        return json.dumps({"aspects": [{
            "aspect_id": payload["key_aspects"][0]["aspect_id"],
            "status": "addressed", "explanation": "Claim answers request.",
            "supporting_claim_ids": ["claim-not-present"], "feedback": None,
        }]})

    adapter = provider(responder)
    item_request = request()
    draft = adapter.generate_department(item_request, DepartmentId.warehouse, 1, (), NOW)
    with pytest.raises(RFPResponseLLMOutputError):
        adapter.evaluate_relevance(item_request, draft)


def test_semantic_relevance_preserves_contradiction_as_business_failure() -> None:
    calls = 0

    def responder(kwargs) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return generation_plan(kwargs)
        payload = json.loads(kwargs["user"])
        return json.dumps({"aspects": [{
            "aspect_id": payload["key_aspects"][0]["aspect_id"],
            "status": "contradicted",
            "explanation": "The response conflicts with the requested operating model.",
            "supporting_claim_ids": [],
            "feedback": "Remove the conflict and answer the requested operating model.",
        }]})

    adapter = provider(responder)
    item_request = request()
    draft = adapter.generate_department(item_request, DepartmentId.warehouse, 1, (), NOW)
    result = adapter.evaluate_relevance(item_request, draft)
    assert result.passed is False
    assert result.aspect_results[0].status == "contradicted"
    assert result.feedback


def test_llm_prose_with_invented_material_value_is_rejected() -> None:
    def invented(kwargs) -> str:
        payload = json.loads(kwargs["user"])
        claim_id = payload["authorized_claims"][0]["claim_id"]
        return json.dumps({
            "response_segments": [{
                "text": "Los Angeles storage is 999 USD per cubic meter per month.",
                "claim_ids": [claim_id],
            }],
            "aspect_plans": [{"aspect_id": payload["key_aspects"][0]["aspect_id"],
                              "claim_ids": [claim_id]}],
            "pending_codes": [item["code"]
                              for item in payload["pending_commercial_items"]],
        })

    with pytest.raises(RFPResponseLLMOutputError):
        provider(invented).generate_department(
            request(), DepartmentId.warehouse, 1, (), NOW)


def test_llm_prose_cannot_turn_documented_fact_into_a_guarantee() -> None:
    def invented_commitment(kwargs) -> str:
        payload = json.loads(kwargs["user"])
        claim_id = payload["authorized_claims"][0]["claim_id"]
        return json.dumps({
            "response_segments": [{
                "text": "TrackFlow guarantees Los Angeles storage at 18 USD per cubic meter per month.",
                "claim_ids": [claim_id],
            }],
            "aspect_plans": [{"aspect_id": payload["key_aspects"][0]["aspect_id"],
                              "claim_ids": [claim_id]}],
            "pending_codes": [item["code"]
                              for item in payload["pending_commercial_items"]],
        })

    with pytest.raises(RFPResponseLLMOutputError):
        provider(invented_commitment).generate_department(
            request(), DepartmentId.warehouse, 1, (), NOW)


@pytest.mark.parametrize("failure", ["timeout", "429"])
def test_http_double_errors_cross_the_real_adapter_as_technical_failures(failure: str) -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream detail", request=http_request)
        return httpx.Response(429, text="private upstream detail")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    configured = settings()
    gateway_settings = GenerationGatewaySettings(
        base_url=configured.base_url, api_key=configured.api_key,
        generation_model=configured.model,
        request_timeout_seconds=configured.timeout_seconds,
    )
    records = []
    adapter = FourGeeksRFPProvider(
        settings=configured,
        gateway_factory=lambda: FourGeeksHTTPClient(gateway_settings, client),
        budget=LLMCallBudget(configured), invocation_sink=records.append,
    )
    with pytest.raises(RAGProviderError) as captured:
        adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert "private upstream detail" not in str(captured.value)
    assert records[0].outcome == "technical_error"
    expected_category = "timeout" if failure == "timeout" else "http_error"
    assert records[0].error_category == expected_category
    if failure == "429":
        assert captured.value.status_code == 429


def test_previous_feedback_reaches_next_real_generation_and_changes_output() -> None:
    observed_feedback: list[list[str]] = []

    def responder(kwargs) -> str:
        payload = json.loads(kwargs["user"])
        feedback = payload["feedback_from_previous_iteration"]
        observed_feedback.append(feedback)
        return generation_plan(kwargs, reverse=bool(feedback))

    adapter = provider(responder)
    item_request = request()
    first = adapter.generate_department(item_request, DepartmentId.warehouse, 1, (), NOW)
    feedback = ["Lead with the documented grace period."]
    second = adapter.generate_department(item_request, DepartmentId.warehouse, 2, feedback, NOW)
    assert observed_feedback == [[], feedback]
    assert first.generated_content != second.generated_content
    assert first.iteration_count == 1 and second.iteration_count == 2


@pytest.mark.parametrize("category", ["timeout", "rate_limited", "http_error"])
def test_provider_failures_are_technical_and_never_fallback(category: str, monkeypatch) -> None:
    def fail(_kwargs):
        raise RAGProviderError("sanitized provider failure", category=category)

    monkeypatch.setattr(
        "data.pipelines.rfp_response.generators.DeterministicContentProvider.render",
        lambda *_args, **_kwargs: pytest.fail("deterministic fallback was invoked"),
    )
    records = []
    adapter = provider(fail, records=records)
    with pytest.raises(RAGProviderError):
        adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert records[0].outcome == "technical_error"
    assert records[0].error_category == category


def test_budget_is_thread_safe_and_enforces_concurrency_and_call_cap() -> None:
    configured = settings(max_calls=3, max_concurrency=2)
    budget = LLMCallBudget(configured)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def consume() -> str:
        nonlocal active, maximum
        with budget.reserve(10):
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
        return "ok"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(consume) for _ in range(4)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except RFPResponseLLMBudgetExceeded:
                outcomes.append("blocked")
    assert outcomes.count("ok") == 3
    assert outcomes.count("blocked") == 1
    assert maximum == 2
    assert budget.call_count == 3


def test_input_budget_blocks_before_gateway_creation() -> None:
    configured = settings(max_input_tokens=1)
    created = False

    def gateway_factory():
        nonlocal created
        created = True
        return FakeGateway(generation_plan)

    adapter = FourGeeksRFPProvider(
        settings=configured, gateway_factory=gateway_factory,
        budget=LLMCallBudget(configured), invocation_sink=lambda _record: None,
    )
    with pytest.raises(RFPResponseLLMBudgetExceeded):
        adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert created is False


def test_gateway_construction_failure_is_observed_without_content() -> None:
    records = []

    def unavailable():
        raise RuntimeError("local client construction failed")

    configured = settings()
    adapter = FourGeeksRFPProvider(
        settings=configured, gateway_factory=unavailable,
        budget=LLMCallBudget(configured), invocation_sink=records.append,
    )
    with pytest.raises(RuntimeError):
        adapter.generate_department(request(), DepartmentId.warehouse, 1, (), NOW)
    assert len(records) == 1
    assert records[0].outcome == "technical_error"
    assert records[0].error_category == "provider_error"


@pytest.mark.parametrize(("response_body", "expected", "returns_text"), [
    ({"choices": [{"message": {"content": None}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}},
     {"choices_count": 1, "content_value_type": "null", "finish_reason": "stop"},
     False),
    ({"choices": [], "usage": {}},
     {"choices_count": 0, "message_type": "null", "finish_reason": "missing"},
     False),
    ({"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]},
     {"content_value_type": "string", "content_length": 1,
      "finish_reason": "length"}, True),
    ({"choices": [{"message": {"content": {"private": "never-store"}},
                    "finish_reason": "stop"}]},
     {"content_value_type": "object", "content_length": None}, False),
])
def test_structured_http_200_observation_is_typed_and_content_free(
    response_body: dict, expected: dict, returns_text: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json; charset=utf-8"},
            json=response_body,
        )

    client = FourGeeksHTTPClient(
        GenerationGatewaySettings(
            base_url="https://gateway.example/v1", api_key="test-only",
            generation_model="test-model", request_timeout_seconds=1,
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    invoke = lambda: client.generate_structured(
        system="safe", user="synthetic", schema={"type": "object"},
        schema_name="diagnostic", max_output_tokens=10,
    )
    if returns_text:
        assert invoke() == "{"
    else:
        with pytest.raises(RAGProviderError) as captured:
            invoke()
        assert captured.value.category == "validation_error"
    observation = client.last_generation_response_observation
    assert observation is not None
    assert observation.http_status == 200
    assert observation.content_type == "application/json"
    assert observation.json_valid is True
    assert observation.top_level_type == "object"
    for field, value in expected.items():
        assert getattr(observation, field) == value
    assert "never-store" not in json.dumps(asdict(observation))


def test_observation_classifies_reasoning_refusal_tools_and_usage_without_values() -> None:
    private_marker = "private-reasoning-must-not-appear"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "safe-id", "object": "chat.completion", "created": 1,
            "model": "provider-model", "choices": [{
                "message": {"content": None, "reasoning": private_marker,
                            "refusal": None, "tool_calls": []},
                "finish_reason": "future-provider-value",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7},
        })

    client = FourGeeksHTTPClient(
        GenerationGatewaySettings(
            base_url="https://gateway.example/v1", api_key="test-only",
            generation_model="configured-model", request_timeout_seconds=1,
        ), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RAGProviderError):
        client.generate_structured(
            system="safe", user="synthetic", schema={"type": "object"},
            schema_name="diagnostic")
    observation = client.last_generation_response_observation
    assert observation is not None
    assert observation.refusal_present is True
    assert observation.tool_calls_present is True
    assert observation.reasoning_present is True
    assert observation.finish_reason == "other"
    assert observation.usage_type == "object"
    assert observation.prompt_tokens_type == "number"
    assert observation.completion_tokens_type == "number"
    assert observation.total_tokens_type == "number"
    assert observation.configured_model == "configured-model"
    assert private_marker not in json.dumps(asdict(observation))


def test_observation_marks_invalid_json_without_retaining_the_body() -> None:
    private_marker = "private-invalid-body"
    client = FourGeeksHTTPClient(
        GenerationGatewaySettings(
            base_url="https://gateway.example/v1", api_key="test-only",
            generation_model="configured-model", request_timeout_seconds=1,
        ), httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/plain"},
                content=private_marker,
            ))))
    with pytest.raises(RAGProviderError):
        client.generate_structured(
            system="safe", user="synthetic", schema={"type": "object"},
            schema_name="diagnostic")
    observation = client.last_generation_response_observation
    assert observation is not None
    assert observation.json_valid is False
    assert observation.top_level_type == "invalid"
    assert observation.content_type == "text/plain"
    assert private_marker not in json.dumps(asdict(observation))
