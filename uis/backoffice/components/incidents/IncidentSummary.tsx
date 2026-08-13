"use client";

import type { IncidentAnalysisResult } from "@/types/incidents";

// ── Error labels for invalid_breakdown ────────────────────────────────────

const ERROR_LABELS: Record<string, string> = {
  MISSING_INCIDENT_ID: "Missing incident ID",
  INVALID_INCIDENT_ID: "Invalid incident ID",
  MISSING_DATE: "Missing date",
  INVALID_DATE: "Invalid date",
  MISSING_COUNTRY: "Missing country",
  INVALID_COUNTRY: "Invalid country",
  MISSING_CUSTOMER_TYPE: "Missing customer type",
  INVALID_CUSTOMER_TYPE: "Invalid customer type",
  MISSING_TRACKING_NUMBER: "Missing tracking number",
  SHORT_TRACKING_NUMBER: "Short tracking number",
  MISSING_CARRIER: "Missing carrier",
  INVALID_CARRIER_FOR_COUNTRY: "Invalid carrier for country",
  MISSING_CATEGORY: "Missing category",
  INVALID_CATEGORY: "Invalid category",
  MISSING_DESCRIPTION: "Missing description",
  SHORT_DESCRIPTION: "Short description",
  MISSING_STATUS: "Missing status",
  INVALID_STATUS: "Invalid status",
  MISSING_CUSTOMER_EMAIL: "Missing customer email",
  INVALID_CUSTOMER_EMAIL: "Invalid customer email",
  MISSING_SATISFACTION_SCORE_FOR_CLOSED: "Missing satisfaction score (closed)",
  INVALID_SATISFACTION_SCORE: "Invalid satisfaction score",
};

// ── Sub-components ────────────────────────────────────────────────────────

function MetricCard({
  title,
  value,
  accent = "text-white",
}: {
  title: string;
  value: string;
  accent?: string;
}) {
  return (
    <article className="rounded-2xl border border-white/10 bg-slate-900/70 p-5 shadow-[0_10px_40px_rgba(15,23,42,0.35)]">
      <p className="text-sm font-medium text-slate-400">{title}</p>
      <p className={`mt-3 text-3xl font-bold ${accent}`}>{value}</p>
    </article>
  );
}

function SectionCard({
  title,
  isEmpty,
  children,
}: {
  title: string;
  isEmpty: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <h3 className="text-xl font-semibold text-white">{title}</h3>
      {isEmpty && (
        <p className="mt-1 text-sm text-slate-400">
          Available once a CSV file has been analysed.
        </p>
      )}
      <div className="mt-5">{children}</div>
    </section>
  );
}

