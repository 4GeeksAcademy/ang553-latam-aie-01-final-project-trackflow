/**
 * Telemetry Dashboard page for TrackFlow Backoffice.
 *
 * Consumes ``GET /telemetry/report`` and displays the four operational
 * metrics returned by the backend: event volume, operational failures,
 * stock insufficiency, and auth failure rate.
 *
 * This is a **technical** dashboard — no business KPIs, no recalculated
 * metrics, no date pickers. The period defaults to the last 7 days.
 *
 * @remarks
 * - Requires authentication via ``<AuthGuard />``.
 * - Uses the existing ``<BackofficeHeader />`` for consistent layout.
 * - State management via ``useApiResource`` (loading / error / success).
 */

"use client";

import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useApiResource } from "@/hooks/useApiResource";
import { getTelemetryReport } from "@/lib/telemetryReportApi";
import type {
  TelemetryReport,
  EventVolumeRecord,
  OperationalFailureRecord,
  StockInsufficiencyRecord,
  AuthFailureRateRecord,
} from "@/types/telemetry";

/* ── Small summary cards ──────────────────────────────────────────────────── */

function SummaryCard({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-slate-900/70 p-4">
      <p className="text-xs font-medium uppercase tracking-wider text-slate-400">
        {label}
      </p>
      <p className="mt-2 text-2xl font-bold text-white">{value}</p>
    </div>
  );
}

/* ── Generic table component ──────────────────────────────────────────────── */

