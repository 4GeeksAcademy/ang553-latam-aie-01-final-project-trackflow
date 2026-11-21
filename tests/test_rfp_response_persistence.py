from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

from services.rfp_intake.models import rfp_jobs, rfp_metadata_registry
from services.rfp_response.models import (
    rfp_response_drafts, rfp_response_evaluations, rfp_response_execution_failures,
    rfp_response_handoffs, rfp_response_llm_invocations, rfp_response_runs,
)
from services.rfp_response.store import canonical_hash


ROOT = Path(__file__).parents[1]


def test_response_metadata_is_isolated_and_declares_all_durable_boundaries() -> None:
    assert rfp_metadata_registry is not SQLModel.metadata
    names = set(rfp_metadata_registry.tables)
    assert {
        "rfp_response_runs", "rfp_response_sections", "rfp_response_drafts",
        "rfp_response_previews", "rfp_response_evaluator_results",
        "rfp_response_evaluations", "rfp_response_execution_failures",
        "rfp_response_handoffs", "rfp_response_llm_invocations",
    } <= names
    for table in (rfp_response_runs, rfp_response_drafts, rfp_response_evaluations,
                  rfp_response_execution_failures, rfp_response_handoffs):
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UUID" in ddl and "JSONB" in ddl if "payload" in table.c else "UUID" in ddl


def test_forward_migration_is_explicit_additive_and_keeps_official_states() -> None:
    ddl = (ROOT / "infra" / "sql" / "rfp_response_generation.sql").read_text(encoding="utf-8")
    for required in (
        "generando_borrador", "en_evaluación", "needs_human_review",
        "job_kind", "response_generation", "state_version", "execution_epoch",
        "rfp_response_execution_failures", "rfp_response_handoffs",
        "rfp_response_llm_invocations", "department_generation",
        "validation_path", "validation_error_type",
        "validation_received_type", "violated_constraint",
        "contract_version='rfp-response-v1'",
        "carrier_model", "carrier_confirmation_actor",
        "carrier_confirmation_source", "carrier_confirmed_at",
        "legacy_migration", "customer_managed", "trackflow_managed",
    ):
        assert required in ddl
    assert "DROP TABLE" not in ddl.upper()
    assert "TRUNCATE" not in ddl.upper()


def test_fresh_intake_ddl_remains_compatible_with_part1_and_part2_job_kinds() -> None:
    ddl = (ROOT / "infra" / "sql" / "rfp_intake.sql").read_text(encoding="utf-8")
    assert "job_kind text NOT NULL DEFAULT 'intake'" in ddl
    assert "response_generation" in ddl
    assert "analisis_completo" in ddl and "generando_borrador" in ddl
    compiled = str(CreateTable(rfp_jobs).compile(dialect=postgresql.dialect()))
    assert "job_kind" in compiled and "response_generation" in compiled


def test_canonical_hash_is_order_independent_and_content_sensitive() -> None:
    assert canonical_hash({"a": 1, "b": [2]}) == canonical_hash({"b": [2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_package_discovery_change_is_exactly_scoped() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    include = next(line for line in text.splitlines() if line.startswith("include = ["))
    assert '"services.rfp_response*"' in include
    assert '"data.pipelines.rfp_response*"' in include
    for existing in ("services.api*", "services.rfp_intake*", "pipelines*", "data.pipelines.rfp_intake*"):
        assert f'"{existing}"' in include
