from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from uuid import UUID, uuid4

import pytest

from services.rfp_intake.fourgeeks_budget import (
    BudgetedIntakeFourGeeksGateway,
    IntakeFourGeeksBudgetError,
    IntakeFourGeeksBudgetSettings,
    IntakeFourGeeksRequestLedger,
    load_intake_fourgeeks_budget_settings,
)
from services.rag.clients import _bounded_httpx_timeout
from services.rag.config import load_settings


PDF_SHA256 = "6e5ccfb2bc31ee846a8a33d853076e79824b09e387b4a8c98530626b59e2f37e"


class Gateway:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls = 0
        self._failure = failure
        self._lock = threading.Lock()

    def generate_structured(self, **_kwargs) -> str:
        with self._lock:
            self.calls += 1
        if self._failure is not None:
            failure, self._failure = self._failure, None
            raise failure
        return "{}"


class ObservedGateway(Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.last_generation_response_observation = object()
        self.last_generation_request_id = "safe-request-id"
        self.last_generation_usage = {"prompt_tokens": 3}


def settings(root: Path, job_id: UUID, *, budget_id: str = "modaviva-e2e"):
    return IntakeFourGeeksBudgetSettings(
        budget_id=budget_id,
        allowed_job_id=job_id,
        expected_pdf_sha256=PDF_SHA256,
        artifact_root=root,
    )


def settings_with_limit(
    root: Path, job_id: UUID, *, maximum_requests: int, budget_id: str
):
    return IntakeFourGeeksBudgetSettings(
        budget_id=budget_id,
        allowed_job_id=job_id,
        expected_pdf_sha256=PDF_SHA256,
        artifact_root=root,
        maximum_requests=maximum_requests,
    )


def ledger(root: Path, job_id: UUID, *, budget_id: str = "modaviva-e2e"):
    return IntakeFourGeeksRequestLedger(
        settings(root, job_id, budget_id=budget_id),
        job_id=job_id,
        ticket_id=uuid4(),
        actual_pdf_sha256=PDF_SHA256,
    )


def invoke(gateway: BudgetedIntakeFourGeeksGateway, index: int = 0) -> str:
    return gateway.generate_structured(schema_name=f"operation-{index}")


def test_budget_wrapper_exposes_only_content_free_delegate_observations(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    delegate = ObservedGateway()
    gateway = BudgetedIntakeFourGeeksGateway(delegate, ledger(tmp_path, job_id))

    assert gateway.last_generation_response_observation is (
        delegate.last_generation_response_observation
    )
    assert gateway.last_generation_request_id == "safe-request-id"
    assert gateway.last_generation_usage == {"prompt_tokens": 3}


def test_exactly_five_attempts_are_allowed_and_sixth_is_pre_network(tmp_path: Path) -> None:
    job_id = uuid4()
    delegate = Gateway()
    gateway = BudgetedIntakeFourGeeksGateway(delegate, ledger(tmp_path, job_id))

    assert [invoke(gateway, index) for index in range(5)] == ["{}"] * 5
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        invoke(gateway, 6)

    assert delegate.calls == 5


def test_explicit_ten_request_budget_allows_ten_and_blocks_eleven(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    configured = settings_with_limit(
        tmp_path, job_id, maximum_requests=10, budget_id="modaviva-ten"
    )
    delegate = Gateway()
    gateway = BudgetedIntakeFourGeeksGateway(
        delegate,
        IntakeFourGeeksRequestLedger(
            configured,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        ),
    )

    assert [invoke(gateway, index) for index in range(10)] == ["{}"] * 10
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        invoke(gateway, 11)
    assert delegate.calls == 10


def test_uncertain_network_error_consumes_slot_and_never_refunds(tmp_path: Path) -> None:
    job_id = uuid4()
    delegate = Gateway(failure=TimeoutError("private network detail"))
    gateway = BudgetedIntakeFourGeeksGateway(delegate, ledger(tmp_path, job_id))

    with pytest.raises(TimeoutError):
        invoke(gateway)
    for index in range(4):
        invoke(gateway, index + 1)
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted") as error:
        invoke(gateway, 6)

    assert delegate.calls == 5
    assert "private network detail" not in str(error.value)


@pytest.mark.parametrize("maximum_requests", [5, 10])
def test_concurrent_callers_cannot_overconsume_configured_slots(
    tmp_path: Path, maximum_requests: int
) -> None:
    job_id = uuid4()
    configured = settings_with_limit(
        tmp_path,
        job_id,
        maximum_requests=maximum_requests,
        budget_id=f"modaviva-concurrent-{maximum_requests}",
    )
    delegate = Gateway()
    gateway = BudgetedIntakeFourGeeksGateway(
        delegate,
        IntakeFourGeeksRequestLedger(
            configured,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        ),
    )

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(invoke, gateway, index) for index in range(20)]
    succeeded = sum(future.exception() is None for future in futures)
    blocked = [future.exception() for future in futures if future.exception() is not None]

    assert succeeded == delegate.calls == maximum_requests
    assert len(blocked) == 20 - maximum_requests
    assert all(isinstance(error, IntakeFourGeeksBudgetError) for error in blocked)


def test_new_ledger_after_worker_restart_does_not_reset_slots(tmp_path: Path) -> None:
    job_id = uuid4()
    first = BudgetedIntakeFourGeeksGateway(Gateway(), ledger(tmp_path, job_id))
    for index in range(5):
        invoke(first, index)

    restarted_delegate = Gateway()
    restarted = BudgetedIntakeFourGeeksGateway(
        restarted_delegate,
        ledger(tmp_path, job_id),
    )
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        invoke(restarted)

    assert restarted_delegate.calls == 0


def test_retry_repeats_operations_without_resetting_ten_request_budget(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    configured = settings_with_limit(
        tmp_path, job_id, maximum_requests=10, budget_id="modaviva-retry"
    )

    def restarted_gateway() -> BudgetedIntakeFourGeeksGateway:
        return BudgetedIntakeFourGeeksGateway(
            Gateway(),
            IntakeFourGeeksRequestLedger(
                configured,
                job_id=job_id,
                ticket_id=uuid4(),
                actual_pdf_sha256=PDF_SHA256,
            ),
        )

    first = restarted_gateway()
    for operation in range(5):
        invoke(first, operation)
    second = restarted_gateway()
    for operation in range(5, 10):
        invoke(second, operation)
    third = restarted_gateway()
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        invoke(third, 11)


def test_other_job_and_wrong_pdf_cannot_consume_reserved_budget(tmp_path: Path) -> None:
    allowed_job = uuid4()
    configured = settings(tmp_path, allowed_job)

    with pytest.raises(IntakeFourGeeksBudgetError, match="job is not authorized"):
        IntakeFourGeeksRequestLedger(
            configured,
            job_id=uuid4(),
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        )
    with pytest.raises(IntakeFourGeeksBudgetError, match="PDF is not authorized"):
        IntakeFourGeeksRequestLedger(
            configured,
            job_id=allowed_job,
            ticket_id=uuid4(),
            actual_pdf_sha256="a" * 64,
        )

    assert not (tmp_path / ".qa-fourgeeks-budgets").exists()


def test_loader_is_qa_scoped_fail_closed_and_keeps_part2_deterministic(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    values = {
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED": "YES",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT": "qa",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID": "modaviva-official-e2e",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID": str(job_id),
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256": PDF_SHA256,
        "RFP_RESPONSE_LLM_MODE": "deterministic",
    }

    configured = load_intake_fourgeeks_budget_settings(
        artifact_root=tmp_path,
        values=values,
    )
    assert configured is not None
    assert configured.maximum_requests == 5
    assert configured.allowed_job_id == job_id

    with pytest.raises(IntakeFourGeeksBudgetError, match="Parte 2"):
        load_intake_fourgeeks_budget_settings(
            artifact_root=tmp_path,
            values={**values, "RFP_RESPONSE_LLM_MODE": "fourgeeks"},
        )
    with pytest.raises(IntakeFourGeeksBudgetError, match="requires QA"):
        load_intake_fourgeeks_budget_settings(
            artifact_root=tmp_path,
            values={
                **values,
                "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT": "production",
            },
        )


@pytest.mark.parametrize("maximum", ["", "0", "-1", "+5", "1.5", "eleven", "11"])
def test_loader_rejects_invalid_or_excessive_request_maximum(
    tmp_path: Path, maximum: str
) -> None:
    job_id = uuid4()
    values = {
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED": "YES",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT": "qa",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID": "modaviva-invalid-limit",
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID": str(job_id),
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256": PDF_SHA256,
        "TRACKFLOW_RFP_INTAKE_FOURGEEKS_MAX_REQUESTS": maximum,
        "RFP_RESPONSE_LLM_MODE": "deterministic",
    }
    with pytest.raises(IntakeFourGeeksBudgetError):
        load_intake_fourgeeks_budget_settings(
            artifact_root=tmp_path,
            values=values,
        )


def test_loader_accepts_explicit_ten_and_timeout_45_fits_shared_budget(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    configured = load_intake_fourgeeks_budget_settings(
        artifact_root=tmp_path,
        values={
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_AUTHORIZED": "YES",
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ENVIRONMENT": "qa",
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_BUDGET_ID": "modaviva-ten-loader",
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_ALLOWED_JOB_ID": str(job_id),
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_EXPECTED_PDF_SHA256": PDF_SHA256,
            "TRACKFLOW_RFP_INTAKE_FOURGEEKS_MAX_REQUESTS": "10",
            "RFP_RESPONSE_LLM_MODE": "deterministic",
        },
    )
    rag = load_settings(
        {
            "FOUR_GEEKS_API_KEY": "offline-present",
            "QDRANT_URL": "http://unused.invalid",
            "RAG_GENERATION_TIMEOUT_SECONDS": "45",
        }
    )

    assert configured is not None and configured.maximum_requests == 10
    assert rag.generation_timeout_seconds == 45
    assert rag.total_budget_seconds == 80
    assert _bounded_httpx_timeout(45).as_dict() == {
        "connect": 3.0,
        "read": 40.5,
        "write": 1.0,
        "pool": 0.5,
    }
    # The shared gateway subtracts at most the configured two-second pacing
    # before constructing the per-call HTTPX timeout.
    assert _bounded_httpx_timeout(43).as_dict() == {
        "connect": 3.0,
        "read": 38.5,
        "write": 1.0,
        "pool": 0.5,
    }
    intake_source = Path("services/rfp_intake/tasks.py").read_text(encoding="utf-8")
    assert "QueryDeadline" not in intake_source
    assert "total_budget_seconds" not in intake_source


def test_started_budget_binding_and_maximum_cannot_change(tmp_path: Path) -> None:
    job_id = uuid4()
    initial = settings_with_limit(
        tmp_path, job_id, maximum_requests=5, budget_id="modaviva-immutable"
    )
    invoke(
        BudgetedIntakeFourGeeksGateway(
            Gateway(),
            IntakeFourGeeksRequestLedger(
                initial,
                job_id=job_id,
                ticket_id=uuid4(),
                actual_pdf_sha256=PDF_SHA256,
            ),
        )
    )
    directory = tmp_path / ".qa-fourgeeks-budgets" / "modaviva-immutable"
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    expanded = settings_with_limit(
        tmp_path, job_id, maximum_requests=10, budget_id="modaviva-immutable"
    )

    with pytest.raises(IntakeFourGeeksBudgetError, match="cannot be changed"):
        IntakeFourGeeksRequestLedger(
            expanded,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        )
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_legacy_five_request_ledger_is_not_modified_or_expanded(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    directory = tmp_path / ".qa-fourgeeks-budgets" / "modaviva-legacy"
    directory.mkdir(parents=True)
    for index in range(1, 6):
        (directory / f"request-{index:02d}.json").write_text(
            "legacy-record", encoding="utf-8"
        )
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    legacy = settings_with_limit(
        tmp_path, job_id, maximum_requests=5, budget_id="modaviva-legacy"
    )
    gateway = BudgetedIntakeFourGeeksGateway(
        Gateway(),
        IntakeFourGeeksRequestLedger(
            legacy,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        ),
    )
    with pytest.raises(IntakeFourGeeksBudgetError, match="budget exhausted"):
        invoke(gateway)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before

    expanded = settings_with_limit(
        tmp_path, job_id, maximum_requests=10, budget_id="modaviva-legacy"
    )
    with pytest.raises(IntakeFourGeeksBudgetError, match="cannot be expanded"):
        IntakeFourGeeksRequestLedger(
            expanded,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        )
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("maximum", [0, 11, True, 1.5])
def test_direct_settings_cannot_bypass_absolute_request_limit(
    tmp_path: Path, maximum: object
) -> None:
    job_id = uuid4()
    configured = IntakeFourGeeksBudgetSettings(
        budget_id="modaviva-direct-invalid",
        allowed_job_id=job_id,
        expected_pdf_sha256=PDF_SHA256,
        artifact_root=tmp_path,
        maximum_requests=maximum,  # type: ignore[arg-type]
    )
    with pytest.raises(IntakeFourGeeksBudgetError, match="maximum is invalid"):
        IntakeFourGeeksRequestLedger(
            configured,
            job_id=job_id,
            ticket_id=uuid4(),
            actual_pdf_sha256=PDF_SHA256,
        )


def test_budget_records_are_content_minimal_and_contain_no_secret(tmp_path: Path) -> None:
    job_id = uuid4()
    invoke(BudgetedIntakeFourGeeksGateway(Gateway(), ledger(tmp_path, job_id)))

    directory = tmp_path / ".qa-fourgeeks-budgets" / "modaviva-e2e"
    record = next(directory.glob("request-*.json"))
    content = record.read_text(encoding="utf-8")
    assert "api_key" not in content.casefold()
    assert "authorization" not in content.casefold()
    assert "bearer" not in content.casefold()
    assert PDF_SHA256 in content
    manifest = (directory / "budget.json").read_text(encoding="utf-8")
    assert "api_key" not in manifest.casefold()
    assert "authorization" not in manifest.casefold()
    assert "bearer" not in manifest.casefold()


def test_response_generation_does_not_import_intake_budget() -> None:
    source = Path("services/rfp_response/tasks.py").read_text(encoding="utf-8")
    assert "fourgeeks_budget" not in source
    assert "IntakeFourGeeksRequestLedger" not in source
