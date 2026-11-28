/**
 * Outbound Stock Form for TrackFlow Backoffice.
 *
 * Allows a warehouse operator to register a stock exit (outbound)
 * by selecting a SKU, entering a quantity, choosing an exit type
 * (dispatch / loss), and optionally providing a tracking number.
 *
 * **Fase 4.2** — Full form with client-side validation and real POST
 * integration through createStockExit().
 *
 * @remarks
 * - Loading / error / empty states from the product fetch are handled
 *   here so that the parent page only deals with data orchestration.
 * - Warehouse is always derived from the selected SKU; the operator
 *   never picks it independently.
 * - **Available stock** is displayed prominently when a SKU is selected.
 * - Quantity is validated against current_stock on the client side.
 * - Exit type controls whether tracking_number is required:
 *   - dispatch → tracking_number required
 *   - loss → tracking_number hidden/cleared, not required
 * - The backend remains authoritative for stock availability; successful
 *   submissions refresh the selected SKU through the inventory API.
 */

"use client";

import { useState, useCallback, useRef } from "react";
import { ApiError, createStockExit, getInventoryProduct, reviewStockExit } from "@/lib/inventoryApi";
import type { DispatchReviewResponse } from "@/lib/inventoryApi";
import type { SKUResponse, ExitType, StockExitCreate } from "@/types/inventory";
import { useInventoryWorkflowLifecycle } from "@/components/telemetry/InventoryWorkflowLifecycle";

/* ── Stock classification (same thresholds as ProductList) ─────────── */

const STOCK_THRESHOLDS = { critical: 0, low: 10 } as const;

type StockLevel = "healthy" | "low" | "critical";

function getStockLevel(stock: number): StockLevel {
  if (stock <= STOCK_THRESHOLDS.critical) return "critical";
  if (stock <= STOCK_THRESHOLDS.low) return "low";
  return "healthy";
}

const STOCK_LABEL: Record<StockLevel, string> = {
  healthy: "Healthy",
  low: "Low",
  critical: "Critical",
};

