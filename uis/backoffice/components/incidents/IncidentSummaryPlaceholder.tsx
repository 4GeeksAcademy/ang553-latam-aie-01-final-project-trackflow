const CATEGORY_LABELS = [
  "Damage",
  "Delayed delivery",
  "Lost parcel",
  "Return request",
  "Wrong address",
];

const STATUS_LABELS = ["Closed", "Discarded", "Open"];
const COUNTRY_LABELS = ["US", "ES"];
const SCORE_LABELS = [
  "Score 1 — Very dissatisfied",
  "Score 2 — Dissatisfied",
  "Score 3 — Neutral",
  "Score 4 — Satisfied",
  "Score 5 — Very satisfied",
];

function EmptyRow({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-between border-b border-white/5 py-2.5 last:border-none">
      <span className="text-sm text-slate-400">{label}</span>
      <span className="text-sm text-slate-600">&mdash;</span>
    </div>
  );
}

function EmptySection({ title, rows }: { title: string; rows: string[] }) {
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <h3 className="text-xl font-semibold text-white">{title}</h3>
      <p className="mt-1 text-sm text-slate-400">
        Available once a CSV file has been analysed.
      </p>
      <div className="mt-5">
        {rows.map((label) => (
          <EmptyRow key={label} label={label} />
        ))}
      </div>
    </section>
  );
}

export function IncidentSummaryPlaceholder() {
  return (
    <div className="space-y-8">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {["Total records", "Valid records", "Invalid records", "Average satisfaction"].map(
          (title) => (
            <article
              key={title}
              className="rounded-2xl border border-white/10 bg-slate-900/70 p-5 shadow-[0_10px_40px_rgba(15,23,42,0.35)]"
            >
              <p className="text-sm font-medium text-slate-400">{title}</p>
              <p className="mt-3 text-3xl font-bold text-white">&mdash;</p>
            </article>
          ),
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <EmptySection title="Breakdown by category" rows={CATEGORY_LABELS} />
        <EmptySection title="Breakdown by status" rows={STATUS_LABELS} />
        <EmptySection title="Breakdown by country" rows={COUNTRY_LABELS} />
        <EmptySection title="Invalid records breakdown" rows={["Error detail"]} />
        <EmptySection title="Satisfaction distribution" rows={SCORE_LABELS} />
        <EmptySection
          title="Satisfaction summary"
          rows={["Closed & scored", "Average"]}
        />
      </div>

      <div className="flex justify-end">
        <button
          disabled
          className="cursor-not-allowed rounded-lg border border-white/10 bg-slate-800/50 px-6 py-3 text-sm font-medium text-slate-500 shadow-sm"
        >
          Download results CSV
        </button>
      </div>
    </div>
  );
}
