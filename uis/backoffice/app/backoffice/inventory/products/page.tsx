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
 * - ``<ProductList />`` provides per-SKU actions (Register inbound /
 *   Register outbound) linking to future order pages.
 * - Filters, search, and pagination remain out of scope for this
 *   phase.
 */

"use client";

import { useEffect, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { ProductList } from "@/components/inventory/ProductList";
import {
  ApiError,
  getInventoryProducts,
} from "@/lib/inventoryApi";
import type { SKUResponse } from "@/types/inventory";

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

export default function InventoryProductsPage() {
  const [products, setProducts] = useState<SKUResponse[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setIsLoading(true);
      setLoadError(null);

      try {
        const data = await getInventoryProducts();
        if (!cancelled) {
          setProducts(data);
        }
      } catch (error: unknown) {
        if (!cancelled) {
          setLoadError(
            getErrorMessage(error, "Failed to load inventory products."),
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