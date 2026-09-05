/**
 * Order History component for TrackFlow Backoffice.
 *
 * Displays a read-only table of all inventory movements (inbound &
 * outbound) returned by the backend ``getInventoryOrders()``.
 *
 * **Fase 5.2** — Enhanced presentation with:
 * - Movement badges (coloured, textual) distinguishing inbound,
 *   outbound-dispatch, and outbound-loss.
 * - Specific columns for ``reference``, ``exit_type``, and
 *   ``tracking_number`` so operators can inspect every field the
 *   backend provides.
 * - Full ``user_uuid`` (no truncation).
 * - Timezone-safe ``created_at`` derived from the ISO string
 *   components directly, avoiding implicit ``Date`` conversions.
 *
 * @remarks
 * - Loading / error / empty states are handled here.
 * - No write/action columns (edit, delete, revert, approve, etc.).
 * - No user lookup — only ``user_uuid`` is available.
 * - Order is preserved as received from ``getInventoryOrders()``.
 */

"use client";

import type { InventoryOrderResponse, MovementType, ExitType } from "@/types/inventory";

/* ── Movement badges ────────────────────────────────────────────────────── */

interface BadgeStyle {
  container: string;
  text: string;
}

const MOVEMENT_BADGE: Record<MovementType, BadgeStyle> = {
  inbound: {
    container: "border-emerald-500/30 bg-emerald-500/15",
    text: "text-emerald-200",
  },
  outbound: {
    container: "border-rose-500/30 bg-rose-500/15",
    text: "text-rose-200",
  },
};

const EXIT_TYPE_BADGE: Record<ExitType, BadgeStyle> = {
  dispatch: {
    container: "border-amber-500/30 bg-amber-500/15",
    text: "text-amber-200",
  },
  loss: {
    container: "border-slate-500/30 bg-slate-500/15",
    text: "text-slate-300",
  },
};

/* ── Props ──────────────────────────────────────────────────────────────── */

interface OrderHistoryProps {
  /** The full list of inventory orders from the API. */
  orders: InventoryOrderResponse[];
  /** Whether the initial data fetch is still in progress. */
  isLoading: boolean;
  /** A human-readable error message, or null. */
  errorMessage: string | null;
}

/* ── Safe date formatting ───────────────────────────────────────────────── */

/**
 * Format an ISO-8601 date string to ``YYYY-MM-DD HH:mm:ss``.
 *
 * **Timezone strategy** — The value is parsed textually from the ISO
 * string components (``YYYY-MM-DDTHH:mm:ss``) without going through
 * ``new Date()``, which would introduce an implicit conversion to the
 * local timezone.  This guarantees the timestamp displayed matches
 * exactly what the backend sent, regardless of the observer's locale.
 *
 * If the string does not match the expected ISO pattern the raw value
 * is returned as a fallback so no data is silently lost.
 *
 * Examples: ``"2026-09-05 14:30:00"``, ``"2026-09-05 02:05:00"``
 */
function formatDate(raw: string): string {
  if (!raw) return "—";
  // Extract date and time components from ISO 8601 without timezone
  // conversion.  Handles "2026-09-05T14:30:00", "2026-09-05T14:30:00Z",
  // "2026-09-05T14:30:00+02:00" — the regex only captures the first
  // 19 characters (date + time).
  const match = raw.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  if (match) {
    return `${match[1]} ${match[2]}`;
  }
  // Fallback: show the raw string so information is never hidden.
  return raw;
}

/* ── Skeleton rows ──────────────────────────────────────────────────────── */

/** Number of columns in the data table — must match thead. */
const COLUMN_COUNT = 9;

function SkeletonRow() {
  return (
    <tr className="border-b border-white/5">
      {Array.from({ length: COLUMN_COUNT }).map((_, i) => (
        <td key={i} className="px-4 py-3">
          <div className="h-4 w-20 rounded bg-slate-700/60 animate-pulse" />
        </td>
      ))}
    </tr>
  );
}

/* ── Component ──────────────────────────────────────────────────────────── */

/**
 * Order history table.
 *
 * Shows a loading skeleton, error panel, empty-state message, or the
 * full responsive table of movements.
 */
