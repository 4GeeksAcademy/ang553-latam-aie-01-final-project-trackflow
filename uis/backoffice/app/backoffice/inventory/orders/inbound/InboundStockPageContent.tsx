/**
 * Content component for the Inbound Stock Entry page.
 *
 * Separated from the top-level ``page.tsx`` so that
 * ``useSearchParams()`` can be isolated behind a ``<Suspense>``
 * boundary as required by Next.js 16 App Router.
 *
 * Loads products via ``getInventoryProducts()`` and passes them to
 * ``<InboundStockForm />`` which handles validation, submission via
 * ``createStockEntry()``, and submit/success/error feedback.
 *
 * @remarks
 * - Reads ``?sku_id=<id>`` from the URL and passes it as
 *   ``initialSkuId`` to the form.
 * - Loads products via ``getInventoryProducts()``.
 * - Handles loading / error states.
 */

"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { InboundStockForm } from "@/components/inventory/InboundStockForm";
import {
  ApiError,
  getInventoryProducts,
} from "@/lib/inventoryApi";
import type { SKUResponse } from "@/types/inventory";

/**
 * Extract a user-safe message from an arbitrary error value.
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

/**
 * Safely parse the ``sku_id`` query parameter.
 *
 * Returns ``null`` when the parameter is missing, not a valid integer,
 * or outside the range of positive integers.
 */
function parseSkuIdParam(searchParams: URLSearchParams): number | null {
  const raw = searchParams.get("sku_id");
  if (raw === null) return null;

  const trimmed = raw.trim();
  if (trimmed.length === 0) return null;

  // Ensure it is an integer (no decimals, no leading zeros like "01").
  const asNumber = Number(trimmed);
  if (
    !Number.isInteger(asNumber) ||
    asNumber <= 0 ||
    String(asNumber) !== trimmed
  ) {
    return null;
  }

  return asNumber;
}

export function InboundStockPageContent() {
  const searchParams = useSearchParams();

  const [products, setProducts] = useState<SKUResponse[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  /* ── Load products on mount ─────────────────────────────────────── */

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

  /* ── Parse optional pre-selection ───────────────────────────────── */

  const initialSkuId = parseSkuIdParam(searchParams);

  return (
    <InboundStockForm
      products={products}
      isLoading={isLoading}
      errorMessage={loadError}
      initialSkuId={initialSkuId}
    />
  );
}