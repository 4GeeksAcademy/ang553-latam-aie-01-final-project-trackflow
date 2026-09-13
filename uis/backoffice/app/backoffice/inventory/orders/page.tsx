/**
 * Inventory Orders (history) page for TrackFlow Backoffice.
 *
 * Route: ``/backoffice/inventory/orders``
 *
 * Displays a read-only table of all inventory movements (inbound &
 * outbound) by consuming the real ``getInventoryOrders()`` API.
 *
 * **Fase 5.1** — Base history view with loading, error, empty, and
 * success states.
 *
 * @remarks
 * - Protected by ``<AuthGuard />`` (client-side).
 * - Uses ``<BackofficeHeader />`` for consistent layout.
 * - Data loading is handled at the page level; the table presentation
 *   is delegated to ``<OrderHistory />``.
 * - No write actions (edit, delete, revert, etc.).
 * - No filters, search, or pagination in this phase.
 */

"use client";

import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { OrderHistory } from "@/components/inventory/OrderHistory";
import { getInventoryOrders } from "@/lib/inventoryApi";
import type { InventoryOrderResponse } from "@/types/inventory";
import { useApiResource } from "@/hooks/useApiResource";

export default function InventoryOrdersPage() {
  const {
    data: orders,
    isLoading,
    error: loadError,
  } = useApiResource<InventoryOrderResponse[]>(
    getInventoryOrders,
    "Failed to load inventory order history.",
    [],
  );

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
              Inventory Order History
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              View all inbound and outbound inventory movements.
            </p>
          </header>

          <OrderHistory
            orders={orders}
            isLoading={isLoading}
            errorMessage={loadError}
          />
        </main>
      </div>
    </AuthGuard>
  );
}