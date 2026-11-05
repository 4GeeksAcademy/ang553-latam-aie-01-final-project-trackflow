"""Leakage-safe monthly revenue forecasting for TrackFlow.

The module is intentionally independent from the existing weekly Prefect
pipeline.  A forecast is simulated as of 2023-12-31: every value produced for
2024-2025 is fed back recursively and no test target is used as a feature.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import matplotlib
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from scipy.stats import normaltest
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = REPOSITORY_ROOT / "pipelines" / "raw" / "trackflow_sales.csv"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "artifacts" / "sales_forecast"
OFFICIAL_DATASET_SHA256 = "2B8C782EFE182AA001DB8044689E3D65E98B2CC17649B598CEDB665E7D2F4C33"
REQUIRED_COLUMNS = (
    "month",
    "revenue_eur",
    "shipments_processed",
    "avg_revenue_per_shipment_eur",
    "market",
)
LAGS = (1, 2, 3, 6, 12)
ROLLING_WINDOWS = (3, 6, 12)
TRAIN_END = pd.Timestamp("2023-12-01")
TEST_START = pd.Timestamp("2024-01-01")
TEST_END = pd.Timestamp("2025-12-01")


class SalesDataValidationError(ValueError):
    """Raised when the official sales dataset violates its contract."""


@dataclass(frozen=True)
class ForecastMetrics:
    mse_eur2: float
    rmse_eur: float
    rmse_percent_mean_actual: float
    mae_eur: float
    psi_train_vs_test_revenue: float
    normalized_gini: float
    k2_residuals: float
    k2_pvalue: float


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_sales_data(
    path: Path = DATASET_PATH, *, verify_official_integrity: bool = True
) -> pd.DataFrame:
    """Load and strictly validate the monthly consolidated sales dataset."""

    path = Path(path)
    if not path.is_file():
        raise SalesDataValidationError(f"Sales dataset does not exist: {path}")
    if verify_official_integrity and path.resolve() == DATASET_PATH.resolve():
        actual_hash = file_sha256(path)
        if actual_hash != OFFICIAL_DATASET_SHA256:
            raise SalesDataValidationError(
                f"Official dataset SHA-256 mismatch: expected {OFFICIAL_DATASET_SHA256}, "
                f"got {actual_hash}"
            )

    try:
        frame = pd.read_csv(path)
    except Exception as exc:
        raise SalesDataValidationError(f"Cannot read sales CSV: {exc}") from exc
    if tuple(frame.columns) != REQUIRED_COLUMNS:
        raise SalesDataValidationError(
            f"Expected columns {list(REQUIRED_COLUMNS)}, got {list(frame.columns)}"
        )
    if frame.empty:
        raise SalesDataValidationError("Sales dataset is empty")
    if frame.loc[:, REQUIRED_COLUMNS].isna().any().any():
        raise SalesDataValidationError("Required sales fields cannot contain null values")

    try:
        frame["month"] = pd.to_datetime(frame["month"], format="%Y-%m-%d", errors="raise")
        for column in ("revenue_eur", "shipments_processed", "avg_revenue_per_shipment_eur"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise SalesDataValidationError(f"Invalid date or numeric value: {exc}") from exc

    numeric_columns = ("revenue_eur", "shipments_processed", "avg_revenue_per_shipment_eur")
    if not np.isfinite(frame.loc[:, numeric_columns].to_numpy(dtype=float)).all():
        raise SalesDataValidationError("Required numeric sales fields must be finite")

    if not frame["month"].dt.is_month_start.all():
        raise SalesDataValidationError("Every month must use its first calendar day")
    if not frame["month"].is_monotonic_increasing:
        raise SalesDataValidationError("Sales rows must be in chronological order")
    if frame["month"].duplicated().any():
        raise SalesDataValidationError("Duplicate months are not allowed")
    expected = pd.date_range(frame["month"].iloc[0], frame["month"].iloc[-1], freq="MS")
    if not frame["month"].reset_index(drop=True).equals(pd.Series(expected)):
        raise SalesDataValidationError("Sales history must contain every consecutive month")
    if (frame["revenue_eur"] <= 0).any():
        raise SalesDataValidationError("revenue_eur must be strictly positive")
    if (frame["shipments_processed"] <= 0).any():
        raise SalesDataValidationError("shipments_processed must be strictly positive")
    if not np.equal(frame["shipments_processed"] % 1, 0).all():
        raise SalesDataValidationError("shipments_processed must contain whole units")
    frame["shipments_processed"] = frame["shipments_processed"].astype("int64")
    if (frame["avg_revenue_per_shipment_eur"] <= 0).any():
        raise SalesDataValidationError(
            "avg_revenue_per_shipment_eur must be strictly positive"
        )
    calculated_average = frame["revenue_eur"] / frame["shipments_processed"]
    # The source average is stored to cents. Half a cent is therefore the
    # largest principled rounding difference, apart from floating-point noise.
    if (
        np.abs(calculated_average - frame["avg_revenue_per_shipment_eur"])
        > 0.0050001
    ).any():
        raise SalesDataValidationError(
            "revenue_eur, shipments_processed and avg_revenue_per_shipment_eur "
            "are inconsistent beyond cent rounding"
        )
    if set(frame["market"]) != {"consolidated"}:
        raise SalesDataValidationError("All rows must have market=consolidated")
    return frame


def temporal_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the immutable academic split: 2016-2023 and 2024-2025."""

    train = frame.loc[frame["month"] <= TRAIN_END].copy()
    test = frame.loc[(frame["month"] >= TEST_START) & (frame["month"] <= TEST_END)].copy()
    if len(frame) != 120 or len(train) != 96 or len(test) != 24:
        raise SalesDataValidationError(
            f"Expected 120/96/24 total/train/test rows, got {len(frame)}/{len(train)}/{len(test)}"
        )
    if train["month"].iloc[0] != pd.Timestamp("2016-01-01") or train["month"].iloc[-1] != TRAIN_END:
        raise SalesDataValidationError("Training period must be 2016-01 through 2023-12")
    if test["month"].iloc[0] != TEST_START or test["month"].iloc[-1] != TEST_END:
        raise SalesDataValidationError("Test period must be 2024-01 through 2025-12")
    if set(train["month"]).intersection(test["month"]):
        raise SalesDataValidationError("Training and test periods overlap")
    return train, test


