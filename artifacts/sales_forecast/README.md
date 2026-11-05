# TrackFlow sales forecast artifacts

These files are reproducible outputs of:

```powershell
uv run python scripts/train_sales_forecast.py
```

- `predictions_2024_2025.csv`: month, actual revenue, recursive Random Forest
  prediction, causal seasonal baseline, and descriptive variability bounds.
- `metrics.json`: dataset fingerprint, split, model parameters, features,
  methodology, metric values, and uncertainty metadata.
- `forecast.png`: actual and predicted monthly revenue with the descriptive
  band calibrated only on temporal backtests within 2016-2023. It is labeled as
  a historical evaluation generated as of December 2023, uses EUR millions on
  the y-axis, and uses exactly two annotations: November 2025 combines the
  actual peak and largest RF error, while December 2025 provides a separate
  actual-versus-RF comparison. Each annotation shows both values in millions.

## Why Random Forest?

Random Forest was selected because TrackFlow has a relatively small historical
dataset (96 original training months, yielding 84 effective training examples
after constructing lag features), and the project requires an explainable and
reproducible regression model.

Random Forest combines multiple decision trees to capture nonlinear
relationships between historical revenue, calendar seasonality, and lagged
observations. It also does not require feature scaling to handle differences in
numerical magnitude.

Compared with XGBoost, Random Forest provides a simpler starting point with
fewer specialized dependencies and less tuning complexity. However, it cannot
naturally extrapolate growth beyond the observed training patterns.

The model achieved a test RMSE of €135,918.88, compared with €123,915.51 for
the seasonal baseline. Therefore, Random Forest did not outperform the simpler
benchmark in overall monetary accuracy. This limitation is explicitly retained
rather than adjusting the model using the test period.

## Why MSE alone is not enough

Mean Squared Error (MSE) measures the average squared difference between actual
and predicted revenue. It penalizes large errors heavily, but its squared
monetary units (EUR²) are not directly intuitive for business stakeholders.

RMSE converts the error back into EUR, while relative RMSE expresses it as a
percentage of average monthly revenue. MAE provides another interpretation of
the typical absolute monetary error.

However, even these metrics do not fully describe whether the model preserves
the ordering of high- and low-revenue months, whether the underlying revenue
distribution changes over time, or whether residuals exhibit unusual
statistical behavior.

For this reason, TrackFlow also reports normalized Gini, PSI, and K² as
explicitly documented operational diagnostics, pending confirmation of their
academic definitions.

The comparison against a seasonal baseline, the observed errors during
November and December, and the historical variability band provide additional
context for evaluating forecast reliability.

A low MSE alone would not establish that the model is suitable for financial
planning.

Metric definitions:

- MSE is the mean squared 2024-2025 error in EUR²; RMSE is its square root in
  EUR, and RMSE percentage divides RMSE by mean actual test revenue:
  `MSE = mean((actual - predicted)^2)` and
  `RMSE% = sqrt(MSE) / mean(actual) * 100`.
- PSI compares training and test `revenue_eur` distributions after forecasting,
  using five quantile bins fixed from training and additive `1e-6` smoothing.
  For bin shares `a_i` and `e_i`,
  `PSI = sum((a_i - e_i) * ln(a_i / e_i))`.
- normalized Gini compares the ordering induced by predictions with the perfect
  actual-revenue ordering; `Gini(actual, prediction) / Gini(actual, actual)`.
  Ties retain chronological order.
- K² and its p-value are the D'Agostino-Pearson normality diagnostic applied to
  the 24 residuals. It is neither R² nor evidence of residual independence.

PSI, Gini, and K² are transparent operational interpretations because no
unambiguous academic definitions were found. They remain pending academic
validation. The band is a descriptive historical range, not a guaranteed
confidence or prediction interval.

## Observed result and limitations

On the fixed 2024-2025 test period, Random Forest produced MSE
`18,473,941,158.68` EUR², RMSE `135,918.88` EUR and RMSE% `10.4628%`. The
seasonal baseline performed better on monetary error: MSE
`15,355,054,004.48` EUR², RMSE `123,915.51` EUR and RMSE% `9.5388%`. The test
was not used to change features or tune the model after observing this result.

The operational PSI (`10.516108`) compares 96 training revenues with only 24
test revenues. With such a small comparison sample it is highly sensitive to
empty quantile bins and to the documented `1e-6` smoothing; it is exploratory,
not a stable production drift threshold. Training-derived bin edges are
`[-∞, 820461.65, 908619.88, 992241.88, 1110225.74, +∞]` EUR, with training
counts `[19, 19, 19, 19, 20]` and test counts `[0, 0, 0, 1, 23]`. This PSI is
strictly a consolidated revenue comparison; it does not measure or infer
differences between Los Angeles, Zaragoza, countries, or the briefing's 60/40
market proportion. Random Forest's normalized ranking
Gini (`0.907568`) is stronger than the baseline's (`0.727419`), but ranking
quality does not replace monetary accuracy and does not contradict the worse
Random Forest RMSE.

The band calibration used three recursive origins: December 2020 (24 forecast
errors over 2021-2022), December 2021 (24 over 2022-2023), and December 2022
(12 over 2023). This yields 60 errors but only 36 unique target months because
the validation windows overlap. Calibration fits use 150 trees for bounded
runtime; the final model uses the fixed 300 trees. The empirical error quantiles
were `-123,414.92` and `154,712.58` EUR. The resulting test-band coverage was
15/24 (`62.5%`), specifically 8/12 in 2024 and 7/12 in 2025; this remains a
descriptive observation, not a promised coverage level.

Random Forest mean bias, defined as `actual - prediction`, was `-39,605.41`
EUR overall (`-47,339.95` in 2024 and `-31,870.87` in 2025). Its RMSE rose from
`114,073.19` EUR in 2024 to `154,710.02` EUR in 2025. The largest absolute RF
error was `313,912.40` EUR in November 2025. Exact yearly summaries, the five
largest errors for both methods, and band coverage are stored under
`diagnostics` in `metrics.json` and recomputable from the added CSV columns.

The three generated outputs are proposed for versioning with this README so an
evaluator can inspect the exact experiment. They may be regenerated; unrelated
artifacts must never be overwritten.
