"use client";

import Link from "next/link";
import type { SKUResponse } from "@/types/inventory";

/* ── Stock classification ──────────────────────────────────────────── */

/**
 * Presentational stock levels for the backoffice UI.
 *
 * These thresholds are **UI-only** and not sourced from the backend.
 *
 * - `current_stock <= 0`   → **critical**
 * - `current_stock` 1…10   → **low**
 * - `current_stock` > 10   → **healthy**
 */
const STOCK_THRESHOLDS = {
  critical: 0,
  low: 10,
} as const;

type StockLevel = "healthy" | "low" | "critical";

function getStockLevel(stock: number): StockLevel {
  if (stock <= STOCK_THRESHOLDS.critical) return "critical";
  if (stock <= STOCK_THRESHOLDS.low) return "low";
  return "healthy";
}

/** Human-readable label for each stock level. */
const STOCK_LABEL: Record<StockLevel, string> = {
  healthy: "Healthy",
  low: "Low",
  critical: "Critical",
};

/** Tailwind classes for the stock badge per level. */
const STOCK_BADGE: Record<
  StockLevel,
  { container: string; text: string }
> = {
  healthy: {
    container: "border-emerald-500/30 bg-emerald-500/15",
    text: "text-emerald-200",
  },
  low: {
    container: "border-amber-500/30 bg-amber-500/15",
    text: "text-amber-200",
  },
  critical: {
    container: "border-rose-500/30 bg-rose-500/15",
    text: "text-rose-200",
  },
};

/* ── Props ──────────────────────────────────────────────────────────── */

interface ProductListProps {
  /** The list of SKUs returned by the inventory API. */
  products: SKUResponse[];
  /** Whether the initial data fetch is in progress. */
  isLoading: boolean;
  /** A human-readable error message, or ``null`` when no error occurred. */
  errorMessage: string | null;
}

/**
 * Table view of inventory SKUs.
 *
 * Handles the four canonical UI states:
 * 1. **Loading**   — skeleton / spinner while the API call is in-flight.
 * 2. **Error**     — clearly visible error with the readable message.
 * 3. **Empty**     — explanatory message when the backend returns ``[]``.
 * 4. **Success**   — the full product table with stock badges and actions.
 */
export function ProductList({
  products,
  isLoading,
  errorMessage,
}: ProductListProps) {
  // ── 1. Loading ──────────────────────────────────────────────────
  if (isLoading) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-5 w-48 rounded bg-slate-700" />
          <div className="h-10 w-full rounded bg-slate-800" />
          <div className="h-10 w-full rounded bg-slate-800" />
          <div className="h-10 w-full rounded bg-slate-800" />
        </div>
        <p className="mt-3 text-sm text-slate-400">Loading products…</p>
      </section>
    );
  }

  // ── 2. Error ────────────────────────────────────────────────────
  if (errorMessage) {
    return (
      <section className="rounded-2xl border border-rose-800/30 bg-rose-950/30 p-6">
        <h3 className="text-base font-semibold text-rose-200">
          Could not load products
        </h3>
        <p className="mt-2 text-sm text-rose-300">{errorMessage}</p>
      </section>
    );
  }

  // ── 3. Empty ────────────────────────────────────────────────────
  if (products.length === 0) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <h3 className="text-lg font-semibold text-white">Products</h3>
        <p className="mt-2 text-sm text-slate-400">
          No SKUs are currently registered.
        </p>
      </section>
    );
  }

  // ── 4. Success ──────────────────────────────────────────────────
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-xl font-semibold text-white">Products</h3>
        <span className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-3 py-1 text-xs font-medium text-cyan-100">
          {products.length} SKUs
        </span>
      </div>

      <div className="mt-5 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-white/10 text-xs uppercase tracking-[0.12em] text-slate-400">
              <th className="py-3 pr-4 font-medium">Product</th>
              <th className="py-3 pr-4 font-medium">SKU</th>
              <th className="py-3 pr-4 font-medium">Client</th>
              <th className="py-3 pr-4 font-medium">Category</th>
              <th className="py-3 pr-4 font-medium">Warehouse</th>
              <th className="py-3 pr-4 font-medium">Stock</th>
              <th className="py-3 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {products.map((product) => {
              const level = getStockLevel(product.current_stock);
              const badge = STOCK_BADGE[level];
              return (
                <tr
                  key={product.id}
                  className="border-b border-white/5 transition-colors hover:bg-slate-800/40"
                >
                  <td className="py-3 pr-4 text-white">{product.name}</td>
                  <td className="py-3 pr-4 font-mono text-slate-300">
                    {product.sku}
                  </td>
                  <td className="py-3 pr-4 text-slate-300">
                    {product.client_name}
                  </td>
                  <td className="py-3 pr-4 text-slate-300">
                    {product.category}
                  </td>
                  <td className="py-3 pr-4 text-slate-300">
                    {product.warehouse}
                  </td>
                  <td className="py-3 pr-4">
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${badge.container} ${badge.text}`}
                    >
                      {product.current_stock}
                      <span className="opacity-80">{STOCK_LABEL[level]}</span>
                    </span>
                  </td>
                  <td className="py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <Link
                        href={`/backoffice/inventory/orders/inbound?sku_id=${product.id}`}
                        className="rounded-md border border-cyan-600/40 bg-cyan-600/20 px-3 py-1.5 text-xs font-medium text-cyan-200 transition-colors hover:bg-cyan-600/30 hover:text-cyan-100"
                      >
                        Register inbound
                      </Link>
                      <Link
                        href={`/backoffice/inventory/orders/outbound?sku_id=${product.id}`}
                        className="rounded-md border border-slate-500/40 bg-slate-800/50 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-700/60 hover:text-white"
                      >
                        Register outbound
                      </Link>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}