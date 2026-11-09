from tests.pipelines.run_external_support_evals import run_evals, write_report


def test_external_eval_matrix_is_passing_and_sanitized(tmp_path):
    report = run_evals()
    assert report["passed"] is True
    assert len(report["evaluations"]) == 13
    serialized = str(report)
    for prohibited in ("What is", "Check ticket", "Grounded synthetic", "TRF-", "EVAL-SKU"):
        assert prohibited not in serialized

    target = tmp_path / "report.json"
    write_report(target, report)
    assert target.read_text(encoding="utf-8").endswith("\n")
