/**
 * TypeScript types for the Telemetry Report endpoint contract.
 *
 * Mirrors the JSON returned by ``GET /telemetry/report``.
 *
 * @see services/api/routes/telemetry.py
 * @see services/telemetry/analysis.py
 */

/** Time window covered by the report. */
export interface TelemetryPeriod {
  from: string;
  to: string;
}

/** A single record in event_volume_by_day_and_type. */
export interface EventVolumeRecord {
  day: string;
  event_type: string;
  count: number;
}

/** A single record in operational_failures_by_type_and_day. */
export interface OperationalFailureRecord {
  day: string;
  event_type: string;
  count: number;
}

/** A single record in stock_insufficiency_by_day_warehouse_category. */
export interface StockInsufficiencyRecord {
  day: string;
  warehouse: string;
  product_category: string;
  count: number;
}

/** A single record in auth_failure_rate. */
export interface AuthFailureRateRecord {
  day: string;
  failed: number;
  succeeded: number;
  total: number;
  failure_rate: number;
}

/** All metrics returned by the telemetry report endpoint. */
export interface TelemetryMetrics {
  event_volume_by_day_and_type: EventVolumeRecord[];
  operational_failures_by_type_and_day: OperationalFailureRecord[];
  stock_insufficiency_by_day_warehouse_category: StockInsufficiencyRecord[];
  auth_failure_rate: AuthFailureRateRecord[];
}

/** Top-level shape returned by ``GET /telemetry/report``. */
export interface TelemetryReport {
  period: TelemetryPeriod;
  metrics: TelemetryMetrics;
}