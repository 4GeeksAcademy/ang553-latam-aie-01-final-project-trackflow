"use client";

import { useMemo, useState } from "react";
import type { Supplier } from "@/types/suppliers";

interface Props {
  suppliers: Supplier[];
  isLoading: boolean;
  errorMessage: string | null;
  activeRateMutationId: number | null;
  activeStatusMutationId: number | null;
  rowErrorById: Record<number, string | undefined>;
  onUpdateRate: (id: number, rate: number) => Promise<void>;
  onToggleStatus: (supplier: Supplier) => Promise<void>;
}

function formatAmount(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "0.00";
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

export function SupplierList({
  suppliers,
  isLoading,
  errorMessage,
  activeRateMutationId,
  activeStatusMutationId,
  rowErrorById,
  onUpdateRate,
  onToggleStatus,
}: Props) {
  const [editingRateId, setEditingRateId] = useState<number | null>(null);
  const [rateInput, setRateInput] = useState<string>("");
  const [localRowError, setLocalRowError] = useState<string | null>(null);

  const isMutatingRow = (id: number) => activeRateMutationId === id || activeStatusMutationId === id;

  const items = useMemo(() => suppliers, [suppliers]);

  const startEditRate = (supplier: Supplier) => {
    setEditingRateId(supplier.id);
    setRateInput(formatAmount(supplier.rate_per_shipment));
    setLocalRowError(null);
  };

  const cancelEditRate = () => {
    setEditingRateId(null);
    setRateInput("");
    setLocalRowError(null);
  };

  const saveRate = async (supplierId: number) => {
    const parsed = Number(rateInput);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setLocalRowError("Rate must be greater than 0.");
      return;
    }

    setLocalRowError(null);
    await onUpdateRate(supplierId, parsed);
    setEditingRateId(null);
    setRateInput("");
  };

  if (isLoading) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <p className="text-sm text-slate-300">Loading suppliers...</p>
      </section>
    );
  }

  if (errorMessage) {
    return (
      <section className="rounded-2xl border border-rose-800/30 bg-rose-950/30 p-6">
        <p className="text-sm text-rose-300">{errorMessage}</p>
      </section>
    );
  }

  if (items.length === 0) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <h3 className="text-lg font-semibold text-white">Suppliers</h3>
        <p className="mt-2 text-sm text-slate-400">No suppliers found for the selected filters.</p>
      </section>
    );
  }

  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-xl font-semibold text-white">Suppliers</h3>
        <span className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-3 py-1 text-xs font-medium text-cyan-100">
          {items.length} results
        </span>
      </div>

      <div className="mt-5 space-y-4">
        {items.map((supplier) => {
          const isEditingRate = editingRateId === supplier.id;
          const rowError = localRowError && isEditingRate ? localRowError : rowErrorById[supplier.id];
          const isRateLoading = activeRateMutationId === supplier.id;
          const isStatusLoading = activeStatusMutationId === supplier.id;
          const isRowLocked = isMutatingRow(supplier.id);
          const nextStatus = supplier.status === "active" ? "suspended" : "active";

          return (
            <article
              key={supplier.id}
              className="rounded-xl border border-white/8 bg-slate-950/50 p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-lg font-semibold text-white">{supplier.name}</p>
                  <p className="mt-1 text-sm text-slate-400">
                    {supplier.country} · Updated {formatDate(supplier.updated_at)}
                  </p>
                </div>

                <span
                  className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide ${
                    supplier.status === "active"
                      ? "border border-emerald-500/30 bg-emerald-500/15 text-emerald-200"
                      : "border border-amber-500/30 bg-amber-500/15 text-amber-200"
                  }`}
                >
                  {supplier.status}
                </span>
              </div>

              <div className="mt-4 flex flex-wrap gap-2">
                {supplier.categories.map((category) => (
                  <span
                    key={`${supplier.id}-${category}`}
                    className="rounded-full border border-white/15 bg-slate-900/80 px-2.5 py-1 text-xs text-slate-200"
                  >
                    {category}
                  </span>
                ))}
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Rate per shipment</p>
                  {isEditingRate ? (
                    <div className="mt-1 flex items-center gap-2">
                      <input
                        type="number"
                        min="0.01"
                        step="0.01"
                        value={rateInput}
                        onChange={(event) => setRateInput(event.target.value)}
                        className="w-28 rounded-md border border-white/15 bg-slate-900/70 px-2 py-1 text-sm text-white"
                        disabled={isRowLocked}
                      />
                      <button
                        type="button"
                        onClick={() => saveRate(supplier.id)}
                        disabled={isRowLocked}
                        className="rounded-md border border-cyan-600/40 bg-cyan-600/20 px-2 py-1 text-xs font-medium text-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {isRateLoading ? "Saving..." : "Save"}
                      </button>
                      <button
                        type="button"
                        onClick={cancelEditRate}
                        disabled={isRowLocked}
                        className="rounded-md border border-white/20 bg-slate-900/60 px-2 py-1 text-xs text-slate-300 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <p className="mt-1 text-sm font-semibold text-white">
                      {formatAmount(supplier.rate_per_shipment)} {supplier.currency}
                    </p>
                  )}
                </div>

                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Service zone</p>
                  <p className="mt-1 text-sm text-slate-300">{supplier.service_zone ?? "-"}</p>
                </div>

                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Contact</p>
                  <p className="mt-1 text-sm text-slate-300">{supplier.contact_email ?? "-"}</p>
                </div>

                <div>
                  <p className="text-xs uppercase tracking-wide text-slate-500">Notes</p>
                  <p className="mt-1 line-clamp-2 text-sm text-slate-300">{supplier.notes ?? "-"}</p>
                </div>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                {!isEditingRate && (
                  <button
                    type="button"
                    onClick={() => startEditRate(supplier)}
                    disabled={isRowLocked}
                    className="rounded-lg border border-cyan-600/40 bg-cyan-600/20 px-3 py-1.5 text-xs font-medium text-cyan-200 transition hover:bg-cyan-600/30 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Edit rate
                  </button>
                )}

                <button
                  type="button"
                  onClick={() => onToggleStatus(supplier)}
                  disabled={isRowLocked}
                  className="rounded-lg border border-white/20 bg-slate-900/70 px-3 py-1.5 text-xs font-medium text-slate-200 transition hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isStatusLoading ? "Updating..." : nextStatus === "suspended" ? "Suspend" : "Reactivate"}
                </button>
              </div>

              {rowError && (
                <p className="mt-3 rounded-md border border-rose-800/30 bg-rose-950/30 px-2.5 py-2 text-xs text-rose-300">
                  {rowError}
                </p>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
