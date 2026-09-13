/**
 * Inventory Products page for TrackFlow Backoffice.
 *
 * Displays the full list of SKUs by consuming the real
 * ``getInventoryProducts()`` API function.
 *
 * The four canonical UI states (loading / error / empty / success)
 * are delegated to the ``<ProductList />`` component.
 *
 * @remarks
 * - Requires authentication via ``<AuthGuard />``.
 * - Uses the existing ``<BackofficeHeader />`` for consistent layout.
 * - Current stock is displayed with visual status (colour-coded badge)
 *   and a text label (Healthy / Low / Critical).
 * - ``<ProductList />`` provides per-SKU actions linking to the
 *   inbound and outbound order pages.
 * - Filters, search, and pagination remain out of scope for this
 *   phase.
 */

"use client";

import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { ProductList } from "@/components/inventory/ProductList";
import { getInventoryProducts } from "@/lib/inventoryApi";
import type { SKUResponse } from "@/types/inventory";
import { useApiResource } from "@/hooks/useApiResource";

export default function InventoryProductsPage() {
  const {
    data: products,
    isLoading,
    error: loadError,
  } = useApiResource<SKUResponse[]>(
    getInventoryProducts,
    "Failed to load inventory products.",
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
              Inventory Products
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Overview of all registered SKUs and their current stock.
            </p>
          </header>

          <ProductList
            products={products}
            isLoading={isLoading}
            errorMessage={loadError}
          />
        </main>
      </div>
    </AuthGuard>
  );
}