from __future__ import annotations

from pathlib import Path

from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from services.rfp_approval.models import rfp_authorized_business_inputs


ROOT = Path(__file__).resolve().parents[1]
BASE_DDL = (ROOT / "infra/sql/rfp_approval.sql").read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "infra/sql/rfp_approval_monthly_storage_volume.sql"
).read_text(encoding="utf-8")


def test_fresh_install_and_sqlalchemy_allow_exactly_the_eighth_input_kind() -> None:
    compiled = str(
        CreateTable(rfp_authorized_business_inputs).compile(
            dialect=postgresql.dialect()
        )
    )
    for source in (BASE_DDL, compiled):
        assert "monthly_storage_volume" in source
        assert "ck_rfp_business_input_kind" in source
        for historical in (
            "client_price_quote",
            "warehouse_capacity",
            "operational_scope",
            "currency_quote",
            "currency_conversion",
            "global_commercial_term",
            "volume_discount",
        ):
            assert historical in source


def test_forward_migration_discovers_the_real_constraint_and_is_fail_closed() -> None:
    assert "FROM pg_constraint" in MIGRATION
    assert "c.conrelid = target_table" in MIGRATION
    assert "c.contype = 'c'" in MIGRATION
    assert "DROP CONSTRAINT %I" in MIGRATION
    assert "existing_name" in MIGRATION
    assert "expected exactly one business input kind constraint" in MIGRATION
    assert "monthly_storage_volume" in MIGRATION
    assert "CREATE TABLE" not in MIGRATION
    assert "UPDATE rfp_authorized_business_inputs" not in MIGRATION
    assert "INSERT INTO rfp_authorized_business_inputs" not in MIGRATION


def test_forward_migration_is_reentrant_for_the_canonical_new_constraint() -> None:
    assert "IF existing_definition LIKE '%monthly_storage_volume%'" in MIGRATION
    assert "existing_name <> 'ck_rfp_business_input_kind'" in MIGRATION
    assert "RETURN;" in MIGRATION
