"""Opt-in official PDF -> FinalDocument E2E through production HTTP paths.

The suite never provisions infrastructure and never creates authentication
contexts. It accepts only already-issued QA bearer tokens and an external JSON
manifest containing authorized commercial-input requests. PostgreSQL/Docker
authorization and environment preparation remain separate preconditions.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest


ROOT = Path(__file__).parents[1].resolve()
FIXTURES = ROOT / "tests" / "fixtures" / "rfp_intake"
RUN_ENV = "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_AUTHORIZED"
BASE_URL_ENV = "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_BASE_URL"
MANIFEST_ENV = "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_MANIFEST"
TOKEN_ENV = {
    "intake_admin": "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_INTAKE_ADMIN_TOKEN",
    "manager": "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_RUN_MANAGER_TOKEN",
    "warehouse": "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_WAREHOUSE_TOKEN",
    "lastmile": "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_LASTMILE_TOKEN",
    "reverse": "TRACKFLOW_RFP_APPROVAL_OFFICIAL_E2E_REVERSE_TOKEN",
}
SCENARIOS = {
    "modaviva": {
        "fixture": "trackflow-request-1.pdf",
        "departments": ("warehouse", "reverse"),
        "currency": "EUR",
    },
    "luna": {
        "fixture": "trackflow-request-2.pdf",
        "departments": ("warehouse", "lastmile"),
        "currency": "USD",
    },
}


class OfficialE2EConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OfficialE2ESettings:
    base_url: str
    manifest: dict[str, Any]
    tokens: dict[str, str]


def _require_loopback_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise OfficialE2EConfigurationError("base URL must be credential-free HTTP")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise OfficialE2EConfigurationError("base URL cannot contain path/query/fragment")
    if parsed.port is None or parsed.hostname is None:
        raise OfficialE2EConfigurationError("base URL requires an explicit port")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise OfficialE2EConfigurationError("base URL host must be a loopback IP") from error
    if not address.is_loopback:
        raise OfficialE2EConfigurationError("base URL must be loopback")
    return raw.rstrip("/")


def _load_settings() -> OfficialE2ESettings:
    if os.getenv(RUN_ENV) != "YES":
        raise OfficialE2EConfigurationError(f"{RUN_ENV}=YES is required")
    base_url = _require_loopback_url(os.getenv(BASE_URL_ENV, ""))
    raw_manifest = os.getenv(MANIFEST_ENV, "")
    if not raw_manifest:
        raise OfficialE2EConfigurationError("external manifest path is required")
    manifest_path = Path(raw_manifest)
    if not manifest_path.is_absolute() or manifest_path.suffix.lower() != ".json":
        raise OfficialE2EConfigurationError("manifest must be an absolute JSON path")
    resolved_manifest = manifest_path.resolve()
    if resolved_manifest == ROOT or ROOT in resolved_manifest.parents:
        raise OfficialE2EConfigurationError("manifest must remain outside the repository")
    if not resolved_manifest.is_file() or resolved_manifest.stat().st_size > 1_000_000:
        raise OfficialE2EConfigurationError("manifest is missing or too large")
    manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("environment") != "qa":
        raise OfficialE2EConfigurationError("manifest environment must be qa")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != set(SCENARIOS):
        raise OfficialE2EConfigurationError("manifest must define exactly ModaViva and Luna")
    tokens = {name: os.getenv(variable, "") for name, variable in TOKEN_ENV.items()}
    if any(not value for value in tokens.values()):
        raise OfficialE2EConfigurationError("all isolated QA bearer tokens are required")
    return OfficialE2ESettings(base_url=base_url, manifest=manifest, tokens=tokens)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise AssertionError("HTTP response is not a JSON object")
    return value


def _wait_json(
    operation: Callable[[], httpx.Response],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    description: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: int | None = None
    while time.monotonic() < deadline:
        response = operation()
        last_status = response.status_code
        if response.status_code == 200:
            value = _json(response)
            if predicate(value):
                return value
        elif response.status_code != 404:
            response.raise_for_status()
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}; status={last_status}")


def _scenario_manifest(settings: OfficialE2ESettings, name: str) -> dict[str, Any]:
    value = settings.manifest["scenarios"].get(name)
    if not isinstance(value, dict):
        raise OfficialE2EConfigurationError(f"scenario {name} must be an object")
    if value.get("carrier_model") not in {
        "unknown", "trackflow_managed", "customer_managed"
    }:
        raise OfficialE2EConfigurationError(f"scenario {name} has invalid carrier_model")
    inputs = value.get("business_inputs")
    if not isinstance(inputs, list):
        raise OfficialE2EConfigurationError(f"scenario {name} inputs must be a list")
    for item in inputs:
        if (
            not isinstance(item, dict)
            or item.get("actor") not in {"manager", *SCENARIOS[name]["departments"]}
            or not isinstance(item.get("request"), dict)
        ):
            raise OfficialE2EConfigurationError(
                f"scenario {name} contains an invalid authorized input envelope"
            )
    volume_plan = value.get("monthly_storage_volume")
    if not isinstance(volume_plan, dict) or set(volume_plan) != {
        "value",
        "unit",
        "period",
        "operational_scope",
        "evidence",
    }:
        raise OfficialE2EConfigurationError(
            f"scenario {name} requires a typed monthly_storage_volume plan"
        )
    if volume_plan["value"] is not None and not isinstance(volume_plan["value"], str):
        raise OfficialE2EConfigurationError(
            f"scenario {name} monthly volume must be an exact decimal string"
        )
    if volume_plan["value"] is None and any(
        volume_plan[field] is not None
        for field in ("unit", "period", "operational_scope", "evidence")
    ):
        raise OfficialE2EConfigurationError(
            f"scenario {name} cannot partially authorize monthly volume"
        )
    volume_requests = [
        item
        for item in inputs
        if item.get("request", {}).get("input_kind") == "monthly_storage_volume"
    ]
    if volume_plan["value"] is None and volume_requests:
        raise OfficialE2EConfigurationError(
            f"scenario {name} has an unauthorized monthly volume request"
        )
    if volume_plan["value"] is not None and len(volume_requests) != 1:
        raise OfficialE2EConfigurationError(
            f"scenario {name} requires exactly one authorized monthly volume request"
        )
    return value


def test_official_e2e_contract_uses_only_briefing_fixtures() -> None:
    assert set(SCENARIOS) == {"modaviva", "luna"}
    assert SCENARIOS["modaviva"]["departments"] == ("warehouse", "reverse")
    assert SCENARIOS["modaviva"]["currency"] == "EUR"
    assert SCENARIOS["luna"]["departments"] == ("warehouse", "lastmile")
    assert SCENARIOS["luna"]["currency"] == "USD"
    for scenario in SCENARIOS.values():
        fixture = FIXTURES / str(scenario["fixture"])
        assert fixture.is_file()
        assert fixture.stat().st_size > 0


def test_official_e2e_guardrails_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RUN_ENV, raising=False)
    with pytest.raises(OfficialE2EConfigurationError, match="=YES"):
        _load_settings()

    monkeypatch.setenv(RUN_ENV, "YES")
    monkeypatch.setenv(BASE_URL_ENV, "https://production.example.com")
    with pytest.raises(OfficialE2EConfigurationError, match="credential-free HTTP"):
        _load_settings()


def test_official_e2e_accepts_only_external_qa_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "authorized-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "environment": "qa",
                "scenarios": {
                    "modaviva": {
                        "carrier_model": "unknown",
                        "business_inputs": [],
                        "monthly_storage_volume": {
                            "value": None,
                            "unit": None,
                            "period": None,
                            "operational_scope": None,
                            "evidence": None,
                        },
                    },
                    "luna": {
                        "carrier_model": "customer_managed",
                        "business_inputs": [],
                        "monthly_storage_volume": {
                            "value": None,
                            "unit": None,
                            "period": None,
                            "operational_scope": None,
                            "evidence": None,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(RUN_ENV, "YES")
    monkeypatch.setenv(BASE_URL_ENV, "http://127.0.0.1:65499")
    monkeypatch.setenv(MANIFEST_ENV, str(manifest_path))
    for variable in TOKEN_ENV.values():
        monkeypatch.setenv(variable, "opaque-qa-token")

    settings = _load_settings()

    assert settings.base_url == "http://127.0.0.1:65499"
    assert set(settings.manifest["scenarios"]) == {"modaviva", "luna"}
    assert _scenario_manifest(settings, "modaviva")["business_inputs"] == []


def test_official_e2e_runner_has_no_database_or_identity_backdoor() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "VerifiedAuthenticationContext" not in imported
    assert not any(name.startswith("psycopg") for name in imported)
    assert not any(name.startswith("sqlalchemy") for name in imported)
    assert "subprocess" not in imported
    assert "httpx.Client" in source


@pytest.mark.skipif(os.getenv(RUN_ENV) != "YES", reason=f"{RUN_ENV}=YES required")
@pytest.mark.parametrize("scenario_name", ["modaviva", "luna"])
def test_official_pdf_reaches_one_final_document(scenario_name: str) -> None:
    settings = _load_settings()
    expected = SCENARIOS[scenario_name]
    configured = _scenario_manifest(settings, scenario_name)
    fixture = FIXTURES / str(expected["fixture"])
    admin = settings.tokens["intake_admin"]
    manager = settings.tokens["manager"]

    with httpx.Client(base_url=settings.base_url, timeout=30.0) as client:
        with fixture.open("rb") as source:
            accepted = _json(
                client.post(
                    "/rfps",
                    headers=_headers(admin),
                    files={"file": (fixture.name, source, "application/pdf")},
                )
            )
        ticket_id = accepted["ticket_id"]
        detail = _wait_json(
            lambda: client.get(f"/rfps/{ticket_id}", headers=_headers(admin)),
            lambda value: value.get("status") == "analisis_completo",
            description=f"{scenario_name} intake",
        )
        metadata = detail.get("metadata")
        assert isinstance(metadata, dict)
        assert set(metadata.get("departments_needed", ())) == set(
            expected["departments"]
        )
        assert metadata.get("currency") == expected["currency"]

        response_start = _json(
            client.post(
                f"/rfps/{ticket_id}/response",
                headers=_headers(admin),
                json={"carrier_model": configured["carrier_model"]},
            )
        )
        response_run_id = response_start["run_id"]
        response = _wait_json(
            lambda: client.get(
                f"/rfps/{ticket_id}/response/{response_run_id}",
                headers=_headers(admin),
            ),
            lambda value: value.get("status")
            in {"ready_for_part3", "needs_human_review", "technical_failure"},
            description=f"{scenario_name} Parte 2 handoff",
        )
        assert response["status"] != "technical_failure"

        initialized = _json(
            client.post(
                f"/rfps/{ticket_id}/approval",
                headers=_headers(manager),
                json={
                    "idempotency_key": str(uuid4()),
                    "automate_proposal_materialization": True,
                },
            )
        )
        assert initialized["proposal_automation_delegated"] is True
        approval_run_id = initialized["approval_run_id"]
        assert initialized["response_run_id"] == response_run_id

        correction_inputs: dict[str, list[dict[str, Any]]] = {}
        for business_input in configured["business_inputs"]:
            actor = business_input["actor"]
            recorded = _json(
                client.post(
                    f"/rfps/{ticket_id}/approval/{approval_run_id}/business-inputs",
                    headers=_headers(settings.tokens[actor]),
                    json=business_input["request"],
                )
            )
            request = business_input["request"]
            payload = request.get("payload", {})
            department = payload.get("department")
            if request.get("input_kind") == "client_price_quote" and isinstance(
                department, str
            ):
                correction_inputs.setdefault(department, []).append(
                    {
                        "input_id": recorded["input_id"],
                        "recorded_at": request["recorded_at"],
                    }
                )

        for revision in initialized["revisions"]:
            if revision["status"] != "blocked_input":
                continue
            department = revision["department"]
            inputs = correction_inputs.get(department, [])
            assert inputs, f"{department} lacks an authorized correction input"
            target = (
                f"/rfps/{ticket_id}/approval/{approval_run_id}/departments/"
                f"{department}/revisions/{revision['revision_id']}"
            )
            source = _json(
                client.get(
                    f"{target}/review",
                    headers=_headers(settings.tokens[department]),
                )
            )
            corrected = _json(
                client.post(
                    f"{target}/correct",
                    headers=_headers(settings.tokens[department]),
                    json={
                        "expected_predecessor_status": "blocked_input",
                        "expected_predecessor_content_hash": source["content_hash"],
                        "expected_predecessor_state_version": source["state_version"],
                        "authorized_input_ids": [item["input_id"] for item in inputs],
                        "idempotency_key": str(uuid4()),
                        "requested_at": inputs[0]["recorded_at"],
                    },
                )
            )
            assert corrected["status"] in {"pending_approval", "blocked_input"}
            assert corrected["ready_for_hitl"] is False
            assert corrected["remaining_input_codes"] == []

        assessment = _json(
            client.post(
                f"/rfps/{ticket_id}/approval/{approval_run_id}/assess-and-start",
                headers=_headers(manager),
            )
        )
        assert assessment["missing_input_codes"] == []
        outcomes = {item["trigger"]: item["outcome"] for item in assessment["outcomes"]}
        assert set(outcomes) == {
            "volume-vs-capacity", "returns-sla-breach", "currency-mismatch"
        }
        assert "insufficient_information" not in outcomes.values()
        assert "demonstrated" not in outcomes.values()
        ready = {item["department"]: item for item in assessment["ready_revisions"]}
        assert set(ready) == set(expected["departments"])

        for index, department in enumerate(expected["departments"]):
            revision = ready[department]
            prefix = f"/rfps/{ticket_id}/approval/{approval_run_id}"
            target = (
                f"{prefix}/departments/{department}/revisions/"
                f"{revision['revision_id']}"
            )
            review = _json(
                client.get(f"{target}/review", headers=_headers(settings.tokens[department]))
            )
            assert review["department"] == department
            assert review["revision_status"] == "pending_approval"
            assert review["is_current"] is True
            assert review["safe_preview"]
            assert review["evidence"]
            assert review["provenance"]
            assert "approve" in review["allowed_actions"]

            applied = _json(
                client.post(
                    f"{target}/decision",
                    headers=_headers(settings.tokens[department]),
                    json={"action": "approve", "reason": None},
                )
            )
            assert applied["decision"]["action"] == "approve"
            assert applied["application"]["applied"] is True
            if index == 0:
                for remaining in expected["departments"][1:]:
                    pending = ready[remaining]
                    state = _json(
                        client.get(
                            f"{prefix}/departments/{remaining}/revisions/"
                            f"{pending['revision_id']}",
                            headers=_headers(settings.tokens[remaining]),
                        )
                    )
                    assert state["interrupted"] is True
                    assert state["decision"] is None

        document = _wait_json(
            lambda: client.get(
                f"/rfps/{ticket_id}/approval/{approval_run_id}/document",
                headers=_headers(manager),
            ),
            lambda value: bool(value.get("final_document_id")),
            description=f"{scenario_name} FinalDocument",
            timeout=30.0,
        )
        assert document["currency"] == {
            "value": expected["currency"], "status": "documented"
        }
        assert {item["department"] for item in document["sections"]} == set(
            expected["departments"]
        )
        assert document["markdown"].startswith("# ")
        assert document["download_filename"].endswith(".md")

        terminal = _json(
            client.get(f"/rfps/{ticket_id}", headers=_headers(admin))
        )
        assert terminal["status"] == "terminado"

        first_department = expected["departments"][0]
        first_revision = ready[first_department]
        replay = _json(
            client.post(
                f"/rfps/{ticket_id}/approval/{approval_run_id}/reconcile",
                headers=_headers(manager),
                json={
                    "revision_id": first_revision["revision_id"],
                    "department": first_department,
                },
            )
        )
        assert replay["coordination"]["final_document_id"] == document["final_document_id"]
        replayed_document = _json(
            client.get(
                f"/rfps/{ticket_id}/approval/{approval_run_id}/document",
                headers=_headers(manager),
            )
        )
        assert replayed_document == document