def _feature_dict(history: Sequence[float], month: pd.Timestamp, time_index: int) -> dict[str, float]:
    if len(history) < max(LAGS):
        raise ValueError(f"At least {max(LAGS)} historical months are required")
    angle = 2.0 * np.pi * month.month / 12.0
    features: dict[str, float] = {
        "month_sin": float(np.sin(angle)),
        "month_cos": float(np.cos(angle)),
        "is_november": float(month.month == 11),
        "is_december": float(month.month == 12),
        "is_february": float(month.month == 2),
        "time_index": float(time_index),
    }
    array = np.asarray(history, dtype=float)
    for lag in LAGS:
        features[f"revenue_lag_{lag}"] = float(array[-lag])
    for window in ROLLING_WINDOWS:
        values = array[-window:]
        features[f"revenue_mean_{window}"] = float(values.mean())
        features[f"revenue_std_{window}"] = float(values.std(ddof=0))
    return features


def build_training_features(train: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Build causal features; every row uses only earlier revenue values."""

    revenues = train["revenue_eur"].astype(float).tolist()
    rows: list[dict[str, float]] = []
    targets: list[float] = []
    indexes: list[pd.Timestamp] = []
    for position in range(max(LAGS), len(train)):
        rows.append(_feature_dict(revenues[:position], train["month"].iloc[position], position))
        targets.append(revenues[position])
        indexes.append(train["month"].iloc[position])
    return pd.DataFrame(rows, index=indexes), pd.Series(targets, index=indexes, name="revenue_eur")


def fit_random_forest(
    train: pd.DataFrame, *, n_estimators: int = 300
) -> tuple[RandomForestRegressor, pd.DataFrame, pd.Series]:
    features, target = build_training_features(train)
    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=6,
        min_samples_leaf=2,
        max_features=0.8,
        random_state=42,
        n_jobs=1,
    )
    model.fit(features, target)
    return model, features, target


def recursive_forecast(
    model: RandomForestRegressor,
    historical_revenue: Sequence[float],
    forecast_months: Sequence[pd.Timestamp],
) -> np.ndarray:
    """Forecast recursively without accepting or reading future target values."""

    history = [float(value) for value in historical_revenue]
    predictions: list[float] = []
    for offset, raw_month in enumerate(forecast_months):
        month = pd.Timestamp(raw_month)
        row = pd.DataFrame([_feature_dict(history, month, len(history))])
        prediction = max(float(model.predict(row)[0]), np.finfo(float).eps)
        predictions.append(prediction)
        history.append(prediction)
    return np.asarray(predictions)


def seasonal_naive_forecast(
    historical_revenue: Sequence[float], horizon: int
) -> np.ndarray:
    """Recursive same-month-last-year baseline, also blind to future targets."""

    history = [float(value) for value in historical_revenue]
    predictions: list[float] = []
    for _ in range(horizon):
        prediction = history[-12]
        predictions.append(prediction)
        history.append(prediction)
    return np.asarray(predictions)


def temporal_backtest_errors(
    train: pd.DataFrame, *, n_estimators: int = 150
) -> np.ndarray:
    """Collect recursive out-of-sample errors using training data only."""

    errors: list[float] = []
    for origin in range(60, len(train), 12):
        available = train.iloc[:origin]
        future = train.iloc[origin : min(origin + 24, len(train))]
        if future.empty:
            continue
        model, _, _ = fit_random_forest(available, n_estimators=n_estimators)
        predictions = recursive_forecast(
            model, available["revenue_eur"], future["month"].tolist()
        )
        errors.extend((future["revenue_eur"].to_numpy(dtype=float) - predictions).tolist())
    if len(errors) < 8:
        raise ValueError("Temporal backtesting produced too few residuals")
    return np.asarray(errors)


def population_stability_details(
    reference: Sequence[float], comparison: Sequence[float], *, bins: int = 5
) -> dict[str, object]:
    """Return PSI plus reproducible reference-bin evidence."""

    ref = np.asarray(reference, dtype=float)
    comp = np.asarray(comparison, dtype=float)
    if ref.size == 0 or comp.size == 0 or not np.isfinite(ref).all() or not np.isfinite(comp).all():
        raise ValueError("PSI requires finite, non-empty samples")
    quantile_edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if quantile_edges.size < 3:
        raise ValueError("PSI reference distribution is degenerate")
    edges = np.concatenate(([-np.inf], quantile_edges[1:-1], [np.inf]))
    ref_counts = np.histogram(ref, bins=edges)[0].astype(float)
    comp_counts = np.histogram(comp, bins=edges)[0].astype(float)
    epsilon = 1e-6
    ref_share = (ref_counts + epsilon) / (ref_counts.sum() + epsilon * len(ref_counts))
    comp_share = (comp_counts + epsilon) / (comp_counts.sum() + epsilon * len(comp_counts))
    value = float(np.sum((comp_share - ref_share) * np.log(comp_share / ref_share)))
    return {
        "value": value,
        "requested_bin_count": bins,
        "effective_bin_count": len(ref_counts),
        "bin_edges_eur": ["-Infinity", *edges[1:-1].tolist(), "Infinity"],
        "training_counts": ref_counts.astype(int).tolist(),
        "test_counts": comp_counts.astype(int).tolist(),
        "smoothing": epsilon,
        "reference": "training revenue_eur (2016-2023)",
        "comparison": "test revenue_eur (2024-2025)",
    }


def population_stability_index(
    reference: Sequence[float], comparison: Sequence[float], *, bins: int = 5
) -> float:
    """PSI using reference quantile bins and small additive smoothing."""

    return float(population_stability_details(reference, comparison, bins=bins)["value"])


def _gini(actual: np.ndarray, ranking: np.ndarray) -> float:
    order = np.lexsort((np.arange(len(ranking)), -ranking))
    ordered = actual[order]
    total = ordered.sum()
    if total <= 0:
        raise ValueError("Gini requires positive aggregate actual revenue")
    cumulative = np.cumsum(ordered) / total
    return float(cumulative.sum() / len(ordered) - (len(ordered) + 1) / (2 * len(ordered)))


def normalized_gini(actual: Sequence[float], predicted: Sequence[float]) -> float:
    """Normalized ranking Gini; 1.0 means the same ordering as actual values."""

    actual_array = np.asarray(actual, dtype=float)
    predicted_array = np.asarray(predicted, dtype=float)
    if actual_array.shape != predicted_array.shape or actual_array.size < 2:
        raise ValueError("Gini requires equal samples with at least two values")
    perfect = _gini(actual_array, actual_array)
    if np.isclose(perfect, 0.0):
        raise ValueError("Normalized Gini is undefined for a degenerate target")
    return float(_gini(actual_array, predicted_array) / perfect)


def evaluate_forecast(
    actual: Sequence[float],
    predicted: Sequence[float],
    train_revenue: Sequence[float],
) -> ForecastMetrics:
    actual_array = np.asarray(actual, dtype=float)
    predicted_array = np.asarray(predicted, dtype=float)
    if actual_array.shape != predicted_array.shape or actual_array.size < 8:
        raise ValueError("Evaluation requires equal samples with at least eight values")
    if not np.isfinite(actual_array).all() or not np.isfinite(predicted_array).all():
        raise ValueError("Evaluation values must be finite")
    residuals = actual_array - predicted_array
    if np.isclose(np.ptp(residuals), 0.0):
        raise ValueError("K2 is undefined for constant residuals")
    mse = float(mean_squared_error(actual_array, predicted_array))
    k2_result = normaltest(residuals)
    if not np.isfinite(k2_result.statistic) or not np.isfinite(k2_result.pvalue):
        raise ValueError("K2 produced a non-finite result")
    return ForecastMetrics(
        mse_eur2=mse,
        rmse_eur=float(np.sqrt(mse)),
        rmse_percent_mean_actual=float(np.sqrt(mse) / actual_array.mean() * 100.0),
        mae_eur=float(np.mean(np.abs(residuals))),
        psi_train_vs_test_revenue=population_stability_index(train_revenue, actual_array),
        normalized_gini=normalized_gini(actual_array, predicted_array),
        k2_residuals=float(k2_result.statistic),
        k2_pvalue=float(k2_result.pvalue),
    )


def _error_summary(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | int]:
    residuals = actual - predicted
    mse = float(np.mean(np.square(residuals)))
    return {
        "count": len(actual),
        "mse_eur2": mse,
        "rmse_eur": float(np.sqrt(mse)),
        "mae_eur": float(np.mean(np.abs(residuals))),
        "mean_bias_actual_minus_prediction_eur": float(np.mean(residuals)),
    }


def _forecast_diagnostics(predictions: pd.DataFrame) -> dict[str, object]:
    actual = predictions["actual_revenue_eur"].to_numpy(dtype=float)
    diagnostics: dict[str, object] = {}
    for label, column in (
        ("random_forest", "random_forest_prediction_eur"),
        ("seasonal_baseline", "seasonal_baseline_prediction_eur"),
    ):
        predicted = predictions[column].to_numpy(dtype=float)
        yearly: dict[str, object] = {}
        for year, group in predictions.groupby(predictions["month"].dt.year, sort=True):
            yearly[str(year)] = _error_summary(
                group["actual_revenue_eur"].to_numpy(dtype=float),
                group[column].to_numpy(dtype=float),
            )
        residuals = actual - predicted
        largest_indexes = np.argsort(-np.abs(residuals), kind="stable")[:5]
        diagnostics[label] = {
            "overall": _error_summary(actual, predicted),
            "by_year": yearly,
            "largest_absolute_errors": [
                {
                    "month": predictions["month"].iloc[index].strftime("%Y-%m-%d"),
                    "actual_revenue_eur": float(actual[index]),
                    "prediction_eur": float(predicted[index]),
                    "error_actual_minus_prediction_eur": float(residuals[index]),
                    "absolute_error_eur": float(abs(residuals[index])),
                }
                for index in largest_indexes
            ],
        }

    inside = predictions["within_variability_band"].to_numpy(dtype=bool)
    band_by_year: dict[str, object] = {}
    for year, group in predictions.groupby(predictions["month"].dt.year, sort=True):
        year_inside = group["within_variability_band"].to_numpy(dtype=bool)
        band_by_year[str(year)] = {
            "count": len(group),
            "inside_count": int(year_inside.sum()),
            "coverage_fraction": float(year_inside.mean()),
        }
    diagnostics["variability_band"] = {
        "count": len(predictions),
        "inside_count": int(inside.sum()),
        "coverage_fraction": float(inside.mean()),
        "by_year": band_by_year,
        "interpretation": "descriptive only; no probabilistic coverage guarantee",
    }
    return diagnostics


def _plot_forecast(predictions: pd.DataFrame, path: Path) -> None:
    fig, axis = plt.subplots(figsize=(12, 6))
    actual_millions = predictions["actual_revenue_eur"] / 1_000_000
    forecast_millions = predictions["random_forest_prediction_eur"] / 1_000_000
    baseline_millions = predictions["seasonal_baseline_prediction_eur"] / 1_000_000
    axis.plot(predictions["month"], actual_millions, label="Actual revenue", marker="o", markersize=4)
    axis.plot(predictions["month"], forecast_millions, label="Random Forest", marker="o", markersize=3)
    axis.plot(predictions["month"], baseline_millions, label="Seasonal baseline", linestyle="--")
    axis.fill_between(
        predictions["month"],
        predictions["variability_lower_eur"] / 1_000_000,
        predictions["variability_upper_eur"] / 1_000_000,
        alpha=0.2,
        label="Descriptive historical variability band",
    )
    annotation_specs = (
        (pd.Timestamp("2025-11-01"), "Peak actual + largest RF error", (8, 10)),
        (pd.Timestamp("2025-12-01"), "December comparison", (8, -22)),
    )
    for target_month, label, offset in annotation_specs:
        matches = np.flatnonzero(
            (predictions["month"] == target_month).to_numpy(dtype=bool)
        )
        if len(matches) != 1:
            continue
        index = int(matches[0])
        axis.annotate(
            f"{label}\nActual {actual_millions.iloc[index]:.2f}M | RF {forecast_millions.iloc[index]:.2f}M",
            (predictions["month"].iloc[index], actual_millions.iloc[index]),
            xytext=offset,
            textcoords="offset points",
            fontsize=8,
            arrowprops={"arrowstyle": "->", "alpha": 0.6},
        )
    axis.set(
        title="Historical forecast evaluation — generated as of December 2023\nTrackFlow consolidated revenue, 2024–2025",
        xlabel="Month",
        ylabel="Monthly revenue (EUR millions)",
    )
    axis.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    axis.grid(alpha=0.25)
    axis.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _safe_dataset_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return f"<external>/{path.name}"


def run_experiment(
    *,
    dataset_path: Path = DATASET_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    n_estimators: int = 300,
    backtest_estimators: int = 150,
) -> dict[str, object]:
    """Run the reproducible experiment and write its three required artifacts."""

    frame = load_sales_data(dataset_path)
    train, test = temporal_split(frame)
    model, features, _ = fit_random_forest(train, n_estimators=n_estimators)
    forecast = recursive_forecast(model, train["revenue_eur"], test["month"].tolist())
    baseline = seasonal_naive_forecast(train["revenue_eur"], len(test))
    errors = temporal_backtest_errors(train, n_estimators=backtest_estimators)
    lower_error, upper_error = np.quantile(errors, [0.1, 0.9])
    lower = np.maximum(forecast + lower_error, 0.0)
    upper = np.maximum(forecast + upper_error, 0.0)
    actual = test["revenue_eur"].to_numpy(dtype=float)
    model_metrics = evaluate_forecast(actual, forecast, train["revenue_eur"])
    baseline_metrics = evaluate_forecast(actual, baseline, train["revenue_eur"])
    psi_details = population_stability_details(train["revenue_eur"], actual)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions = pd.DataFrame(
        {
            "month": test["month"],
            "actual_revenue_eur": actual,
            "random_forest_prediction_eur": forecast,
            "seasonal_baseline_prediction_eur": baseline,
            "variability_lower_eur": lower,
            "variability_upper_eur": upper,
        }
    )
    predictions["rf_error_actual_minus_prediction_eur"] = (
        predictions["actual_revenue_eur"] - predictions["random_forest_prediction_eur"]
    )
    predictions["rf_absolute_error_eur"] = predictions[
        "rf_error_actual_minus_prediction_eur"
    ].abs()
    predictions["baseline_error_actual_minus_prediction_eur"] = (
        predictions["actual_revenue_eur"]
        - predictions["seasonal_baseline_prediction_eur"]
    )
    predictions["within_variability_band"] = (
        (predictions["actual_revenue_eur"] >= predictions["variability_lower_eur"])
        & (predictions["actual_revenue_eur"] <= predictions["variability_upper_eur"])
    )
    diagnostics = _forecast_diagnostics(predictions)
    predictions_path = output_dir / "predictions_2024_2025.csv"
    metrics_path = output_dir / "metrics.json"
    plot_path = output_dir / "forecast.png"
    predictions.to_csv(predictions_path, index=False, date_format="%Y-%m-%d")

    metadata: dict[str, object] = {
        "dataset": {
            "path": _safe_dataset_label(Path(dataset_path)),
            "sha256": file_sha256(Path(dataset_path)),
            "rows": len(frame),
            "market": "consolidated",
        },
        "split": {"training_original_rows": len(train), "test_rows": len(test), "training_effective_rows": len(features)},
        "model": {"algorithm": "RandomForestRegressor", **model.get_params()},
        "features": list(features.columns),
        "forecast": {"method": "recursive_24_month", "origin": "2023-12-31", "uses_test_targets": False},
        "variability_band": {
            "type": "descriptive empirical 10th-90th error quantiles",
            "calibration_scope": "temporal recursive backtests within 2016-2023 only",
            "backtest_error_count": len(errors),
            "lower_error_quantile_eur": float(lower_error),
            "upper_error_quantile_eur": float(upper_error),
            "probabilistic_coverage_guaranteed": False,
            "backtest_origins": [
                {
                    "origin": train["month"].iloc[origin - 1].strftime("%Y-%m-%d"),
                    "forecast_start": train["month"].iloc[origin].strftime("%Y-%m-%d"),
                    "forecast_end": train["month"].iloc[min(origin + 24, len(train)) - 1].strftime("%Y-%m-%d"),
                    "error_count": min(24, len(train) - origin),
                }
                for origin in range(60, len(train), 12)
            ],
            "unique_target_month_count": 36,
            "windows_overlap": True,
            "backtest_n_estimators": backtest_estimators,
            "final_model_n_estimators": n_estimators,
        },
        "metrics": {
            "random_forest": asdict(model_metrics),
            "seasonal_baseline": asdict(baseline_metrics),
            "definitions_status": {
                "mse": "contractually unambiguous",
                "psi": "operational interpretation pending academic validation",
                "gini": "operational interpretation pending academic validation",
                "k2": "operational D'Agostino-Pearson interpretation pending academic validation",
            },
        },
        "metric_details": {"psi": psi_details},
        "diagnostics": diagnostics,
    }
    metrics_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    _plot_forecast(predictions, plot_path)
    return {
        "predictions_path": predictions_path,
        "metrics_path": metrics_path,
        "plot_path": plot_path,
        "metadata": metadata,
    }

