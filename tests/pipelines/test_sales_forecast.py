"""Focused tests for TrackFlow's monthly revenue forecasting pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import mean_squared_error

from pipelines.sales_forecast import (
    DATASET_PATH,
    OFFICIAL_DATASET_SHA256,
    REQUIRED_COLUMNS,
    SalesDataValidationError,
    build_training_features,
    evaluate_forecast,
    file_sha256,
    fit_random_forest,
    load_sales_data,
    normalized_gini,
    population_stability_index,
    recursive_forecast,
    run_experiment,
    seasonal_naive_forecast,
    temporal_split,
)


def _valid_frame() -> pd.DataFrame:
    return pd.read_csv(DATASET_PATH)


def _write(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_csv(path, index=False)
    return path


def test_official_csv_contract_and_integrity():
    frame = load_sales_data()
    assert tuple(frame.columns) == REQUIRED_COLUMNS
    assert len(frame) == 120
    assert frame["month"].iloc[0] == pd.Timestamp("2016-01-01")
    assert frame["month"].iloc[-1] == pd.Timestamp("2025-12-01")
    assert (frame["revenue_eur"] > 0).all()
    assert set(frame["market"]) == {"consolidated"}
    assert file_sha256(DATASET_PATH) == OFFICIAL_DATASET_SHA256


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda frame: frame.drop(columns=["market"]), "Expected columns"),
        (lambda frame: pd.concat([frame, frame.iloc[[0]]], ignore_index=True), "chronological|Duplicate"),
        (lambda frame: frame.drop(index=1).reset_index(drop=True), "consecutive"),
        (lambda frame: frame.assign(revenue_eur=frame["revenue_eur"].mask(frame.index == 0)), "null"),
        (lambda frame: frame.assign(revenue_eur=frame["revenue_eur"].mask(frame.index == 0, 0)), "strictly positive"),
        (lambda frame: frame.assign(revenue_eur=frame["revenue_eur"].mask(frame.index == 0, np.inf)), "finite"),
        (lambda frame: frame.assign(shipments_processed=frame["shipments_processed"].mask(frame.index == 0, 1.5)), "whole units"),
        (lambda frame: frame.assign(avg_revenue_per_shipment_eur=frame["avg_revenue_per_shipment_eur"].mask(frame.index == 0, np.inf)), "finite"),
        (lambda frame: frame.assign(avg_revenue_per_shipment_eur=frame["avg_revenue_per_shipment_eur"].mask(frame.index == 0, -1)), "strictly positive"),
        (lambda frame: frame.assign(avg_revenue_per_shipment_eur=frame["avg_revenue_per_shipment_eur"].mask(frame.index == 0, 99)), "inconsistent"),
        (lambda frame: frame.assign(month=frame["month"].mask(frame.index == 0, "invalid")), "Invalid date"),
        (lambda frame: frame.assign(market=frame["market"].mask(frame.index == 0, "spain")), "consolidated"),
        (lambda frame: frame.iloc[::-1].reset_index(drop=True), "chronological"),
    ],
)
def test_invalid_sales_inputs_are_rejected(tmp_path, mutation, match):
    path = _write(mutation(_valid_frame()), tmp_path / "invalid.csv")
    with pytest.raises(SalesDataValidationError, match=match):
        load_sales_data(path, verify_official_integrity=False)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(SalesDataValidationError, match="does not exist"):
        load_sales_data(tmp_path / "missing.csv", verify_official_integrity=False)


def test_temporal_split_is_exact_and_non_overlapping():
    train, test = temporal_split(load_sales_data())
    assert len(train) == 96
    assert len(test) == 24
    assert (train["month"].iloc[0], train["month"].iloc[-1]) == (
        pd.Timestamp("2016-01-01"),
        pd.Timestamp("2023-12-01"),
    )
    assert (test["month"].iloc[0], test["month"].iloc[-1]) == (
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2025-12-01"),
    )
    assert train["month"].max() < test["month"].min()


def test_features_are_lagged_and_rolling_values_exclude_current_target():
    train, _ = temporal_split(load_sales_data())
    features, target = build_training_features(train)
    first_position = 12
    prior = train["revenue_eur"].iloc[:first_position].to_numpy(dtype=float)
    assert len(features) == 84
    assert features.iloc[0]["revenue_lag_1"] == prior[-1]
    assert features.iloc[0]["revenue_lag_12"] == prior[0]
    assert features.iloc[0]["revenue_mean_3"] == pytest.approx(prior[-3:].mean())
    assert features.iloc[0]["revenue_std_12"] == pytest.approx(prior.std(ddof=0))
    assert target.iloc[0] == train["revenue_eur"].iloc[first_position]
    assert target.iloc[0] not in prior[-1:]


def test_recursive_forecast_is_unchanged_when_test_targets_are_replaced():
    frame = load_sales_data()
    train, test = temporal_split(frame)
    model, _, _ = fit_random_forest(train, n_estimators=40)
    original = recursive_forecast(model, train["revenue_eur"], test["month"].tolist())
    altered = test.copy()
    altered["revenue_eur"] = np.linspace(1.0, 24.0, len(altered))
    repeated = recursive_forecast(model, train["revenue_eur"], altered["month"].tolist())
    np.testing.assert_array_equal(original, repeated)


def test_end_to_end_artifacts_are_blind_to_changed_test_targets(tmp_path):
    original = _valid_frame()
    altered = original.copy()
    test_mask = altered["month"] >= "2024-01-01"
    # Change only the held-out labels. EUR 100 is below half-cent-per-shipment
    # for every test row, so the unchanged rounded average remains coherent.
    altered.loc[test_mask, "revenue_eur"] += 100.0
    first_csv = _write(original, tmp_path / "first_external.csv")
    second_csv = _write(altered, tmp_path / "second_external.csv")
    first = run_experiment(
        dataset_path=first_csv,
        output_dir=tmp_path / "first-output",
        n_estimators=30,
        backtest_estimators=20,
    )
    second = run_experiment(
        dataset_path=second_csv,
        output_dir=tmp_path / "second-output",
        n_estimators=30,
        backtest_estimators=20,
    )
    first_predictions = pd.read_csv(first["predictions_path"])
    second_predictions = pd.read_csv(second["predictions_path"])
    for column in (
        "random_forest_prediction_eur",
        "seasonal_baseline_prediction_eur",
        "variability_lower_eur",
        "variability_upper_eur",
    ):
        np.testing.assert_array_equal(first_predictions[column], second_predictions[column])
    assert not np.array_equal(
        first_predictions["actual_revenue_eur"],
        second_predictions["actual_revenue_eur"],
    )
    first_metadata = first["metadata"]
    second_metadata = second["metadata"]
    assert (
        first_metadata["metrics"]["random_forest"]["mse_eur2"]
        != second_metadata["metrics"]["random_forest"]["mse_eur2"]
    )
    for metadata in (first_metadata, second_metadata):
        assert metadata["dataset"]["path"].startswith("<external>/")
        assert str(tmp_path) not in metadata["dataset"]["path"]


def test_model_and_baseline_are_positive_reproducible_and_complete():
    train, test = temporal_split(load_sales_data())
    first_model, _, _ = fit_random_forest(train, n_estimators=40)
    second_model, _, _ = fit_random_forest(train, n_estimators=40)
    first = recursive_forecast(first_model, train["revenue_eur"], test["month"].tolist())
    second = recursive_forecast(second_model, train["revenue_eur"], test["month"].tolist())
    baseline = seasonal_naive_forecast(train["revenue_eur"], 24)
    assert len(first) == len(second) == len(baseline) == 24
    assert (first > 0).all() and (baseline > 0).all()
    np.testing.assert_array_equal(first, second)
    np.testing.assert_array_equal(baseline[:12], train["revenue_eur"].to_numpy()[-12:])
    np.testing.assert_array_equal(baseline[12:], baseline[:12])


def test_error_metrics_have_correct_dimensions_and_values():
    actual = np.arange(10.0, 34.0)
    offsets = np.resize(np.array([-1.0, 1.0]), actual.size)
    predicted = actual + offsets
    train = np.arange(1.0, 97.0)
    metrics = evaluate_forecast(actual, predicted, train)
    assert metrics.mse_eur2 == pytest.approx(mean_squared_error(actual, predicted))
    assert metrics.mse_eur2 == pytest.approx(1.0)
    assert metrics.rmse_eur == pytest.approx(1.0)
    assert metrics.rmse_percent_mean_actual == pytest.approx(1.0 / actual.mean() * 100)
    assert metrics.mae_eur == pytest.approx(1.0)


def test_operational_psi_gini_and_k2_contracts():
    reference = np.arange(1.0, 101.0)
    comparison = np.arange(51.0, 75.0)
    assert population_stability_index(reference, reference) == pytest.approx(0.0)
    assert population_stability_index(reference, comparison) > 0
    actual = np.array([5.0, 1.0, 4.0, 2.0, 3.0, 8.0, 7.0, 6.0])
    assert normalized_gini(actual, actual) == pytest.approx(1.0)
    metrics = evaluate_forecast(actual, actual + np.array([1, -1, 2, -2, 1, -1, 2, -2]), reference)
    assert np.isfinite(metrics.k2_residuals)
    assert 0 <= metrics.k2_pvalue <= 1


@pytest.mark.parametrize(
    "call",
    [
        lambda: population_stability_index([], [1]),
        lambda: population_stability_index([1, 1, 1], [1, 1]),
        lambda: normalized_gini([1], [1]),
        lambda: normalized_gini([1, 2], [1]),
        lambda: evaluate_forecast([1] * 7, [1] * 7, list(range(10))),
        lambda: evaluate_forecast(list(range(1, 9)), list(range(2, 10)), list(range(10))),
    ],
)
def test_metrics_reject_degenerate_or_invalid_inputs(call):
    with pytest.raises(ValueError):
        call()


def test_experiment_writes_consistent_artifacts(tmp_path):
    result = run_experiment(output_dir=tmp_path, n_estimators=40, backtest_estimators=30)
    predictions = pd.read_csv(result["predictions_path"])
    metrics = json.loads(Path(result["metrics_path"]).read_text(encoding="utf-8"))
    _, official_test = temporal_split(load_sales_data())
    assert Path(result["plot_path"]).stat().st_size > 0
    assert len(predictions) == 24
    assert predictions["month"].iloc[0] == "2024-01-01"
    assert predictions["month"].iloc[-1] == "2025-12-01"
    np.testing.assert_array_equal(
        predictions["actual_revenue_eur"].to_numpy(dtype=float),
        official_test["revenue_eur"].to_numpy(dtype=float),
    )
    assert (predictions["variability_lower_eur"] <= predictions["variability_upper_eur"]).all()
    assert metrics["dataset"]["sha256"] == OFFICIAL_DATASET_SHA256
    assert metrics["split"] == {
        "training_original_rows": 96,
        "test_rows": 24,
        "training_effective_rows": 84,
    }
    assert metrics["forecast"]["uses_test_targets"] is False
    assert metrics["variability_band"]["probabilistic_coverage_guaranteed"] is False
    assert metrics["variability_band"]["backtest_error_count"] == 60
    assert metrics["variability_band"]["unique_target_month_count"] == 36
    assert metrics["variability_band"]["windows_overlap"] is True
    assert [row["error_count"] for row in metrics["variability_band"]["backtest_origins"]] == [24, 24, 12]
    psi = metrics["metric_details"]["psi"]
    assert sum(psi["training_counts"]) == 96
    assert sum(psi["test_counts"]) == 24
    assert len(psi["training_counts"]) == psi["effective_bin_count"]
    actual = predictions["actual_revenue_eur"].to_numpy(dtype=float)
    for prediction_column, metric_key in (
        ("random_forest_prediction_eur", "random_forest"),
        ("seasonal_baseline_prediction_eur", "seasonal_baseline"),
    ):
        recomputed_mse = mean_squared_error(
            actual, predictions[prediction_column].to_numpy(dtype=float)
        )
        stored = metrics["metrics"][metric_key]
        assert stored["mse_eur2"] == pytest.approx(recomputed_mse)
        assert stored["rmse_eur"] == pytest.approx(np.sqrt(recomputed_mse))
    rf_residuals = (
        predictions["actual_revenue_eur"]
        - predictions["random_forest_prediction_eur"]
    )
    diagnostics = metrics["diagnostics"]
    assert diagnostics["random_forest"]["overall"][
        "mean_bias_actual_minus_prediction_eur"
    ] == pytest.approx(rf_residuals.mean())
    for year in (2024, 2025):
        mask = pd.to_datetime(predictions["month"]).dt.year == year
        year_mse = np.mean(np.square(rf_residuals[mask]))
        assert diagnostics["random_forest"]["by_year"][str(year)]["count"] == 12
        assert diagnostics["random_forest"]["by_year"][str(year)][
            "mse_eur2"
        ] == pytest.approx(year_mse)
    inside = (
        (predictions["actual_revenue_eur"] >= predictions["variability_lower_eur"])
        & (predictions["actual_revenue_eur"] <= predictions["variability_upper_eur"])
    )
    assert diagnostics["variability_band"]["inside_count"] == int(inside.sum())
    assert diagnostics["variability_band"]["coverage_fraction"] == pytest.approx(
        inside.mean()
    )
    largest = diagnostics["random_forest"]["largest_absolute_errors"]
    assert [row["absolute_error_eur"] for row in largest] == sorted(
        (row["absolute_error_eur"] for row in largest), reverse=True
    )
    assert largest[0]["absolute_error_eur"] == pytest.approx(
        predictions["rf_absolute_error_eur"].max()
    )

