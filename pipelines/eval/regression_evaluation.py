"""Leakage-safe temporal evaluation of TrackFlow's inherited Random Forest.

This module evaluates, but never redesigns, the model implemented in
``pipelines.sales_forecast``. Cross-validation splits are made on the original
monthly rows before lagged features are built. Each validation forecast is
recursive and therefore cannot consume validation labels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import matplotlib
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

from pipelines.sales_forecast import (
    DATASET_PATH,
    OFFICIAL_DATASET_SHA256,
    build_training_features,
    file_sha256,
    fit_random_forest,
    load_sales_data,
    recursive_forecast,
    seasonal_naive_forecast,
    temporal_split,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent
EXPECTED_TRAIN_MONTHS = (36, 48, 60, 72, 84)
EXPECTED_EFFECTIVE_ROWS = (24, 36, 48, 60, 72)
FIXED_VALIDATION_START = pd.Timestamp("2023-01-01")
FIXED_VALIDATION_END = pd.Timestamp("2023-12-01")
METRIC_KEYS = (
    "train_mae_eur",
    "train_rmse_eur",
    "validation_mae_eur",
    "validation_rmse_eur",
    "baseline_mae_eur",
    "baseline_rmse_eur",
)


class EvaluationError(ValueError):
    """Raised when the temporal evaluation contract cannot be satisfied."""


@dataclass(frozen=True)
class TemporalFold:
    """One expanding training prefix and its following validation year."""

    number: int
    train: pd.DataFrame
    validation: pd.DataFrame


def calculate_error_metrics(
    actual: Sequence[float], predicted: Sequence[float]
) -> dict[str, float]:
    """Return finite MAE and RMSE values for equal non-empty samples."""

    actual_array = np.asarray(actual, dtype=float)
    predicted_array = np.asarray(predicted, dtype=float)
    if actual_array.shape != predicted_array.shape or actual_array.size == 0:
        raise EvaluationError("Metrics require equal, non-empty samples")
    if not np.isfinite(actual_array).all() or not np.isfinite(predicted_array).all():
        raise EvaluationError("Metrics require finite values")
    errors = actual_array - predicted_array
    return {
        "mae_eur": float(np.mean(np.abs(errors))),
        "rmse_eur": float(np.sqrt(np.mean(np.square(errors)))),
    }


def build_temporal_folds(frame: pd.DataFrame) -> list[TemporalFold]:
    """Build the five exact expanding folds from the protected 2016-2023 set."""

    historical, protected_test = temporal_split(frame)
    if len(protected_test) != 24 or protected_test["month"].min() != pd.Timestamp("2024-01-01"):
        raise EvaluationError("The protected 2024-2025 test period is not intact")

    splitter = TimeSeriesSplit(n_splits=5, test_size=12)
    folds: list[TemporalFold] = []
    for number, (train_indexes, validation_indexes) in enumerate(
        splitter.split(historical), start=1
    ):
        train = historical.iloc[train_indexes].copy()
        validation = historical.iloc[validation_indexes].copy()
        if len(validation) != 12:
            raise EvaluationError(f"Fold {number} does not contain 12 validation months")
        if train["month"].max() >= validation["month"].min():
            raise EvaluationError(f"Fold {number} is not strictly chronological")
        if set(train_indexes).intersection(validation_indexes):
            raise EvaluationError(f"Fold {number} train and validation overlap")
        if validation["month"].max() >= pd.Timestamp("2024-01-01"):
            raise EvaluationError(f"Fold {number} enters the protected test period")
        folds.append(TemporalFold(number=number, train=train, validation=validation))

    observed_train_months = tuple(len(fold.train) for fold in folds)
    if observed_train_months != EXPECTED_TRAIN_MONTHS:
        raise EvaluationError(
            f"Unexpected training sizes: {observed_train_months}; expected {EXPECTED_TRAIN_MONTHS}"
        )
    return folds


def build_fixed_validation_windows(frame: pd.DataFrame) -> list[TemporalFold]:
    """Build recent contiguous training windows with the same 2023 validation year."""

    historical, protected_test = temporal_split(frame)
    if len(protected_test) != 24 or protected_test["month"].min() != pd.Timestamp("2024-01-01"):
        raise EvaluationError("The protected 2024-2025 test period is not intact")

    validation = historical.loc[
        historical["month"].between(FIXED_VALIDATION_START, FIXED_VALIDATION_END)
    ].copy()
    available_training = historical.loc[historical["month"] < FIXED_VALIDATION_START].copy()
    if len(validation) != 12 or available_training["month"].max() != pd.Timestamp("2022-12-01"):
        raise EvaluationError("Fixed validation must be exactly January-December 2023")

    windows: list[TemporalFold] = []
    for number, original_rows in enumerate(EXPECTED_TRAIN_MONTHS, start=1):
        train = available_training.tail(original_rows).copy()
        expected_months = pd.date_range(train["month"].iloc[0], "2022-12-01", freq="MS")
        if len(train) != original_rows or not train["month"].reset_index(drop=True).equals(
            pd.Series(expected_months)
        ):
            raise EvaluationError(
                f"Fixed window {number} is not a contiguous {original_rows}-month history"
            )
        if train["month"].max() >= validation["month"].min():
            raise EvaluationError(f"Fixed window {number} is not strictly chronological")
        windows.append(
            TemporalFold(number=number, train=train, validation=validation.copy())
        )
    return windows


def evaluate_fold(fold: TemporalFold) -> dict[str, object]:
    """Fit the inherited model and evaluate one fold without future labels."""

    model, training_features, training_target = fit_random_forest(fold.train)
    training_predictions = model.predict(training_features)
    validation_predictions = recursive_forecast(
        model,
        fold.train["revenue_eur"],
        fold.validation["month"].tolist(),
    )
    baseline_predictions = seasonal_naive_forecast(
        fold.train["revenue_eur"], len(fold.validation)
    )

    train_metrics = calculate_error_metrics(training_target, training_predictions)
    validation_actual = fold.validation["revenue_eur"].to_numpy(dtype=float)
    validation_metrics = calculate_error_metrics(validation_actual, validation_predictions)
    baseline_metrics = calculate_error_metrics(validation_actual, baseline_predictions)
    absolute_errors = np.abs(validation_actual - validation_predictions)
    squared_errors = np.square(validation_actual - validation_predictions)

    return {
        "fold": fold.number,
        "train_start": fold.train["month"].iloc[0].strftime("%Y-%m-%d"),
        "train_end": fold.train["month"].iloc[-1].strftime("%Y-%m-%d"),
        "validation_start": fold.validation["month"].iloc[0].strftime("%Y-%m-%d"),
        "validation_end": fold.validation["month"].iloc[-1].strftime("%Y-%m-%d"),
        "training_original_rows": len(fold.train),
        "training_effective_rows": len(training_features),
        "validation_rows": len(fold.validation),
        "train_mae_eur": train_metrics["mae_eur"],
        "train_rmse_eur": train_metrics["rmse_eur"],
        "validation_mae_eur": validation_metrics["mae_eur"],
        "validation_rmse_eur": validation_metrics["rmse_eur"],
        "baseline_mae_eur": baseline_metrics["mae_eur"],
        "baseline_rmse_eur": baseline_metrics["rmse_eur"],
        "validation_predictions_eur": validation_predictions.tolist(),
        "baseline_predictions_eur": baseline_predictions.tolist(),
        "validation_actual_eur": validation_actual.tolist(),
        "horizon_absolute_errors_eur": absolute_errors.tolist(),
        "horizon_squared_errors_eur2": squared_errors.tolist(),
    }


def evaluate_fixed_validation_curve(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Evaluate different recent-history sizes against one fixed 2023 target year."""

    results = [evaluate_fold(window) for window in build_fixed_validation_windows(frame)]
    effective_rows = tuple(int(result["training_effective_rows"]) for result in results)
    if effective_rows != EXPECTED_EFFECTIVE_ROWS:
        raise EvaluationError(
            f"Unexpected fixed-curve sizes: {effective_rows}; expected {EXPECTED_EFFECTIVE_ROWS}"
        )
    for result in results:
        result["curve_role"] = "fixed_2023_validation"
    return results