const STOCK_BADGE: Record<StockLevel, { container: string; text: string }> = {
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

/* ── Props ──────────────────────────────────────────────────────────────── */

interface OutboundStockFormProps {
  /** The full list of SKUs loaded from the inventory API. */
  products: SKUResponse[];
  /** Whether the initial product fetch is still in progress. */
  isLoading: boolean;
  /** A human-readable error message, or null. */
  errorMessage: string | null;
  /** Optional pre-selected SKU id from ?sku_id=<id>. */
  initialSkuId?: number | null;
}

/* ── Validation errors shape ────────────────────────────────────────────── */

interface FormErrors {
  sku?: string;
  quantity?: string;
  exit_type?: string;
  tracking_number?: string;
}

/* ── Helpers ────────────────────────────────────────────────────────────── */

/**
 * Run all client-side validations and return FormErrors.
 *
 * Keys are only present when the corresponding field is invalid.
 */
function validateForm(
  effectiveSkuId: number | null,
  quantityRaw: string,
  exitType: ExitType | null,
  trackingRaw: string,
  products: SKUResponse[],
): FormErrors {
  const errors: FormErrors = {};

  // SKU
  if (!effectiveSkuId || !products.some((p) => p.id === effectiveSkuId)) {
    errors.sku = "Select a product.";
  }

  // Quantity
  const qty = Number(quantityRaw);

  if (!quantityRaw) {
    errors.quantity = "Quantity is required.";
  } else if (!Number.isInteger(qty) || qty <= 0) {
    errors.quantity = "Quantity must be a positive integer (> 0).";
  }

  // Exit type
  if (!exitType || !["dispatch", "loss"].includes(exitType)) {
    errors.exit_type = "Select an exit type.";
  }

  // Tracking number
  if (exitType === "dispatch" && (!trackingRaw || trackingRaw.trim().length === 0)) {
    errors.tracking_number = "Tracking number is required for dispatch.";
  }

  return errors;
}

/* ── Stock-over-quantity warning check ─────────────────────────────────── */

function getStockWarning(
  quantityRaw: string,
  currentStock: number | null,
): string | null {
  const qty = Number(quantityRaw);
  if (currentStock === null || !Number.isInteger(qty) || qty <= 0) return null;
  // Ensure qty > 0 for meaningful comparison
  if (qty > currentStock) {
    return "Quantity cannot exceed available stock (" + currentStock + ").";
  }
  return null;
}

/* ── Component ──────────────────────────────────────────────────────────── */

/**
 * Outbound stock form.
 *
 * Displays loading / error / empty states, shows stock info for the
 * selected SKU, runs client-side validation, registers the stock exit
 * via real POST (createStockExit()), shows success/error feedback,
 * and refreshes current_stock from the backend after a successful or
 * failed (400) submission.
 */
export function OutboundStockForm({
  products,
  isLoading,
  errorMessage,
  initialSkuId,
}: OutboundStockFormProps) {
  const {
    markValidationError,
    markCreateAttempt,
    markCompletedSuccessfully,
  } = useInventoryWorkflowLifecycle();

  /* User-interaction state */
  const [manualSkuId, setManualSkuId] = useState<number | null>(null);
  const [userInteracted, setUserInteracted] = useState(false);
  const [quantity, setQuantity] = useState("");
  const [exitType, setExitType] = useState<ExitType | null>(null);
  const [trackingNumber, setTrackingNumber] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitSuccess, setSubmitSuccess] = useState<string | null>(null);
  const [refreshWarning, setRefreshWarning] = useState<string | null>(null);
  const [refreshedProduct, setRefreshedProduct] = useState<SKUResponse | null>(null);
  const [pendingReview, setPendingReview] = useState<{
    payload: StockExitCreate;
    fingerprint: string;
    idempotencyKey: string;
    review: DispatchReviewResponse;
  } | null>(null);
  const submitInFlightRef = useRef(false);
  // Retained across edits for this mounted form. A full reload/unmount loses
  // these client-side retry keys; the backend remains the source of truth.
  const ambiguousDispatchKeysRef = useRef(new Map<string, string>());

  /* Effective SKU declarative, no effect */
  const initialSkuIsValid =
    initialSkuId != null &&
    Number.isInteger(initialSkuId) &&
    initialSkuId > 0 &&
    products.some((p) => p.id === initialSkuId);

  const effectiveSkuId = userInteracted
    ? manualSkuId
    : initialSkuIsValid
      ? (initialSkuId as number)
      : null;

  const selectedProduct =
    refreshedProduct?.id === effectiveSkuId
      ? refreshedProduct
      : products.find((p) => p.id === effectiveSkuId) ?? null;

  const warehouse = selectedProduct?.warehouse ?? null;
  const currentStock = selectedProduct?.current_stock ?? null;

  /* Validation & submit */
  const runValidation = useCallback(
    (): FormErrors => validateForm(effectiveSkuId, quantity, exitType, trackingNumber, products),
    [effectiveSkuId, quantity, exitType, trackingNumber, products],
  );

  const getSubmitErrorMessage = (error: unknown): string => {
    if (error instanceof ApiError) return error.message;
    if (error instanceof Error) return error.message;
    return "An unexpected error occurred while registering the outbound movement.";
  };

  const refreshSelectedProduct = async (skuId: number): Promise<boolean> => {
    try {
      const refreshed = await getInventoryProduct(skuId);
      setRefreshedProduct(refreshed);
      return true;
    } catch {
      return false;
    }
  };

  const clearFeedback = () => {
    setSubmitSuccess(null);
    setSubmitError(null);
    setRefreshWarning(null);
    setPendingReview(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (submitInFlightRef.current) return;

    clearFeedback();
    const validationErrors = runValidation();
    setErrors(validationErrors);
    if (Object.keys(validationErrors).length > 0) {
      markValidationError();
      return;
    }

    if (effectiveSkuId === null || !selectedProduct) {
      setSubmitError("Cannot submit: no product selected.");
      return;
    }

    const payload: StockExitCreate = {
      sku_id: effectiveSkuId,
      quantity: Number(quantity),
      exit_type: exitType as ExitType,
      tracking_number: exitType === "dispatch" ? trackingNumber.trim() : null,
      warehouse: selectedProduct.warehouse,
    };
    const payloadFingerprint = JSON.stringify(payload);
    const retainedIdempotencyKey = payload.exit_type === "dispatch"
      ? ambiguousDispatchKeysRef.current.get(payloadFingerprint)
      : undefined;
    const stockWarning = getStockWarning(quantity, currentStock);
    if (stockWarning && retainedIdempotencyKey === undefined) {
      setErrors((prev) => ({ ...prev, quantity: stockWarning }));
      markValidationError();
      return;
    }

    setIsSubmitting(true);
    submitInFlightRef.current = true;

    try {
      let idempotencyKey = retainedIdempotencyKey;
      if (payload.exit_type === "dispatch" && idempotencyKey === undefined) {
        idempotencyKey = crypto.randomUUID();
        ambiguousDispatchKeysRef.current.set(payloadFingerprint, idempotencyKey);
      }
      if (payload.exit_type === "dispatch") {
        const review = await reviewStockExit(payload, idempotencyKey as string);
        setPendingReview({
          payload,
          fingerprint: payloadFingerprint,
          idempotencyKey: idempotencyKey as string,
          review,
        });
        setSubmitSuccess("Review ready. Verify the summary and confirm the dispatch.");
        return;
      }
      markCreateAttempt();
      await createStockExit(payload);
      ambiguousDispatchKeysRef.current.delete(payloadFingerprint);
      markCompletedSuccessfully();

      setSubmitSuccess(
        `Outbound ${payload.exit_type} recorded: ${payload.quantity} units of ${selectedProduct.name} from ${payload.warehouse}.`,
      );
      setQuantity("");
      setTrackingNumber("");
      setErrors({});

      const refreshed = await refreshSelectedProduct(payload.sku_id);
      if (!refreshed) {
        setRefreshWarning(
          "Outbound was recorded, but current stock could not be refreshed.",
        );
      }
    } catch (error: unknown) {
      // Keep the key until a successful response is parsed. A timeout, proxy
      // error, auth response, or malformed success body can follow a commit.
      setSubmitError(getSubmitErrorMessage(error));

      if (error instanceof ApiError && error.statusCode === 400) {
        const refreshed = await refreshSelectedProduct(payload.sku_id);
        if (!refreshed) {
          setRefreshWarning("Current stock could not be refreshed.");
        }
      }
    } finally {
      setIsSubmitting(false);
      submitInFlightRef.current = false;
    }
  };

  const handleConfirmDispatch = async () => {
    if (pendingReview === null || submitInFlightRef.current) return;
    setIsSubmitting(true);
    submitInFlightRef.current = true;
    setSubmitError(null);
    try {
      if (Date.parse(pendingReview.review.expires_at) <= Date.now()) {
        const refreshedReview = await reviewStockExit(
          pendingReview.payload,
          pendingReview.idempotencyKey,
        );
        setPendingReview({ ...pendingReview, review: refreshedReview });
        setSubmitSuccess("Review refreshed. Verify it and confirm again.");
        return;
      }
      markCreateAttempt();
      await createStockExit(
        pendingReview.payload,
        pendingReview.idempotencyKey,
        pendingReview.review.review_proof,
      );
      ambiguousDispatchKeysRef.current.delete(pendingReview.fingerprint);
      markCompletedSuccessfully();
      setSubmitSuccess(
        `Outbound dispatch recorded: ${pendingReview.payload.quantity} units of ${selectedProduct?.name ?? "the selected product"} from ${pendingReview.payload.warehouse}.`,
      );
      setPendingReview(null);
      setQuantity("");
      setTrackingNumber("");
      setErrors({});
      if (!await refreshSelectedProduct(pendingReview.payload.sku_id)) {
        setRefreshWarning("Outbound was recorded, but current stock could not be refreshed.");
      }
    } catch (error: unknown) {
      if (error instanceof ApiError && error.statusCode === 428) {
        try {
          const refreshedReview = await reviewStockExit(
            pendingReview.payload,
            pendingReview.idempotencyKey,
          );
          setPendingReview({ ...pendingReview, review: refreshedReview });
          setSubmitSuccess("Review refreshed. Verify it and confirm again.");
          return;
        } catch (refreshError: unknown) {
          setSubmitError(getSubmitErrorMessage(refreshError));
          return;
        }
      }
      setSubmitError(getSubmitErrorMessage(error));
    } finally {
      setIsSubmitting(false);
      submitInFlightRef.current = false;
    }
  };

  const clearError = (field: keyof FormErrors) => {
    setErrors((prev) => {
      const next = { ...prev };
      delete next[field];
      return next;
    });
  };

  const handleExitTypeChange = (value: string) => {
    if (value === "dispatch" || value === "loss") {
      setExitType(value as ExitType);
      if (value === "loss") {
        setTrackingNumber("");
      }
    } else {
      setExitType(null);
    }
    clearError("exit_type");
    clearError("tracking_number");
    clearFeedback();
  };

  /* Render: loading */
  if (isLoading) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-5 w-48 rounded bg-slate-700" />
          <div className="h-10 w-full rounded bg-slate-800" />
          <div className="h-10 w-full rounded bg-slate-800" />
          <div className="h-10 w-full rounded bg-slate-800" />
          <div className="h-10 w-full rounded bg-slate-800" />
        </div>
        <p className="mt-3 text-sm text-slate-400">Loading products…</p>
      </section>
    );
  }

  /* Render: error */
  if (errorMessage) {
    return (
      <section className="rounded-2xl border border-rose-800/30 bg-rose-950/30 p-6">
        <h3 className="text-base font-semibold text-rose-200">Could not load products</h3>
        <p className="mt-2 text-sm text-rose-300">{errorMessage}</p>
      </section>
    );
  }

  /* Render: empty */
  if (products.length === 0) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <h3 className="text-lg font-semibold text-white">Register outbound</h3>
        <p className="mt-2 text-sm text-slate-400">
          No SKUs are currently registered.  An outbound movement cannot be recorded.
        </p>
      </section>
    );
  }

  /* Render: form */
  return (
    <form
      onSubmit={handleSubmit}
      noValidate
      className="rounded-2xl border border-white/10 bg-slate-900/70 p-6"
    >
      <h3 className="text-xl font-semibold text-white">Register outbound</h3>
      <p className="mt-1 text-sm text-slate-400">
        Record a stock exit from a TrackFlow warehouse.
      </p>

      {/* SKU / Product */}
      <div className="mt-6">
        <label
          htmlFor="outbound-sku-select"
          className="block text-sm font-medium text-slate-300"
        >
          SKU / Product
        </label>
        <select
          id="outbound-sku-select"
          value={effectiveSkuId ?? ""}
          onChange={(e) => {
            const val = e.target.value;
            setManualSkuId(val ? Number(val) : null);
            setUserInteracted(true);
            clearError("sku");
            setRefreshedProduct(null);
            clearFeedback();
          }}
          disabled={isSubmitting}
          className={`mt-1.5 block w-full rounded-lg border bg-slate-800 px-3 py-2.5 text-sm text-white transition-colors focus:outline-none focus:ring-2 ${
            errors.sku
              ? "border-rose-500/50 focus:ring-rose-500/40"
              : "border-white/10 focus:border-cyan-500/50 focus:ring-cyan-500/40"
          }`}
        >
          <option value="" className="bg-slate-800 text-slate-400">
            Select a product…
          </option>
          {products.map((p) => (
            <option
              key={p.id}
              value={p.id}
              className="bg-slate-800 text-white"
            >
              {p.name} — {p.sku} — {p.warehouse}
            </option>
          ))}
        </select>
        {errors.sku && (
          <p className="mt-1 text-xs text-rose-300">{errors.sku}</p>
        )}
      </div>

      {/* Stock available — CRITICAL */}
      {selectedProduct && (
        <div className="mt-5 rounded-lg border border-slate-700/50 bg-slate-800/50 px-4 py-3">
          <p className="text-xs font-medium uppercase tracking-[0.1em] text-slate-400">
            Available stock
          </p>
          <div className="mt-1 flex items-center gap-3">
            <span className="text-2xl font-bold text-white">
              {selectedProduct.current_stock}
            </span>
            <span className="text-sm text-slate-400">units</span>
            {(() => {
              const level = getStockLevel(selectedProduct.current_stock);
              const badge = STOCK_BADGE[level];
              return (
                <span
                  className={`ml-auto inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${badge.container} ${badge.text}`}
                >
                  {STOCK_LABEL[level]}
                </span>
              );
            })()}
          </div>
        </div>
      )}

      {/* Quantity */}
      <div className="mt-5">
        <label
          htmlFor="outbound-quantity"
          className="block text-sm font-medium text-slate-300"
        >
          Quantity
        </label>
        <input
          id="outbound-quantity"
          type="number"
          min={1}
          step={1}
          value={quantity}
          onChange={(e) => {
            setQuantity(e.target.value);
            clearError("quantity");
            clearFeedback();
          }}
          placeholder="e.g. 10"
          disabled={!selectedProduct || isSubmitting}
          className={`mt-1.5 block w-full rounded-lg border bg-slate-800 px-3 py-2.5 text-sm text-white transition-colors placeholder:text-slate-500 focus:outline-none focus:ring-2 disabled:cursor-not-allowed disabled:opacity-50 ${
            errors.quantity
              ? "border-rose-500/50 focus:ring-rose-500/40"
              : "border-white/10 focus:border-cyan-500/50 focus:ring-cyan-500/40"
          }`}
        />
        {errors.quantity && (
          <p className="mt-1 text-xs text-rose-300">{errors.quantity}</p>
        )}
        {/* Stock warning (non-error informational) */}
        {!errors.quantity && getStockWarning(quantity, currentStock) && (
          <p className="mt-1 text-xs text-amber-300">
            {getStockWarning(quantity, currentStock)}
          </p>
        )}
      </div>

      {/* Exit type */}
      <div className="mt-5">
        <label className="block text-sm font-medium text-slate-300">
          Exit type
        </label>
        <div className="mt-1.5 flex gap-4">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="exit_type"
              value="dispatch"
              checked={exitType === "dispatch"}
              onChange={() => handleExitTypeChange("dispatch")}
              disabled={isSubmitting}
              className="accent-cyan-500"
            />
            <span className="text-sm text-white">Dispatch</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="exit_type"
              value="loss"
              checked={exitType === "loss"}
              onChange={() => handleExitTypeChange("loss")}
              disabled={isSubmitting}
              className="accent-cyan-500"
            />
            <span className="text-sm text-white">Loss</span>
          </label>
        </div>
        {errors.exit_type && (
          <p className="mt-1 text-xs text-rose-300">{errors.exit_type}</p>
        )}
      </div>

      {/* Tracking number (conditional on exit type) */}
      {exitType === "dispatch" && (
        <div className="mt-5">
          <label
            htmlFor="outbound-tracking"
            className="block text-sm font-medium text-slate-300"
          >
            Tracking number
          </label>
          <input
            id="outbound-tracking"
            type="text"
            value={trackingNumber}
            onChange={(e) => {
              setTrackingNumber(e.target.value);
              clearError("tracking_number");
              clearFeedback();
            }}
            disabled={isSubmitting}
            placeholder="e.g. TRK-2024-12345"
            className={`mt-1.5 block w-full rounded-lg border bg-slate-800 px-3 py-2.5 text-sm text-white transition-colors placeholder:text-slate-500 focus:outline-none focus:ring-2 ${
              errors.tracking_number
                ? "border-rose-500/50 focus:ring-rose-500/40"
                : "border-white/10 focus:border-cyan-500/50 focus:ring-cyan-500/40"
            }`}
          />
          {errors.tracking_number && (
            <p className="mt-1 text-xs text-rose-300">{errors.tracking_number}</p>
          )}
        </div>
      )}

      {/* Warehouse (read-only, derived from SKU) */}
      <div className="mt-5">
        <label className="block text-sm font-medium text-slate-300">
          Warehouse
        </label>
        <div className="mt-1.5 flex items-center gap-3 rounded-lg border border-white/10 bg-slate-800/60 px-3 py-2.5 text-sm">
          {warehouse ? (
            <>
              <span className="font-semibold text-white">{warehouse}</span>
              <span className="text-slate-500">
                {warehouse === "LA" ? "— Los Angeles" : "— Zaragoza"}
              </span>
            </>
          ) : (
            <span className="text-slate-500">
              Select a SKU to see its warehouse.
            </span>
          )}
        </div>
      </div>

      {/* Submit / operation result */}
      {pendingReview && (
        <section className="mt-6 rounded-lg border border-cyan-500/40 bg-cyan-950/30 p-4">
          <h4 className="text-sm font-semibold text-cyan-100">Review dispatch</h4>
          <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
            <dt className="text-slate-400">Product</dt>
            <dd className="text-right text-white">{selectedProduct?.name ?? pendingReview.payload.sku_id}</dd>
            <dt className="text-slate-400">Quantity</dt>
            <dd className="text-right text-white">{pendingReview.review.summary.total_quantity}</dd>
            <dt className="text-slate-400">Warehouse</dt>
            <dd className="text-right text-white">{pendingReview.review.summary.warehouse}</dd>
            <dt className="text-slate-400">Tracking</dt>
            <dd className="text-right text-white">{pendingReview.payload.tracking_number}</dd>
          </dl>
          <p className="mt-3 text-xs text-slate-400">
            Confirming records the stock exit. This is your confirmation as the authorized operator, not an independent approval.
          </p>
          <button
            type="button"
            onClick={handleConfirmDispatch}
            disabled={isSubmitting}
            className="mt-4 rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white hover:bg-cyan-500 disabled:opacity-50"
          >
            {isSubmitting ? "Confirming…" : "Confirm dispatch"}
          </button>
        </section>
      )}

      <div className="mt-8 flex items-center justify-between gap-4 border-t border-white/10 pt-6">
        <div className="flex flex-col gap-1">
          {submitSuccess && (
            <p className="text-sm font-medium text-emerald-300">{submitSuccess}</p>
          )}
          {submitError && (
            <p className="text-sm text-rose-300">{submitError}</p>
          )}
          {refreshWarning && (
            <p className="text-sm text-amber-300">{refreshWarning}</p>
          )}
        </div>

        <button
          type="submit"
          disabled={isSubmitting}
          className={`ml-auto rounded-lg px-5 py-2.5 text-sm font-semibold text-white shadow transition-colors focus:outline-none focus:ring-2 focus:ring-cyan-500/50 ${
            isSubmitting
              ? "cursor-not-allowed bg-cyan-600/50"
              : "bg-cyan-600 hover:bg-cyan-500"
          }`}
        >
          {isSubmitting
            ? "Preparing…"
            : exitType === "dispatch"
              ? "Review dispatch"
              : "Register outbound"}
        </button>
      </div>
    </form>
  );
}
