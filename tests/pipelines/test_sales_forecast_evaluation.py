"""Tests for TrackFlow's temporal regression evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pipelines.eval.regression_evaluation import (
    EXPECTED_EFFECTIVE_ROWS,
    EXPECTED_TRAIN_MONTHS,
    EvaluationError,
    TemporalFold,
    aggregate_fold_metrics,
    build_fixed_validation_windows,
    build_temporal_folds,
    calculate_error_metrics,
    evaluate_fold,
    run_evaluation,
)
from pipelines.sales_forecast import (
    build_training_features,
    fit_random_forest,
    load_sales_data,
    recursive_forecast,
)


@pytest.fixture(scope="module")
def official_folds() -> list[TemporalFold]:
    return build_temporal_folds(load_sales_data())


@pytest.fixture(scope="module")
def fixed_validation_windows() -> list[TemporalFold]:
    return build_fixed_validation_windows(load_sales_data())


@pytest.fixture(scope="module")
def generated_evaluation(tmp_path_factory):
    return run_evaluation(output_dir=tmp_path_factory.mktemp("regression-evaluation"))


def test_folds_have_exact_dates_sizes_order_and_no_overlap(official_folds):
    expected = (
        ("2016-01-01", "2018-12-01", "2019-01-01", "2019-12-01"),
        ("2016-01-01", "2019-12-01", "2020-01-01", "2020-12-01"),
        ("2016-01-01", "2020-12-01", "2021-01-01", "2021-12-01"),
        ("2016-01-01", "2021-12-01", "2022-01-01", "2022-12-01"),
        ("2016-01-01", "2022-12-01", "2023-01-01", "2023-12-01"),
    )
    assert len(official_folds) == 5
    assert tuple(len(fold.train) for fold in official_folds) == EXPECTED_TRAIN_MONTHS
    for fold, dates in zip(official_folds, expected, strict=True):
        observed = tuple(
            frame["month"].iloc[position].strftime("%Y-%m-%d")
            for frame, position in (
                (fold.train, 0),
                (fold.train, -1),
                (fold.validation, 0),
                (fold.validation, -1),
            )
        )
        assert observed == dates
        assert len(fold.validation) == 12
        assert fold.train["month"].is_monotonic_increasing
        assert fold.validation["month"].is_monotonic_increasing
        assert fold.train["month"].max() < fold.validation["month"].min()
        assert set(fold.train["month"]).isdisjoint(fold.validation["month"])


def test_folds_completely_exclude_protected_2024_2025(official_folds):
    all_fold_months = pd.concat(
        [pd.concat([fold.train["month"], fold.validation["month"]]) for fold in official_folds]
    )
    assert all_fold_months.max() == pd.Timestamp("2023-12-01")
    assert not (all_fold_months >= pd.Timestamp("2024-01-01")).any()


def test_fixed_validation_windows_are_recent_contiguous_and_share_2023(
    fixed_validation_windows,
):
    expected_starts = (
        "2020-01-01",
        "2019-01-01",
        "2018-01-01",
        "2017-01-01",
        "2016-01-01",
    )
    assert tuple(len(window.train) for window in fixed_validation_windows) == EXPECTED_TRAIN_MONTHS
    for window, expected_start in zip(
        fixed_validation_windows, expected_starts, strict=True
    ):
        assert window.train["month"].iloc[0].strftime("%Y-%m-%d") == expected_start
        assert window.train["month"].iloc[-1] == pd.Timestamp("2022-12-01")
        assert window.validation["month"].iloc[0] == pd.Timestamp("2023-01-01")
        assert window.validation["month"].iloc[-1] == pd.Timestamp("2023-12-01")
        assert len(window.validation) == 12
        expected = pd.date_range(expected_start, "2022-12-01", freq="MS")
        assert window.train["month"].reset_index(drop=True).equals(pd.Series(expected))
        assert window.train["month"].max() < window.validation["month"].min()
        assert not (window.train["month"] >= pd.Timestamp("2023-01-01")).any()


def test_fixed_validation_predictions_do_not_depend_on_2023_labels(
    fixed_validation_windows,
):
    for window in fixed_validation_windows:
        model, _, _ = fit_random_forest(window.train)
        original = recursive_forecast(
            model, window.train["revenue_eur"], window.validation["month"].tolist()
        )
        altered = window.validation.copy()
        altered["revenue_eur"] = np.linspace(1.0, 12.0, len(altered))
        repeated = recursive_forecast(
            model, window.train["revenue_eur"], altered["month"].tolist()
        )
        np.testing.assert_array_equal(original, repeated)
        assert calculate_error_metrics(window.validation["revenue_eur"], original) != (
            calculate_error_metrics(altered["revenue_eur"], repeated)
        )


def test_each_fold_builds_causal_lags_and_rolling_statistics(official_folds):
    for fold, expected_rows in zip(official_folds, EXPECTED_EFFECTIVE_ROWS, strict=True):
        features, target = build_training_features(fold.train)
        assert len(features) == expected_rows
        first_position = 12
        prior = fold.train["revenue_eur"].iloc[:first_position].to_numpy(dtype=float)
        assert features.index.max() <= fold.train["month"].max()
        assert features.index.max() < fold.validation["month"].min()
        assert features.iloc[0]["revenue_lag_1"] == prior[-1]
        assert features.iloc[0]["revenue_lag_12"] == prior[0]
        assert features.iloc[0]["revenue_mean_3"] == pytest.approx(prior[-3:].mean())
        assert features.iloc[0]["revenue_std_12"] == pytest.approx(prior.std(ddof=0))
        assert target.iloc[0] == fold.train["revenue_eur"].iloc[first_position]


def test_validation_label_mutation_changes_metrics_but_not_predictions(official_folds):
    fold = official_folds[0]
    model, _, _ = fit_random_forest(fold.train)
    original_predictions = recursive_forecast(
        model, fold.train["revenue_eur"], fold.validation["month"].tolist()
    )
    altered_validation = fold.validation.copy()
    altered_validation["revenue_eur"] = np.linspace(1.0, 12.0, 12)
    repeated_predictions = recursive_forecast(
        model, fold.train["revenue_eur"], altered_validation["month"].tolist()
    )
    np.testing.assert_array_equal(original_predictions, repeated_predictions)
    original_metrics = calculate_error_metrics(
        fold.validation["revenue_eur"], original_predictions
    )
    altered_metrics = calculate_error_metrics(
        altered_validation["revenue_eur"], repeated_predictions
    )
    assert original_metrics != altered_metrics


def test_error_metrics_and_invalid_inputs():
    actual = np.array([1.0, 2.0, 3.0])
    predicted = np.array([2.0, 2.0, 1.0])
    metrics = calculate_error_metrics(actual, predicted)
    assert metrics["mae_eur"] == pytest.approx(1.0)
    assert metrics["rmse_eur"] == pytest.approx(np.sqrt(5.0 / 3.0))
    with pytest.raises(EvaluationError, match="equal, non-empty"):
        calculate_error_metrics([1.0], [1.0, 2.0])
    with pytest.raises(EvaluationError, match="finite"):
        calculate_error_metrics([1.0], [np.inf])


def test_aggregation_uses_mean_and_sample_standard_deviation():
    rows = []
    for value in (1.0, 3.0, 5.0):
        rows.append({key: value for key in (
            "train_mae_eur",
            "train_rmse_eur",
            "validation_mae_eur",
            "validation_rmse_eur",
            "baseline_mae_eur",
            "baseline_rmse_eur",
        )})
    summary = aggregate_fold_metrics(rows)
    assert summary["validation_mae_eur"]["mean"] == pytest.approx(3.0)
    assert summary["validation_mae_eur"]["sample_standard_deviation"] == pytest.approx(2.0)
    assert summary["validation_mae_vs_baseline_percent"] == pytest.approx(0.0)


def test_generated_fold_results_summary_curve_and_report_are_consistent(generated_evaluation):
    metadata = generated_evaluation["metadata"]
    assert len(metadata["folds"]) == 5
    assert tuple(row["training_effective_rows"] for row in metadata["folds"]) == EXPECTED_EFFECTIVE_ROWS
    for key in (
        "train_mae_eur",
        "train_rmse_eur",
        "validation_mae_eur",
        "validation_rmse_eur",
        "baseline_mae_eur",
        "baseline_rmse_eur",
    ):
        values = np.asarray([row[key] for row in metadata["folds"]], dtype=float)
        assert np.isfinite(values).all()
        assert metadata["summary"][key]["mean"] == pytest.approx(values.mean())
        assert metadata["summary"][key]["sample_standard_deviation"] == pytest.approx(
            values.std(ddof=1)
        )
    assert len(metadata["horizon_metrics"]) == 12
    fixed_curve = metadata["fixed_validation_curve"]
    assert len(fixed_curve) == 5
    assert tuple(row["training_effective_rows"] for row in fixed_curve) == EXPECTED_EFFECTIVE_ROWS
    assert {row["validation_start"] for row in fixed_curve} == {"2023-01-01"}
    assert {row["validation_end"] for row in fixed_curve} == {"2023-12-01"}
    assert {row["train_end"] for row in fixed_curve} == {"2022-12-01"}
    np.testing.assert_array_equal(
        fixed_curve[-1]["validation_predictions_eur"],
        metadata["folds"][-1]["validation_predictions_eur"],
    )
    assert np.isfinite(metadata["horizon_analysis"]["horizon_mae_pearson_correlation"])
    assert metadata["summary"]["model_better_mae_fold_count"] == sum(
        row["validation_mae_eur"] < row["baseline_mae_eur"]
        for row in metadata["folds"]
    )
    assert Path(generated_evaluation["plot_path"]).stat().st_size > 0
    report = Path(generated_evaluation["report_path"]).read_text(encoding="utf-8")
    assert "## E. Diagnóstico explícito" in report
    assert "## D. Curva de aprendizaje con validación fija" in report
    assert metadata["diagnosis"]["category"] in report
    assert metadata["diagnosis"]["recommended_action"] in report


def test_metrics_artifact_round_trips_exactly(generated_evaluation):
    stored = json.loads(
        Path(generated_evaluation["metrics_path"]).read_text(encoding="utf-8")
    )
    assert stored == generated_evaluation["metadata"]
    assert stored["splitter"]["shuffle"] is False
    assert stored["interpretation_limits"]["holdout_used_for_selection"] is False
    assert stored["business_context"]["source_period_specified"] is False
    assert stored["business_context"]["reconciliation_with_dataset_claimed"] is False
    assert stored["dataset"]["annual_revenue_eur"]["2016"] == pytest.approx(9_384_261.30)
    assert stored["dataset"]["annual_revenue_eur"]["2023"] == pytest.approx(14_418_668.57)


def test_fold_evaluation_is_reproducible(official_folds):
    first = evaluate_fold(official_folds[-1])
    second = evaluate_fold(official_folds[-1])
    for key in (
        "train_mae_eur",
        "train_rmse_eur",
        "validation_mae_eur",
        "validation_rmse_eur",
        "validation_predictions_eur",
    ):
        np.testing.assert_array_equal(first[key], second[key])