function BreakdownRow({
  label,
  count,
  total,
  color = "bg-cyan-500",
}: {
  label: string;
  count: number;
  total: number;
  color?: string;
}) {
  const pct = total > 0 ? ((count / total) * 100).toFixed(1) : "0.0";
  return (
    <div className="border-b border-white/5 py-2.5 last:border-none">
      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-300">{label}</span>
        <span className="text-sm font-medium text-white">
          {count} <span className="text-slate-500">&mdash; {pct}%</span>
        </span>
      </div>
      {/* Progress bar */}
      <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full transition-all duration-500 ${color}`}
          style={{ width: `${Math.min(100, parseFloat(pct))}%` }}
        />
      </div>
    </div>
  );
}

function SimpleRow({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-center justify-between border-b border-white/5 py-2.5 last:border-none">
      <span className="text-sm text-slate-400">{label}</span>
      <span className="text-sm font-medium text-white">{value}</span>
    </div>
  );
}

function EmptyRow({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-between border-b border-white/5 py-2.5 last:border-none">
      <span className="text-sm text-slate-400">{label}</span>
      <span className="text-sm text-slate-600">&mdash;</span>
    </div>
  );
}

// ── Labels ────────────────────────────────────────────────────────────────

const CATEGORY_LABELS: Record<string, string> = {
  DAMAGE: "Damage",
  DELAYED_DELIVERY: "Delayed delivery",
  LOST_PARCEL: "Lost parcel",
  RETURN_REQUEST: "Return request",
  WRONG_ADDRESS: "Wrong address",
};

const STATUS_LABELS: Record<string, string> = {
  CLOSED: "Closed",
  DISCARDED: "Discarded",
  OPEN: "Open",
};

const SCORE_LABELS: Record<number, string> = {
  1: "Score 1 \u2014 Very dissatisfied",
  2: "Score 2 \u2014 Dissatisfied",
  3: "Score 3 \u2014 Neutral",
  4: "Score 4 \u2014 Satisfied",
  5: "Score 5 \u2014 Very satisfied",
};

const CATEGORY_ORDER = ["LOST_PARCEL", "DELAYED_DELIVERY", "WRONG_ADDRESS", "RETURN_REQUEST", "DAMAGE"];
const STATUS_ORDER = ["OPEN", "CLOSED", "DISCARDED"];
const COUNTRY_ORDER = ["US", "ES"];

const BAR_COLORS = [
  "bg-cyan-500",
  "bg-emerald-500",
  "bg-violet-500",
  "bg-amber-500",
  "bg-rose-500",
];

// ── Props ─────────────────────────────────────────────────────────────────

interface Props {
  result: IncidentAnalysisResult | null;
  onDownload?: () => void;
  isDownloading?: boolean;
}

// ── Component ─────────────────────────────────────────────────────────────

export function IncidentSummary({ result, onDownload, isDownloading }: Props) {
  const hasData = result !== null;
  const valid = result?.valid_records ?? 1;

  // ── Category (ordered) ──
  const categoryItems = CATEGORY_ORDER.map((key, i) => ({
    key,
    label: CATEGORY_LABELS[key] ?? key,
    count: result?.category_breakdown[key] ?? 0,
    color: BAR_COLORS[i % BAR_COLORS.length],
  }));

  // ── Status (ordered) ──
  const statusItems = STATUS_ORDER.map((key, i) => ({
    key,
    label: STATUS_LABELS[key] ?? key,
    count: result?.status_breakdown[key] ?? 0,
    color: BAR_COLORS[i % BAR_COLORS.length],
  }));

  // ── Country (ordered) ──
  const countryItems = COUNTRY_ORDER.map((key, i) => ({
    key,
    label: key,
    count: result?.country_breakdown[key] ?? 0,
    color: BAR_COLORS[i % BAR_COLORS.length],
  }));

  // ── Invalid breakdown ──
  const invalidEntries = hasData
    ? Object.entries(result.invalid_breakdown).sort((a, b) => b[1] - a[1])
    : [];

  // ── Satisfaction ──
  const scoreKeys = [1, 2, 3, 4, 5];

  return (
    <div className="space-y-8">
      {/* ── Top metric cards ── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          title="Total records"
          value={hasData ? String(result.total_records) : "\u2014"}
        />
        <MetricCard
          title="Valid records"
          value={hasData ? String(result.valid_records) : "\u2014"}
          accent="text-emerald-400"
        />
        <MetricCard
          title="Invalid records"
          value={hasData ? String(result.invalid_records) : "\u2014"}
          accent="text-rose-400"
        />
        <MetricCard
          title="Average satisfaction"
          value={hasData ? String(result.average_satisfaction) : "\u2014"}
          accent="text-cyan-300"
        />
      </div>

      {/* ── Breakdown sections ── */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* ── Category ── */}
        <SectionCard title="Breakdown by category" isEmpty={!hasData}>
          {hasData
            ? categoryItems.map((item) => (
                <BreakdownRow
                  key={item.key}
                  label={item.label}
                  count={item.count}
                  total={valid}
                  color={item.color}
                />
              ))
            : Object.values(CATEGORY_LABELS).map((label) => (
                <EmptyRow key={label} label={label} />
              ))}
        </SectionCard>

        {/* ── Status ── */}
        <SectionCard title="Breakdown by status" isEmpty={!hasData}>
          {hasData
            ? statusItems.map((item) => (
                <BreakdownRow
                  key={item.key}
                  label={item.label}
                  count={item.count}
                  total={valid}
                  color={item.color}
                />
              ))
            : Object.values(STATUS_LABELS).map((label) => (
                <EmptyRow key={label} label={label} />
              ))}
        </SectionCard>

        {/* ── Country ── */}
        <SectionCard title="Breakdown by country" isEmpty={!hasData}>
          {hasData
            ? countryItems.map((item) => (
                <BreakdownRow
                  key={item.key}
                  label={item.label}
                  count={item.count}
                  total={valid}
                  color={item.color}
                />
              ))
            : COUNTRY_ORDER.map((co) => <EmptyRow key={co} label={co} />)}
        </SectionCard>

        {/* ── Invalid breakdown ── */}
        <SectionCard title="Invalid records breakdown" isEmpty={!hasData}>
          {hasData && invalidEntries.length > 0
            ? invalidEntries.map(([code, count]) => (
                <SimpleRow
                  key={code}
                  label={ERROR_LABELS[code] ?? code}
                  value={count}
                />
              ))
            : [<EmptyRow key="placeholder" label="Error detail" />]}
        </SectionCard>

        {/* ── Satisfaction distribution ── */}
        <SectionCard title="Satisfaction distribution" isEmpty={!hasData}>
          {hasData
            ? scoreKeys.map((score) => (
                <SimpleRow
                  key={score}
                  label={SCORE_LABELS[score]}
                  value={result.score_distribution[String(score)] ?? 0}
                />
              ))
            : scoreKeys.map((score) => (
                <EmptyRow key={score} label={SCORE_LABELS[score]} />
              ))}
        </SectionCard>

        {/* ── Satisfaction footer (closed_scored + average) ── */}
        <SectionCard title="Satisfaction summary" isEmpty={!hasData}>
          {hasData ? (
            <>
              <SimpleRow label="Closed & scored" value={result.closed_scored} />
              <SimpleRow
                label="Average"
                value={`${result.average_satisfaction} / 5.00`}
              />
            </>
          ) : (
            <>
              <EmptyRow label="Closed & scored" />
              <EmptyRow label="Average" />
            </>
          )}
        </SectionCard>
      </div>

      {/* ── Export button ── */}
      <div className="flex justify-end">
        <button
          onClick={onDownload}
          disabled={!hasData || isDownloading}
          className={`rounded-lg border px-6 py-3 text-sm font-medium shadow-sm transition ${
            !hasData || isDownloading
              ? "cursor-not-allowed border-white/10 bg-slate-800/50 text-slate-500"
              : "cursor-pointer border-cyan-600/40 bg-cyan-600/20 text-cyan-300 hover:bg-cyan-600/30"
          }`}
        >
          {isDownloading ? "Downloading\u2026" : "Download results CSV"}
        </button>
      </div>
    </div>
  );
}