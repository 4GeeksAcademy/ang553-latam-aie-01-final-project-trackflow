/**
 * Inbound Stock Form for TrackFlow Backoffice.
 *
 * Allows a warehouse operator to register a merchandise receipt
 * (StockEntry) by selecting a SKU, entering a quantity, a client
 * reference, and confirming the warehouse derived from the selected
 * SKU.
 *
 * **Fase 3** — Full form with real POST integration.  Client-side
 * validation runs first; if the form is valid, ``createStockEntry()``
 * is called to persist the movement on the backend.  Submit, success
 * and error states are reflected in the UI.
 *
 * @remarks
 * - Loading / error / empty states from the product fetch are handled
 *   here so that the parent page only deals with data orchestration.
 * - Warehouse is always derived from the selected SKU; the operator
 *   never picks it independently, eliminating invalid combinations.
 * - ``reference`` is a free-text field (client dispatch / receipt
 *   reference such as ``PO-2024-0098``).
 * - ``quantity`` is validated strictly as integer > 0.
 * - ``createStockEntry()`` is called exactly once per valid submit.
 * - Double-click / network-backoff protection via ``isSubmitting``.
 */

"use client";

import { useState, useCallback, useRef } from "react";
import { createStockEntry, ApiError } from "@/lib/inventoryApi";
import type { SKUResponse } from "@/types/inventory";

/* ── Props ──────────────────────────────────────────────────────────────── */

interface InboundStockFormProps {
  /** The full list of SKUs loaded from the inventory API. */
  products: SKUResponse[];
  /** Whether the initial product fetch is still in progress. */
  isLoading: boolean;
  /** A human-readable error message, or ``null``. */
  errorMessage: string | null;
  /** Optional pre-selected SKU id from ``?sku_id=<id>``. */
  initialSkuId?: number | null;
}

/* ── Validation errors shape ────────────────────────────────────────────── */

interface FormErrors {
  sku?: string;
  quantity?: string;
  reference?: string;
}

/* ── Helpers ────────────────────────────────────────────────────────────── */

/**
 * Run all client-side validations and return ``FormErrors``.
 *
 * ``products`` is used to verify that the selected SKU actually
 * exists — an ``initialSkuId`` that does not match any known product
 * is treated as invalid.
 *
 * Keys are only present when the corresponding field is invalid.
 */
function validateForm(
  effectiveSkuId: number | null,
  quantityRaw: string,
  referenceRaw: string,
  products: SKUResponse[],
): FormErrors {
  const errors: FormErrors = {};

  if (
    !effectiveSkuId ||
    !products.some((p) => p.id === effectiveSkuId)
  ) {
    errors.sku = "Select a product.";
  }

  const qty = Number(quantityRaw);

  if (!quantityRaw) {
    errors.quantity = "Quantity is required.";
  } else if (!Number.isInteger(qty) || qty <= 0) {
    errors.quantity = "Quantity must be a positive integer (> 0).";
  }

  if (!referenceRaw || referenceRaw.trim().length === 0) {
    errors.reference = "Reference is required.";
  }

  return errors;
}

/* ── Component ──────────────────────────────────────────────────────────── */

/**
 * Inbound stock registration form.
 *
 * Displays loading / error / empty / success / submit states, runs
 * client-side validation, and persists valid submissions to the
 * backend via ``createStockEntry()``.
 */
