from pathlib import Path


BACKOFFICE = Path(__file__).parents[1] / "uis" / "backoffice"


def test_backoffice_separates_global_compliance_from_consolidated_outcome() -> None:
    page = (BACKOFFICE / "app/backoffice/rfps/page.tsx").read_text(encoding="utf-8")
    api = (BACKOFFICE / "lib/rfpApi.ts").read_text(encoding="utf-8")

    assert "Términos globales: {heading}" in page
    assert 'passed: "Evaluación global aprobada"' in page
    assert 'failed: "Evaluación global no aprobada"' in page
    assert '"en_evaluación": "Evaluación global en curso"' in page
    assert 'not_started: "Evaluación global todavía no ejecutada"' in page
    assert 'technical_failure: "Evaluación global no ejecutada por fallo técnico"' in page
    assert 'needs_human_input: "Requiere información humana"' in page
    assert 'needs_human_review: "Revisión humana necesaria"' in page
    assert "generación global ${terms.iteration_count}/3" in page
    assert "terms.compliance_pass === true" in page
    assert "value.compliance_status" in api
    assert "value.evaluation_executed" in api


def test_backoffice_global_panel_does_not_use_consolidated_overall_pass() -> None:
    page = (BACKOFFICE / "app/backoffice/rfps/page.tsx").read_text(encoding="utf-8")
    global_component = page.split("function GlobalEvaluation", 1)[1].split(
        "function ResponseProgress", 1)[0]

    assert "overall_pass" not in global_component
    assert "terminal_status" not in global_component
    assert "compliance_status" in global_component
    assert "compliance_pass" in global_component
