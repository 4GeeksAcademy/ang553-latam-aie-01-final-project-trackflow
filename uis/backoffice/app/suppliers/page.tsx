"use client";

import { useCallback, useEffect, useState } from "react";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { SupplierForm } from "@/components/suppliers/SupplierForm";
import { SupplierList } from "@/components/suppliers/SupplierList";
import {
  ApiError,
  createSupplier,
  getSuppliers,
  updateSupplierRate,
  updateSupplierStatus,
} from "@/lib/suppliersApi";
import {
  SUPPLIER_CATEGORIES,
  type Supplier,
  type SupplierCategory,
  type SupplierCountry,
  type SupplierCreate,
} from "@/types/suppliers";

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return fallback;
}

export default function SuppliersPage() {
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [countryFilter, setCountryFilter] = useState<"all" | SupplierCountry>("all");
  const [categoryFilter, setCategoryFilter] = useState<"all" | SupplierCategory>("all");

  const [isCreating, setIsCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [activeRateMutationId, setActiveRateMutationId] = useState<number | null>(null);
  const [activeStatusMutationId, setActiveStatusMutationId] = useState<number | null>(null);
  const [rowErrorById, setRowErrorById] = useState<Record<number, string | undefined>>({});

  const fetchSuppliers = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);

    try {
      const data = await getSuppliers({
        country: countryFilter === "all" ? undefined : countryFilter,
        category: categoryFilter === "all" ? undefined : categoryFilter,
      });
      setSuppliers(data);
    } catch (error: unknown) {
      setLoadError(getErrorMessage(error, "Failed to load suppliers."));
    } finally {
      setIsLoading(false);
    }
  }, [countryFilter, categoryFilter]);

  useEffect(() => {
    void fetchSuppliers();
  }, [fetchSuppliers]);

  const handleCreate = async (payload: SupplierCreate): Promise<boolean> => {
    setIsCreating(true);
    setCreateError(null);

    try {
      await createSupplier(payload);
      await fetchSuppliers();
      return true;
    } catch (error: unknown) {
      setCreateError(getErrorMessage(error, "Failed to create supplier."));
      return false;
    } finally {
      setIsCreating(false);
    }
  };

  const handleUpdateRate = async (id: number, rate: number) => {
    setActiveRateMutationId(id);
    setRowErrorById((prev) => ({ ...prev, [id]: undefined }));

    try {
      await updateSupplierRate(id, { rate_per_shipment: rate });
      await fetchSuppliers();
    } catch (error: unknown) {
      setRowErrorById((prev) => ({
        ...prev,
        [id]: getErrorMessage(error, "Failed to update supplier rate."),
      }));
    } finally {
      setActiveRateMutationId(null);
    }
  };

  const handleToggleStatus = async (supplier: Supplier) => {
    const nextStatus = supplier.status === "active" ? "suspended" : "active";

    setActiveStatusMutationId(supplier.id);
    setRowErrorById((prev) => ({ ...prev, [supplier.id]: undefined }));

    try {
      await updateSupplierStatus(supplier.id, { status: nextStatus });
      await fetchSuppliers();
    } catch (error: unknown) {
      setRowErrorById((prev) => ({
        ...prev,
        [supplier.id]: getErrorMessage(error, "Failed to update supplier status."),
      }));
    } finally {
      setActiveStatusMutationId(null);
    }
  };

  return (
    <div className="min-h-screen">
      <BackofficeHeader />

      <main className="mx-auto max-w-7xl space-y-8 px-6 py-10">
        <section>
          <p className="text-sm font-semibold uppercase tracking-[0.25em] text-cyan-300">
            Supplier directory
          </p>
          <h2 className="mt-3 text-3xl font-bold text-white">Suppliers</h2>
          <p className="mt-3 max-w-3xl text-slate-300">
            Manage operational suppliers for TrackFlow, including onboarding, filtering,
            rate updates, and status changes.
          </p>
        </section>

        <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
          <h3 className="text-lg font-semibold text-white">Filters</h3>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="space-y-2 text-sm text-slate-300">
              <span>Country</span>
              <select
                value={countryFilter}
                onChange={(event) => setCountryFilter(event.target.value as "all" | SupplierCountry)}
                className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              >
                <option value="all">All countries</option>
                <option value="USA">USA</option>
                <option value="Spain">Spain</option>
              </select>
            </label>

            <label className="space-y-2 text-sm text-slate-300">
              <span>Category</span>
              <select
                value={categoryFilter}
                onChange={(event) => setCategoryFilter(event.target.value as "all" | SupplierCategory)}
                className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              >
                <option value="all">All categories</option>
                {SUPPLIER_CATEGORIES.map((category) => (
                  <option key={category} value={category}>
                    {category}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </section>

        <SupplierForm onCreate={handleCreate} isSubmitting={isCreating} errorMessage={createError} />

        <SupplierList
          suppliers={suppliers}
          isLoading={isLoading}
          errorMessage={loadError}
          activeRateMutationId={activeRateMutationId}
          activeStatusMutationId={activeStatusMutationId}
          rowErrorById={rowErrorById}
          onUpdateRate={handleUpdateRate}
          onToggleStatus={handleToggleStatus}
        />
      </main>
    </div>
  );
}