export function OrderHistory({
  orders,
  isLoading,
  errorMessage,
}: OrderHistoryProps) {
  /* ── Loading ──────────────────────────────────────────────────── */
  if (isLoading) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-white/10 text-xs font-semibold uppercase tracking-[0.08em] text-slate-400">
                <th className="px-4 py-3">Movement</th>
                <th className="px-4 py-3">Type</th>
                <th className="px-4 py-3">Product</th>
                <th className="px-4 py-3">SKU</th>
                <th className="px-4 py-3">Qty</th>
                <th className="px-4 py-3">Warehouse</th>
                <th className="px-4 py-3">Reference / Tracking</th>
                <th className="whitespace-nowrap px-4 py-3">Created At</th>
                <th className="px-4 py-3">Created By</th>
              </tr>
            </thead>
            <tbody>
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-sm text-slate-400">Loading movements…</p>
      </section>
    );
  }

  /* ── Error ────────────────────────────────────────────────────── */
  if (errorMessage) {
    return (
      <section className="rounded-2xl border border-rose-800/30 bg-rose-950/30 p-6">
        <h3 className="text-base font-semibold text-rose-200">
          Could not load order history
        </h3>
        <p className="mt-2 text-sm text-rose-300">{errorMessage}</p>
      </section>
    );
  }

  /* ── Empty ────────────────────────────────────────────────────── */
  if (orders.length === 0) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <h3 className="text-lg font-semibold text-white">Order history</h3>
        <p className="mt-2 text-sm text-slate-400">
          No inventory movements have been recorded yet.
        </p>
      </section>
    );
  }

  /* ── Success — table ─────────────────────────────────────────── */
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-white/10 text-xs font-semibold uppercase tracking-[0.08em] text-slate-400">
              <th className="px-4 py-3">Movement</th>
              <th className="px-4 py-3">Type</th>
              <th className="px-4 py-3">Product</th>
              <th className="px-4 py-3">SKU</th>
              <th className="px-4 py-3">Qty</th>
              <th className="px-4 py-3">Warehouse</th>
              <th className="px-4 py-3">Reference / Tracking</th>
              <th className="whitespace-nowrap px-4 py-3">Created At</th>
              <th className="px-4 py-3">Created By</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr
                key={order.id}
                className="border-b border-white/5 transition-colors last:border-b-0 hover:bg-slate-800/40"
              >
                {/* Movement type badge */}
                <td className="px-4 py-3">
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${
                      MOVEMENT_BADGE[order.movement_type].container
                    } ${MOVEMENT_BADGE[order.movement_type].text}`}
                  >
                    {order.movement_type === "inbound" ? "Inbound" : "Outbound"}
                  </span>
                </td>

                {/* Exit-type badge (only for outbound) */}
                <td className="px-4 py-3">
                  {order.movement_type === "inbound" ? (
                    <span className="text-xs text-slate-500">—</span>
                  ) : order.exit_type === "dispatch" ? (
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${EXIT_TYPE_BADGE.dispatch.container} ${EXIT_TYPE_BADGE.dispatch.text}`}
                    >
                      Dispatch
                    </span>
                  ) : order.exit_type === "loss" ? (
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${EXIT_TYPE_BADGE.loss.container} ${EXIT_TYPE_BADGE.loss.text}`}
                    >
                      Loss
                    </span>
                  ) : (
                    <span className="text-xs text-slate-500">—</span>
                  )}
                </td>

                {/* Product name */}
                <td className="px-4 py-3 text-white">
                  {order.sku?.name ?? (
                    <span className="text-slate-500">Unknown</span>
                  )}
                </td>

                {/* SKU code */}
                <td className="px-4 py-3 font-mono text-xs text-slate-300">
                  {order.sku?.sku ?? (
                    <span className="text-slate-500">—</span>
                  )}
                </td>

                {/* Quantity */}
                <td className="px-4 py-3 text-white">
                  {order.quantity}
                </td>

                {/* Warehouse */}
                <td className="px-4 py-3 text-slate-300">
                  {order.warehouse}
                </td>

                {/* Reference / Tracking number */}
                <td className="px-4 py-3 font-mono text-xs text-slate-300 max-w-[180px] break-all">
                  {order.movement_type === "inbound"
                    ? (order.reference ?? <span className="text-slate-500">—</span>)
                    : order.exit_type === "dispatch"
                      ? (order.tracking_number ?? <span className="text-slate-500">—</span>)
                      : <span className="text-slate-500">—</span>}
                </td>

                {/* Created At */}
                <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                  {formatDate(order.created_at)}
                </td>

                {/* Created By (user_uuid — full, no truncation) */}
                <td className="max-w-[200px] px-4 py-3 font-mono text-xs text-slate-400 break-all">
                  {order.user_uuid ?? <span className="text-slate-500">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}