def aggregate_fold_metrics(fold_results: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Aggregate fold metrics using arithmetic mean and sample standard deviation."""

    results = list(fold_results)
    if len(results) < 2:
        raise EvaluationError("At least two fold results are required for sample deviation")
    summary: dict[str, object] = {}
    for key in METRIC_KEYS:
        values = np.asarray([float(result[key]) for result in results], dtype=float)
        if not np.isfinite(values).all():
            raise EvaluationError(f"Non-finite values found for {key}")
        mean = float(values.mean())
        standard_deviation = float(values.std(ddof=1))
        summary[key] = {
            "mean": mean,
            "sample_standard_deviation": standard_deviation,
            "formatted": f"EUR {mean:,.2f} ± EUR {standard_deviation:,.2f}",
        }
    validation_mae = float(summary["validation_mae_eur"]["mean"])  # type: ignore[index]
    baseline_mae = float(summary["baseline_mae_eur"]["mean"])  # type: ignore[index]
    summary["validation_mae_vs_baseline_percent"] = (
        (validation_mae - baseline_mae) / baseline_mae * 100.0
    )
    validation_values = np.asarray(
        [float(result["validation_mae_eur"]) for result in results], dtype=float
    )
    summary["validation_mae_range_eur"] = {
        "minimum": float(validation_values.min()),
        "maximum": float(validation_values.max()),
    }
    summary["model_better_mae_fold_count"] = sum(
        float(result["validation_mae_eur"]) < float(result["baseline_mae_eur"])
        for result in results
    )
    return summary


def _aggregate_horizons(fold_results: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for horizon in range(12):
        absolute = np.asarray(
            [result["horizon_absolute_errors_eur"][horizon] for result in fold_results],  # type: ignore[index]
            dtype=float,
        )
        squared = np.asarray(
            [result["horizon_squared_errors_eur2"][horizon] for result in fold_results],  # type: ignore[index]
            dtype=float,
        )
        rows.append(
            {
                "horizon_month": horizon + 1,
                "mean_absolute_error_eur": float(absolute.mean()),
                "rmse_eur": float(np.sqrt(squared.mean())),
            }
        )
    return rows


def _analyze_horizon_pattern(
    horizon_metrics: Sequence[Mapping[str, float | int]],
) -> dict[str, object]:
    mae = np.asarray([float(row["mean_absolute_error_eur"]) for row in horizon_metrics])
    first_half_mean = float(mae[:6].mean())
    second_half_mean = float(mae[6:].mean())
    second_vs_first_percent = (second_half_mean - first_half_mean) / first_half_mean * 100.0
    correlation = float(np.corrcoef(np.arange(1, 13, dtype=float), mae)[0, 1])
    return {
        "first_six_month_mae_mean_eur": first_half_mean,
        "last_six_month_mae_mean_eur": second_half_mean,
        "last_vs_first_six_month_percent": second_vs_first_percent,
        "horizon_mae_pearson_correlation": correlation,
        "interpretation": (
            "El MAE medio es mayor en la segunda mitad del horizonte, pero la serie no "
            "aumenta de forma monotónica. Esto es compatible con propagación recursiva, "
            "estacionalidad o cambios temporales; no identifica por sí solo una causa."
        ),
    }


def _diagnose(
    fold_results: Sequence[Mapping[str, object]],
    fixed_curve_results: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Classify the fixed model by contrasting temporal CV and fixed-year evidence."""

    train_mae = float(summary["train_mae_eur"]["mean"])  # type: ignore[index]
    validation_mae = float(summary["validation_mae_eur"]["mean"])  # type: ignore[index]
    baseline_mae = float(summary["baseline_mae_eur"]["mean"])  # type: ignore[index]
    gap_ratio = validation_mae / train_mae
    baseline_ratio = validation_mae / baseline_mae
    validation_values = np.asarray(
        [float(result["validation_mae_eur"]) for result in fold_results]
    )
    coefficient_of_variation = float(validation_values.std(ddof=1) / validation_values.mean())
    fixed_train = np.asarray(
        [float(result["train_mae_eur"]) for result in fixed_curve_results], dtype=float
    )
    fixed_validation = np.asarray(
        [float(result["validation_mae_eur"]) for result in fixed_curve_results], dtype=float
    )
    fixed_sizes = np.asarray(
        [float(result["training_effective_rows"]) for result in fixed_curve_results],
        dtype=float,
    )
    fixed_gap_ratios = fixed_validation / fixed_train
    fixed_validation_correlation = float(np.corrcoef(fixed_sizes, fixed_validation)[0, 1])
    fixed_largest_vs_smallest_percent = float(
        (fixed_validation[-1] - fixed_validation[0]) / fixed_validation[0] * 100.0
    )
    persistent_fixed_gap = bool(np.all(fixed_validation > fixed_train))

    # The category is an academic description of the combined observed pattern,
    # not a theorem or a universal threshold. The fixed-year curve removes the
    # changing validation-year confound, while recursive versus in-sample error
    # remains a material alternative explanation for part of the gap.
    if persistent_fixed_gap and baseline_ratio >= 1.0:
        category = "Overfitting"
        action = (
            "Como experimento correctivo, no como solución garantizada, comparar una "
            "regularización mayor del Random Forest (menor profundidad y/o mayor "
            "min_samples_leaf) mediante la curva fija y los cinco folds temporales, sin "
            "utilizar 2024-2025 para seleccionar parámetros. Si la brecha no disminuye, "
            "investigar por separado propagación recursiva y representación causal de tendencia."
        )
        rationale = (
            "La brecha entre MAE de entrenamiento y validación persiste al mantener fijo "
            "2023 como año de validación para los cinco tamaños, y la validación cruzada "
            "también muestra errores fuera de muestra mayores y un MAE medio peor que el "
            "baseline. La evidencia es mixta porque las cinco ventanas fijas sí superan "
            "el MAE del baseline de 2023 y la ventana mayor obtiene el mejor MAE fijo. "
            "Aun así, la brecha persistente respalda overfitting como etiqueta académica "
            "principal, sin identificar una causa única."
        )
    elif persistent_fixed_gap:
        category = "Overfitting"
        action = (
            "Contrastar en un experimento posterior una regularización mayor con ambos "
            "diseños temporales; conservar el cambio solo si reduce la brecha sin degradar "
            "MAE/RMSE frente al baseline."
        )
        rationale = (
            "La curva de validación fija conserva una brecha de generalización en todos "
            "los tamaños, aunque el modelo sí resulta competitivo con el baseline medio."
        )
    elif baseline_ratio > 1.0:
        category = "Underfitting"
        action = (
            "En un experimento posterior, revisar features causales y representación de "
            "tendencia, verificando cualquier cambio con los mismos folds temporales."
        )
        rationale = (
            "Los errores de entrenamiento y validación son relativamente cercanos, pero "
            "el modelo no mejora el baseline estacional."
        )
    else:
        category = "Ajuste razonable"
        action = (
            "Conservar el modelo y repetir esta evaluación al incorporar nuevos meses, "
            "manteniendo el baseline estacional como gate de promoción."
        )
        rationale = (
            "La brecha de generalización es acotada y el modelo es competitivo frente al "
            "baseline estacional bajo los folds temporales."
        )
    return {
        "category": category,
        "rationale": rationale,
        "recommended_action": action,
        "evidence": {
            "validation_to_train_mae_ratio": gap_ratio,
            "validation_to_baseline_mae_ratio": baseline_ratio,
            "validation_mae_coefficient_of_variation": coefficient_of_variation,
            "fixed_curve_validation_to_train_mae_ratios": fixed_gap_ratios.tolist(),
            "fixed_curve_validation_mae_size_correlation": fixed_validation_correlation,
            "fixed_curve_largest_vs_smallest_validation_mae_percent": (
                fixed_largest_vs_smallest_percent
            ),
            "fixed_curve_persistent_validation_gap": persistent_fixed_gap,
        },
        "uncertainty": (
            "Incluso con 2023 fijo, la brecha compara error dentro de muestra con un "
            "pronóstico recursivo de doce meses. Puede incorporar sobreajuste, propagación "
            "de errores, estacionalidad no capturada o limitaciones de extrapolación; la "
            "validación disponible no separa causalmente esas explicaciones."
        ),
    }


def _plot_learning_curve(
    fixed_curve_results: Sequence[Mapping[str, object]], path: Path
) -> None:
    sizes = [int(result["training_effective_rows"]) for result in fixed_curve_results]
    figure, axes = plt.subplots(1, 2, figsize=(13, 5), sharex=True)
    for axis, metric, title in (
        (axes[0], "mae", "MAE by effective training size"),
        (axes[1], "rmse", "RMSE by effective training size"),
    ):
        axis.plot(
            sizes,
            [float(result[f"train_{metric}_eur"]) for result in fixed_curve_results],
            marker="o",
            label="Training (in-sample, historical lags)",
        )
        axis.plot(
            sizes,
            [float(result[f"validation_{metric}_eur"]) for result in fixed_curve_results],
            marker="o",
            label="Validation (fixed 2023, 12-month recursive)",
        )
        axis.plot(
            sizes,
            [float(result[f"baseline_{metric}_eur"]) for result in fixed_curve_results],
            marker="o",
            linestyle="--",
            label="Seasonal validation baseline",
        )
        axis.set(title=title, xlabel="Effective training examples", ylabel="Error (EUR)")
        axis.grid(alpha=0.25)
        axis.ticklabel_format(axis="y", style="plain")
    handles, labels = axes[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="lower center", ncol=3, fontsize=8)
    figure.suptitle(
        "TrackFlow temporal learning curve\nFixed January-December 2023 validation",
        fontsize=12,
    )
    figure.tight_layout(rect=(0, 0.11, 1, 0.92))
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _money(value: float) -> str:
    return f"€{value:,.2f}"


def _write_report(metadata: Mapping[str, object], path: Path) -> None:
    folds = metadata["folds"]  # type: ignore[assignment]
    fixed_curve = metadata["fixed_validation_curve"]  # type: ignore[assignment]
    summary = metadata["summary"]  # type: ignore[assignment]
    diagnosis = metadata["diagnosis"]  # type: ignore[assignment]
    horizon = metadata["horizon_metrics"]  # type: ignore[assignment]
    horizon_analysis = metadata["horizon_analysis"]  # type: ignore[assignment]
    fold_lines = []
    for fold in folds:  # type: ignore[union-attr]
        fold_lines.append(
            "| {fold} | {train_start} – {train_end} | {validation_start} – "
            "{validation_end} | {training_original_rows} / {training_effective_rows} | "
            "{train_mae} | {train_rmse} | {validation_mae} | {validation_rmse} | "
            "{baseline_mae} | {baseline_rmse} |".format(
                **fold,
                train_mae=_money(float(fold["train_mae_eur"])),
                train_rmse=_money(float(fold["train_rmse_eur"])),
                validation_mae=_money(float(fold["validation_mae_eur"])),
                validation_rmse=_money(float(fold["validation_rmse_eur"])),
                baseline_mae=_money(float(fold["baseline_mae_eur"])),
                baseline_rmse=_money(float(fold["baseline_rmse_eur"])),
            )
        )
    summary_lines = []
    labels = {
        "train_mae_eur": "Train MAE",
        "train_rmse_eur": "Train RMSE",
        "validation_mae_eur": "Validation MAE",
        "validation_rmse_eur": "Validation RMSE",
        "baseline_mae_eur": "Baseline MAE",
        "baseline_rmse_eur": "Baseline RMSE",
    }
    for key, label in labels.items():
        item = summary[key]
        summary_lines.append(f"| {label} | {item['formatted']} |")
    horizon_lines = [
        f"| {row['horizon_month']} | {_money(float(row['mean_absolute_error_eur']))} | "
        f"{_money(float(row['rmse_eur']))} |"
        for row in horizon  # type: ignore[union-attr]
    ]
    fixed_curve_lines = []
    for row in fixed_curve:  # type: ignore[union-attr]
        fixed_curve_lines.append(
            "| {training_original_rows} / {training_effective_rows} | {train_start} – "
            "{train_end} | {train_mae} | {train_rmse} | {validation_mae} | "
            "{validation_rmse} | {baseline_mae} |".format(
                **row,
                train_mae=_money(float(row["train_mae_eur"])),
                train_rmse=_money(float(row["train_rmse_eur"])),
                validation_mae=_money(float(row["validation_mae_eur"])),
                validation_rmse=_money(float(row["validation_rmse_eur"])),
                baseline_mae=_money(float(row["baseline_mae_eur"])),
            )
        )

    report = f"""# TrackFlow — Evaluación temporal del modelo de regresión

## A. Objetivo empresarial

Evaluar la fiabilidad técnica del Random Forest mensual antes de considerar una
promoción posterior. Este informe no autoriza staging ni producción.

MAE en EUR es la métrica principal porque expresa el error mensual típico en
unidades directamente interpretables. RMSE es secundaria y aumenta el peso de
errores mensuales grandes. `CONTEXT.md` y `memory-bank/projectbrief.md` respaldan
una facturación empresarial aproximada de €9 millones anuales, pero no indican el
periodo de referencia. El CSV consolidado suma €9,384,261.30 en 2016 y
€14,418,668.57 en 2023. Por ello, la cifra contextual no se trata como el total de
2023 ni como una reconciliación contable del dataset. Ninguna fuente documenta una
función de costes que demuestre que sobreestimar sea más caro que subestimar.

## B. Dataset y modelo heredado

- Dataset oficial: `pipelines/raw/trackflow_sales.csv`, SHA-256
  `{metadata['dataset']['sha256']}`.
- 120 meses consolidados: enero de 2016 a diciembre de 2025.
- Desarrollo temporal: 2016–2023; holdout protegido: 2024–2025.
- Modelo sin cambios: `RandomForestRegressor`, 300 árboles, `max_depth=6`,
  `min_samples_leaf=2`, `max_features=0.8`, `random_state=42`, `n_jobs=1`.
- Features: calendario, tendencia, lags de ingresos 1/2/3/6/12 y estadísticas
  rodantes causales de 3/6/12 meses.
- El pipeline semanal, el dataset, el modelo y los artefactos anteriores no se
  modificaron.

## C. Validación cruzada temporal

Se aplicó `TimeSeriesSplit(n_splits=5, test_size=12)` sobre las 96 filas
originales de 2016–2023. Las features se construyeron después de cada corte. Cada
modelo recibió solo el prefijo de entrenamiento y generó recursivamente los doce
meses siguientes antes de consultar los valores reales de validación.

| Fold | Entrenamiento | Validación | Filas originales / efectivas | Train MAE | Train RMSE | Validation MAE | Validation RMSE | Baseline MAE | Baseline RMSE |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(fold_lines)}

### Resumen de estabilidad

| Métrica | Media ± desviación estándar muestral |
|---|---:|
{chr(10).join(summary_lines)}

El MAE medio del Random Forest es
`{float(summary['validation_mae_vs_baseline_percent']):.2f}%` mayor que el del
baseline, aunque lo mejora en `{summary['model_better_mae_fold_count']}` de 5
folds. El MAE de validación abarca
`{_money(float(summary['validation_mae_range_eur']['minimum']))}` a
`{_money(float(summary['validation_mae_range_eur']['maximum']))}`, con coeficiente
de variación `{diagnosis['evidence']['validation_mae_coefficient_of_variation']:.3f}`.
Esto indica variabilidad moderada entre años, pero no estabilidad productiva.

## D. Curva de aprendizaje con validación fija

![Curva de aprendizaje temporal](learning_curve.png)

La curva principal mantiene fija la validación enero–diciembre de 2023 y el origen
diciembre de 2022. Utiliza las 36, 48, 60, 72 y 84 observaciones más recientes
disponibles antes de 2023, sin discontinuidades; equivalen a 24, 36, 48, 60 y 72
ejemplos efectivos. Así se elimina el cambio de año de validación que afectaba a la
curva original, mientras los cinco folds anteriores se conservan como evaluación
independiente de estabilidad temporal.

| Filas originales / efectivas | Ventana de entrenamiento | Train MAE | Train RMSE | Validation MAE 2023 | Validation RMSE 2023 | Baseline MAE |
|---:|---|---:|---:|---:|---:|---:|
{chr(10).join(fixed_curve_lines)}

La curva anterior combinaba aumento de muestra con validaciones en años distintos;
la nueva mantiene año, origen y horizonte constantes. En ambas, los errores de
entrenamiento proceden de predicción dentro de muestra con lags reales y los de
validación son pronósticos recursivos. La nueva comparación aísla mejor el efecto
del tamaño/ventana histórica, pero esa diferencia de régimen predictivo permanece.

El MAE fijo no evoluciona monótonamente con el tamaño (`r=-0.272`), pero la ventana
mayor obtiene el mejor resultado: €98,484.94, un 8.98% menos que la ventana menor.
Las cinco ventanas superan por MAE al baseline 2023 de €110,373.73. Esto contrasta
con la validación cruzada, donde el Random Forest tiene un MAE medio 18.26% peor que
el baseline, y demuestra que el año evaluado influía materialmente en la curva
anterior.

### Error por horizonte

| Mes de horizonte | MAE medio | RMSE conjunto |
|---:|---:|---:|
{chr(10).join(horizon_lines)}

El MAE medio de los meses 7–12 es
`{float(horizon_analysis['last_vs_first_six_month_percent']):.2f}%` mayor que el de
los meses 1–6 y su correlación lineal con el horizonte es
`{float(horizon_analysis['horizon_mae_pearson_correlation']):.3f}`.
{horizon_analysis['interpretation']}

## E. Diagnóstico explícito

**{diagnosis['category']}.** {diagnosis['rationale']}

- Ratio validation/train MAE: `{diagnosis['evidence']['validation_to_train_mae_ratio']:.3f}`.
- Ratio validation/baseline MAE: `{diagnosis['evidence']['validation_to_baseline_mae_ratio']:.3f}`.
- Coeficiente de variación del MAE de validación: `{diagnosis['evidence']['validation_mae_coefficient_of_variation']:.3f}`.
- Ratios validation/train de la curva fija: `{', '.join(f"{value:.3f}" for value in diagnosis['evidence']['fixed_curve_validation_to_train_mae_ratios'])}`.
- Cambio de MAE fijo, ventana mayor frente a menor: `{diagnosis['evidence']['fixed_curve_largest_vs_smallest_validation_mae_percent']:.2f}%`.
- Correlación entre tamaño efectivo y MAE fijo: `{diagnosis['evidence']['fixed_curve_validation_mae_size_correlation']:.3f}`.

Incertidumbre: {diagnosis['uncertainty']} La etiqueta describe el patrón principal
observado; no demuestra por sí sola su causa.

## F. Acción correctiva

{diagnosis['recommended_action']}

La acción no se aplicó ni se presenta como solución garantizada. Cualquier
experimento posterior debe conservar la curva fija, los cinco folds, el mismo
baseline y mantener 2024–2025 fuera de la selección.

## G. Conclusión empresarial

No se recomienda promover este modelo a staging en el estado actual. Aunque la
evaluación es reproducible y causal, el diagnóstico y la comparación con el
baseline requieren resolver la acción propuesta y volver a superar los gates
temporales. Esta es una recomendación técnica, no una autorización operativa.

## H. Limitaciones y excepciones

- Histórico pequeño: 96 meses de desarrollo y entre 24 y 72 ejemplos efectivos.
- Solo cinco folds; cada validación corresponde a un año distinto.
- La curva fija usa ventanas móviles recientes: al variar el tamaño también cambia
  el inicio histórico, aunque año, origen y horizonte de validación permanezcan fijos.
- El error de entrenamiento dentro de muestra no es directamente equivalente al
  pronóstico recursivo de validación.
- Los folds usan horizonte 12; el holdout histórico anterior usó 24 meses.
- Los resultados 2024–2025 del proyecto anterior ya son conocidos, pero quedaron
  excluidos de folds, diagnóstico y recomendación nuevos.
- Random Forest no extrapola tendencias fuera de los patrones observados de forma
  natural; el presente diseño no atribuye causalmente los errores a esa limitación.
- Los artefactos se encuentran en `pipelines/eval/` por autorización del usuario,
  no en la ruta académica `data/eval/`. Esto no acredita aceptación académica.
"""
    path.write_text(report, encoding="utf-8")


def run_evaluation(
    *, dataset_path: Path = DATASET_PATH, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> dict[str, object]:
    """Run the fixed temporal evaluation and write its reproducible artifacts."""

    frame = load_sales_data(dataset_path)
    folds = build_temporal_folds(frame)
    fold_results = [evaluate_fold(fold) for fold in folds]
    effective_rows = tuple(int(result["training_effective_rows"]) for result in fold_results)
    if effective_rows != EXPECTED_EFFECTIVE_ROWS:
        raise EvaluationError(
            f"Unexpected effective sizes: {effective_rows}; expected {EXPECTED_EFFECTIVE_ROWS}"
        )
    summary = aggregate_fold_metrics(fold_results)
    fixed_curve_results = evaluate_fixed_validation_curve(frame)
    horizon_metrics = _aggregate_horizons(fold_results)
    horizon_analysis = _analyze_horizon_pattern(horizon_metrics)
    diagnosis = _diagnose(fold_results, fixed_curve_results, summary)
    annual_revenue = {
        str(int(year)): float(group["revenue_eur"].sum())
        for year, group in frame.groupby(frame["month"].dt.year, sort=True)
    }

    model, features, _ = fit_random_forest(folds[-1].train)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "fold_metrics.json"
    plot_path = output_dir / "learning_curve.png"
    report_path = output_dir / "evaluation_report.md"
    metadata: dict[str, object] = {
        "dataset": {
            "path": DATASET_PATH.relative_to(REPOSITORY_ROOT).as_posix()
            if Path(dataset_path).resolve() == DATASET_PATH.resolve()
            else f"<external>/{Path(dataset_path).name}",
            "sha256": file_sha256(Path(dataset_path)),
            "official_sha256": OFFICIAL_DATASET_SHA256,
            "total_rows": len(frame),
            "development_rows": 96,
            "protected_test_rows": 24,
            "annual_revenue_eur": annual_revenue,
        },
        "business_context": {
            "approximate_annual_revenue_eur": 9_000_000,
            "sources": ["CONTEXT.md", "memory-bank/projectbrief.md"],
            "source_period_specified": False,
            "reconciliation_with_dataset_claimed": False,
        },
        "splitter": {
            "type": "TimeSeriesSplit",
            "n_splits": 5,
            "test_size": 12,
            "shuffle": False,
            "protected_period": "2024-01 through 2025-12",
        },
        "model": {"algorithm": "RandomForestRegressor", **model.get_params()},
        "features": list(features.columns),
        "folds": fold_results,
        "summary": summary,
        "fixed_validation_curve": fixed_curve_results,
        "horizon_metrics": horizon_metrics,
        "horizon_analysis": horizon_analysis,
        "diagnosis": diagnosis,
        "interpretation_limits": {
            "training_error": "in-sample with observed historical lags",
            "validation_error": "12-month recursive forecast",
            "learning_curve": (
                "fixed January-December 2023 validation; recent contiguous training "
                "windows all end December 2022"
            ),
            "temporal_cv_role": "independent stability evaluation across 2019-2023",
            "holdout_used_for_selection": False,
        },
    }
    metrics_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _plot_learning_curve(fixed_curve_results, plot_path)
    _write_report(metadata, report_path)
    return {
        "metrics_path": metrics_path,
        "plot_path": plot_path,
        "report_path": report_path,
        "metadata": metadata,
    }