export function InboundStockForm({
  products,
  isLoading,
  errorMessage,
  initialSkuId,
}: InboundStockFormProps) {
  /* ── User-interaction state ─────────────────────────────────────── */

  const [manualSkuId, setManualSkuId] = useState<number | null>(null);
  const [userInteracted, setUserInteracted] = useState(false);
  const [quantity, setQuantity] = useState("");
  const [reference, setReference] = useState("");
  const [errors, setErrors] = useState<FormErrors>({});

  /* ── Submit state (Fase 3 — real POST) ──────────────────────────── */

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitSuccess, setSubmitSuccess] = useState<string | null>(null);

  /* Ref to prevent double-submit across re-renders. */
  const submitInFlightRef = useRef(false);

  /* ── Effective SKU — declarative, no effect ─────────────────────── */

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

  /* ── Derive warehouse from effective SKU ────────────────────────── */

  const selectedProduct =
    products.find((p) => p.id === effectiveSkuId) ?? null;

  const warehouse = selectedProduct?.warehouse ?? null;

  /* ── Validation & submit ────────────────────────────────────────── */

  const runValidation = useCallback(
    (): FormErrors => validateForm(effectiveSkuId, quantity, reference, products),
    [effectiveSkuId, quantity, reference, products],
  );

  const getSubmitErrorMessage = (error: unknown): string => {
    if (error instanceof ApiError) {
      return error.message;
    }
    if (error instanceof Error) {
      return error.message;
    }
    return "An unexpected error occurred while registering the inbound entry.";
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // Guard against double-submit (network-level protection).
    if (submitInFlightRef.current) return;

    // Clear previous submit feedback.
    setSubmitSuccess(null);
    setSubmitError(null);

    const validationErrors = runValidation();
    setErrors(validationErrors);

    if (Object.keys(validationErrors).length > 0) {
      return;
    }

    // Safety check: selectedProduct must exist before building payload.
    if (!selectedProduct) {
      setSubmitError("Cannot submit: no product selected.");
      return;
    }

    // ── Build payload ──────────────────────────────────────
    const payload = {
      sku_id: effectiveSkuId as number,
      quantity: Number(quantity),
      reference: reference.trim(),
      warehouse: selectedProduct.warehouse,
    };

    // ── Submit ──────────────────────────────────────────────
    setIsSubmitting(true);
    submitInFlightRef.current = true;

    try {
      const entry = await createStockEntry(payload);

      // ── Success ───────────────────────────────────────────
      const skuLabel = selectedProduct.name;
      const successMsg = `Inbound stock entry recorded: ${entry.quantity} units of ${skuLabel} at ${entry.warehouse}.`;
      setSubmitSuccess(successMsg);

      // Reset quantity and reference; keep SKU selected.
      setQuantity("");
      setReference("");
      setErrors({});
    } catch (error: unknown) {
      setSubmitError(getSubmitErrorMessage(error));
    } finally {
      setIsSubmitting(false);
      submitInFlightRef.current = false;
    }
  };

  /* ── Clear a single field error when the user starts typing ──────── */

  const clearError = (field: keyof FormErrors) => {
    setErrors((prev) => {
      const next = { ...prev };
      delete next[field];
      return next;
    });
  };

  /* ── Render: loading ────────────────────────────────────────────── */

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
        <p className="mt-3 text-sm text-slate-400">
          Loading products…
        </p>
      </section>
    );
  }

  /* ── Render: error ──────────────────────────────────────────────── */

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

  /* ── Render: empty ──────────────────────────────────────────────── */

  if (products.length === 0) {
    return (
      <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
        <h3 className="text-lg font-semibold text-white">
          Register inbound
        </h3>
        <p className="mt-2 text-sm text-slate-400">
          No SKUs are currently registered.  An inbound movement cannot
          be recorded.
        </p>
      </section>
    );
  }

  /* ── Render: form (success) ─────────────────────────────────────── */

  return (
    <form
      onSubmit={handleSubmit}
      noValidate
      className="rounded-2xl border border-white/10 bg-slate-900/70 p-6"
    >
      <h3 className="text-xl font-semibold text-white">
        Register inbound
      </h3>
      <p className="mt-1 text-sm text-slate-400">
        Record a merchandise receipt in a TrackFlow warehouse.
      </p>

      {/* ── SKU / Product ──────────────────────────────────── */}
      <div className="mt-6">
        <label
          htmlFor="sku-select"
          className="block text-sm font-medium text-slate-300"
        >
          SKU / Product
        </label>
        <select
          id="sku-select"
          value={effectiveSkuId ?? ""}
          onChange={(e) => {
            const val = e.target.value;
            setManualSkuId(val ? Number(val) : null);
            setUserInteracted(true);
            clearError("sku");
          }}
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

      {/* ── Quantity ───────────────────────────────────────── */}
      <div className="mt-5">
        <label
          htmlFor="inbound-quantity"
          className="block text-sm font-medium text-slate-300"
        >
          Quantity
        </label>
        <input
          id="inbound-quantity"
          type="number"
          min={1}
          step={1}
          value={quantity}
          onChange={(e) => {
            setQuantity(e.target.value);
            clearError("quantity");
          }}
          placeholder="e.g. 50"
          className={`mt-1.5 block w-full rounded-lg border bg-slate-800 px-3 py-2.5 text-sm text-white transition-colors placeholder:text-slate-500 focus:outline-none focus:ring-2 ${
            errors.quantity
              ? "border-rose-500/50 focus:ring-rose-500/40"
              : "border-white/10 focus:border-cyan-500/50 focus:ring-cyan-500/40"
          }`}
        />
        {errors.quantity && (
          <p className="mt-1 text-xs text-rose-300">{errors.quantity}</p>
        )}
      </div>

      {/* ── Reference ──────────────────────────────────────── */}
      <div className="mt-5">
        <label
          htmlFor="inbound-reference"
          className="block text-sm font-medium text-slate-300"
        >
          Reference
        </label>
        <input
          id="inbound-reference"
          type="text"
          value={reference}
          onChange={(e) => {
            setReference(e.target.value);
            clearError("reference");
          }}
          placeholder="e.g. PO-2024-0098"
          className={`mt-1.5 block w-full rounded-lg border bg-slate-800 px-3 py-2.5 text-sm text-white transition-colors placeholder:text-slate-500 focus:outline-none focus:ring-2 ${
            errors.reference
              ? "border-rose-500/50 focus:ring-rose-500/40"
              : "border-white/10 focus:border-cyan-500/50 focus:ring-cyan-500/40"
          }`}
        />
        {errors.reference && (
          <p className="mt-1 text-xs text-rose-300">{errors.reference}</p>
        )}
      </div>

      {/* ── Warehouse (read-only, derived from SKU) ─────────── */}
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

      {/* ── Submit ─────────────────────────────────────────── */}
      <div className="mt-8 flex items-center justify-between gap-4 border-t border-white/10 pt-6">
        <div className="flex flex-col gap-1">
          {submitSuccess && (
            <p className="text-sm font-medium text-emerald-300">
              {submitSuccess}
            </p>
          )}
          {submitError && (
            <p className="text-sm text-rose-300">
              {submitError}
            </p>
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
          {isSubmitting ? "Registering…" : "Register inbound"}
        </button>
      </div>
    </form>
  );
}