/**
 * Outbound Stock Exit page for TrackFlow Backoffice.
 *
 * Route: ``/backoffice/inventory/orders/outbound``
 *
 * Loads the full SKU list via ``getInventoryProducts()`` and delegates
 * form rendering to ``<OutboundStockForm />``.
 *
 * Supports pre-selection via ``?sku_id=<id>`` query parameter.
 *
 * **Fase 4** — Full form with client-side validation and real POST
 * integration via createStockExit().
 *
 * @remarks
 * - Protected by ``<AuthGuard />`` (client-side).
 * - Uses ``<BackofficeHeader />`` for consistent layout.
 * - ``useSearchParams()`` is isolated behind a ``<Suspense>`` boundary
 *   to comply with Next.js 16 App Router requirements and avoid build
 *   errors.
 */

"use client";

import { Suspense } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { OutboundStockPageContent } from "./OutboundStockPageContent";

/**
 * Top-level page component.
 *
 * Wraps the content in ``<Suspense>`` because
 * ``OutboundStockPageContent`` uses ``useSearchParams()``.
 */
export default function OutboundStockPage() {
  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-7xl px-6 py-10">
          <header className="mb-8">
            <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">
              TrackFlow Internal
            </p>
            <h2 className="mt-2 text-2xl font-bold text-white">
              Register outbound
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Record a stock exit (dispatch or loss) from a TrackFlow
              warehouse.
            </p>
          </header>

          <Suspense
            fallback={
              <div className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
                <div className="animate-pulse space-y-4">
                  <div className="h-5 w-48 rounded bg-slate-700" />
                  <div className="h-10 w-full rounded bg-slate-800" />
                  <div className="h-10 w-full rounded bg-slate-800" />
                  <div className="h-10 w-full rounded bg-slate-800" />
                </div>
                <p className="mt-3 text-sm text-slate-400">
                  Loading…
                </p>
              </div>
            }
          >
            <OutboundStockPageContent />
          </Suspense>
        </main>
      </div>
    </AuthGuard>
  );
}