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

import { useEffect, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { OrderHistory } from "@/components/inventory/OrderHistory";
import {
  ApiError,
  getInventoryOrders,
} from "@/lib/inventoryApi";
import type { InventoryOrderResponse } from "@/types/inventory";

/**
 * Extract a user-safe message from an arbitrary error value.
 * Prefers ``ApiError.message``, falls back to ``Error.message``,
 * then uses a generic fallback string.
 */
function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return fallback;
}

export default function InventoryOrdersPage() {
  const [orders, setOrders] = useState<InventoryOrderResponse[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setIsLoading(true);
      setLoadError(null);

      try {
        const data = await getInventoryOrders();
        if (!cancelled) {
          setOrders(data);
        }
      } catch (error: unknown) {
        if (!cancelled) {
          setLoadError(
            getErrorMessage(error, "Failed to load inventory order history."),
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

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