function DataTable({
  caption,
  columns,
  rows,
  rowKey,
}: {
  caption: string;
  columns: { key: string; label: string }[];
  rows: Record<string, unknown>[];
  rowKey: string;
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm italic text-slate-500">
        No data available for this metric.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-white/10 text-xs font-semibold uppercase tracking-wider text-slate-400">
            {columns.map((col) => (
              <th key={col.key} scope="col" className="px-3 py-2">
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={String(row[rowKey])}
              className="border-b border-white/5 text-slate-200 last:border-0"
            >
              {columns.map((col) => (
                <td key={col.key} className="px-3 py-2 whitespace-nowrap">
                  {formatCell(row[col.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return String(value);
}

/* ── Metric section wrapper ───────────────────────────────────────────────── */

function MetricSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-5 shadow-[0_10px_40px_rgba(15,23,42,0.35)]">
      <h3 className="mb-4 text-lg font-semibold text-white">{title}</h3>
      {children}
    </section>
  );
}

/* ── Auth failure rate display ────────────────────────────────────────────── */

function AuthFailureRateTable({
  records,
}: {
  records: AuthFailureRateRecord[];
}) {
  if (records.length === 0) {
    return (
      <p className="text-sm italic text-slate-500">
        No authentication data for this period.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Auth failure rate by day</caption>
        <thead>
          <tr className="border-b border-white/10 text-xs font-semibold uppercase tracking-wider text-slate-400">
            <th scope="col" className="px-3 py-2">Day</th>
            <th scope="col" className="px-3 py-2">Failed</th>
            <th scope="col" className="px-3 py-2">Succeeded</th>
            <th scope="col" className="px-3 py-2">Total</th>
            <th scope="col" className="px-3 py-2">Failure Rate</th>
          </tr>
        </thead>
        <tbody>
          {records.map((rec) => (
            <tr
              key={rec.day}
              className="border-b border-white/5 text-slate-200 last:border-0"
            >
              <td className="px-3 py-2 whitespace-nowrap">{rec.day}</td>
              <td className="px-3 py-2">{rec.failed}</td>
              <td className="px-3 py-2">{rec.succeeded}</td>
              <td className="px-3 py-2">{rec.total}</td>
              <td className="px-3 py-2 font-medium tabular-nums">
                {(rec.failure_rate * 100).toFixed(1)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ── Helpers for summary cards ────────────────────────────────────────────── */

function totalEvents(records: EventVolumeRecord[]): number {
  return records.reduce((sum, r) => sum + r.count, 0);
}

function totalFailures(records: OperationalFailureRecord[]): number {
  return records.reduce((sum, r) => sum + r.count, 0);
}

function totalStockInsufficiency(records: StockInsufficiencyRecord[]): number {
  return records.reduce((sum, r) => sum + r.count, 0);
}

function totalAuthAttempts(records: AuthFailureRateRecord[]): number {
  return records.reduce((sum, r) => sum + r.total, 0);
}

/* ── Loading skeleton ─────────────────────────────────────────────────────── */

function LoadingSkeleton() {
  return (
    <div className="space-y-8" role="status" aria-live="polite">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="animate-pulse rounded-xl border border-white/10 bg-slate-900/70 p-4"
          >
            <div className="mb-2 h-3 w-20 rounded bg-slate-700" />
            <div className="h-8 w-16 rounded bg-slate-700" />
          </div>
        ))}
      </div>
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="animate-pulse rounded-2xl border border-white/10 bg-slate-900/70 p-5"
        >
          <div className="mb-4 h-5 w-48 rounded bg-slate-700" />
          <div className="space-y-2">
            <div className="h-4 w-full rounded bg-slate-700" />
            <div className="h-4 w-3/4 rounded bg-slate-700" />
            <div className="h-4 w-1/2 rounded bg-slate-700" />
          </div>
        </div>
      ))}
      <p className="text-xs text-slate-500">Loading telemetry report…</p>
    </div>
  );
}

/* ── Error display ────────────────────────────────────────────────────────── */

function ErrorDisplay({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      className="rounded-2xl border border-rose-400/20 bg-rose-950/40 p-6 text-center"
      role="alert"
    >
      <p className="text-sm font-medium text-rose-200">
        Failed to load telemetry report
      </p>
      <p className="mt-2 text-sm text-slate-400">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 inline-flex items-center rounded-lg border border-white/10 bg-slate-800 px-4 py-2 text-sm font-medium text-slate-200 transition-colors hover:bg-slate-700"
        >
          Retry
        </button>
      )}
    </div>
  );
}

/* ── Empty state ──────────────────────────────────────────────────────────── */

function EmptyState() {
  return (
    <div className="rounded-2xl border border-white/10 bg-slate-900/70 p-10 text-center">
      <p className="text-lg font-medium text-slate-300">
        No telemetry data for this period
      </p>
      <p className="mt-2 text-sm text-slate-500">
        The default 7-day window returned no events. Check that the telemetry
        pipeline has ingested events for the current period.
      </p>
    </div>
  );
}

/* ── Page component ───────────────────────────────────────────────────────── */

export default function TelemetryDashboard() {
  const {
    data: report,
    isLoading,
    error,
    reload,
  } = useApiResource<TelemetryReport>(
    getTelemetryReport,
    "Failed to load telemetry report.",
    // Initial empty state matching the real contract shape
    {
      period: { from: "", to: "" },
      metrics: {
        event_volume_by_day_and_type: [],
        operational_failures_by_type_and_day: [],
        stock_insufficiency_by_day_warehouse_category: [],
        auth_failure_rate: [],
      },
    },
  );

  const metrics = report.metrics;
  const eventVolume = metrics.event_volume_by_day_and_type;
  const operationalFailures = metrics.operational_failures_by_type_and_day;
  const stockInsufficiency = metrics.stock_insufficiency_by_day_warehouse_category;
  const authRate = metrics.auth_failure_rate;

  const hasData =
    eventVolume.length > 0 ||
    operationalFailures.length > 0 ||
    stockInsufficiency.length > 0 ||
    authRate.length > 0;

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-7xl px-6 py-10">
          {/* Header */}
          <header className="mb-8">
            <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">
              TrackFlow Internal
            </p>
            <h2 className="mt-2 text-2xl font-bold text-white">
              Telemetry Report
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Operational visibility for the last reported period.
            </p>
          </header>

          {/* Loading state */}
          {isLoading && <LoadingSkeleton />}

          {/* Error state */}
          {!isLoading && error && (
            <ErrorDisplay message={error} onRetry={reload} />
          )}

          {/* Empty state */}
          {!isLoading && !error && !hasData && <EmptyState />}

          {/* Success state */}
          {!isLoading && !error && hasData && (
            <div className="space-y-8">
              {/* Period info */}
              {report.period.from && report.period.to && (
                <p className="text-sm text-slate-400">
                  Period: {report.period.from} — {report.period.to}
                </p>
              )}

              {/* Summary cards (presentation-only sums of received data) */}
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <SummaryCard
                  label="Total events"
                  value={String(totalEvents(eventVolume))}
                />
                <SummaryCard
                  label="Operational failures"
                  value={String(totalFailures(operationalFailures))}
                />
                <SummaryCard
                  label="Stock insufficiency"
                  value={String(totalStockInsufficiency(stockInsufficiency))}
                />
                <SummaryCard
                  label="Auth attempts"
                  value={String(totalAuthAttempts(authRate))}
                />
              </div>

              {/* A — Event Volume */}
              <MetricSection title="A. Event Volume">
                <DataTable
                  caption="Event volume by day and type"
                  columns={[
                    { key: "day", label: "Day" },
                    { key: "event_type", label: "Event Type" },
                    { key: "count", label: "Count" },
                  ]}
                  rows={eventVolume as unknown as Record<string, unknown>[]}
                  rowKey="day"
                />
              </MetricSection>

              {/* B — Operational Failures */}
              <MetricSection title="B. Operational Failures">
                <DataTable
                  caption="Operational failures by type and day"
                  columns={[
                    { key: "day", label: "Day" },
                    { key: "event_type", label: "Failure Type" },
                    { key: "count", label: "Count" },
                  ]}
                  rows={
                    operationalFailures as unknown as Record<string, unknown>[]
                  }
                  rowKey="day"
                />
              </MetricSection>

              {/* C — Stock Insufficiency */}
              <MetricSection title="C. Stock Insufficiency">
                <DataTable
                  caption="Stock insufficiency by day, warehouse and category"
                  columns={[
                    { key: "day", label: "Day" },
                    { key: "warehouse", label: "Warehouse" },
                    { key: "product_category", label: "Product Category" },
                    { key: "count", label: "Count" },
                  ]}
                  rows={
                    stockInsufficiency as unknown as Record<string, unknown>[]
                  }
                  rowKey="day"
                />
              </MetricSection>

              {/* D — Auth Failure Rate */}
              <MetricSection title="D. Auth Failure Rate">
                <AuthFailureRateTable records={authRate} />
              </MetricSection>
            </div>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}