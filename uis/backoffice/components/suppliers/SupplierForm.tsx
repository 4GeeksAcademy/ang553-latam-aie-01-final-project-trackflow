"use client";

import { useMemo, useState } from "react";
import type { SupplierCategory, SupplierCreate, SupplierCountry, SupplierStatus } from "@/types/suppliers";
import {
  SUPPLIER_CATEGORIES,
  SUPPLIER_COUNTRIES,
  SUPPLIER_STATUSES,
} from "@/types/suppliers";

interface Props {
  onCreate: (payload: SupplierCreate) => Promise<boolean>;
  isSubmitting: boolean;
  errorMessage: string | null;
}

interface FormState {
  name: string;
  country: SupplierCountry;
  categories: SupplierCategory[];
  rate_per_shipment: string;
  status: SupplierStatus;
  service_zone: string;
  contact_email: string;
  notes: string;
}

function countryToCurrency(country: SupplierCountry): "USD" | "EUR" {
  return country === "USA" ? "USD" : "EUR";
}

const INITIAL_STATE: FormState = {
  name: "",
  country: "USA",
  categories: [],
  rate_per_shipment: "",
  status: "active",
  service_zone: "",
  contact_email: "",
  notes: "",
};

export function SupplierForm({ onCreate, isSubmitting, errorMessage }: Props) {
  const [form, setForm] = useState<FormState>(INITIAL_STATE);
  const [localError, setLocalError] = useState<string | null>(null);

  const currency = useMemo(() => countryToCurrency(form.country), [form.country]);

  const setField = <K extends keyof FormState>(field: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const toggleCategory = (category: SupplierCategory) => {
    setForm((prev) => {
      const exists = prev.categories.includes(category);
      return {
        ...prev,
        categories: exists
          ? prev.categories.filter((item) => item !== category)
          : [...prev.categories, category],
      };
    });
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError(null);

    const trimmedName = form.name.trim();
    const trimmedServiceZone = form.service_zone.trim();
    const trimmedContactEmail = form.contact_email.trim();
    const trimmedNotes = form.notes.trim();
    const rate = Number(form.rate_per_shipment);

    if (!trimmedName) {
      setLocalError("Name is required.");
      return;
    }

    if (!form.country) {
      setLocalError("Country is required.");
      return;
    }

    if (form.categories.length === 0) {
      setLocalError("Select at least one category.");
      return;
    }

    if (!Number.isFinite(rate) || rate <= 0) {
      setLocalError("Rate per shipment must be greater than 0.");
      return;
    }

    const expectedCurrency = countryToCurrency(form.country);
    if (currency !== expectedCurrency) {
      setLocalError("Currency must be consistent with country.");
      return;
    }

    if (!form.status) {
      setLocalError("Status is required.");
      return;
    }

    const payload: SupplierCreate = {
      name: trimmedName,
      country: form.country,
      categories: form.categories,
      rate_per_shipment: rate,
      currency,
      status: form.status,
      service_zone: trimmedServiceZone || undefined,
      contact_email: trimmedContactEmail || undefined,
      notes: trimmedNotes || undefined,
    };

    const created = await onCreate(payload);
    if (created) {
      setForm(INITIAL_STATE);
    }
  };

  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-6 shadow-[0_10px_40px_rgba(15,23,42,0.35)]">
      <h3 className="text-xl font-semibold text-white">Register supplier</h3>
      <p className="mt-1 text-sm text-slate-400">
        Add a new supplier for TrackFlow operations.
      </p>

      <form className="mt-6 space-y-5" onSubmit={handleSubmit}>
        <div className="grid gap-4 md:grid-cols-2">
          <label className="space-y-2 text-sm text-slate-300">
            <span>Name *</span>
            <input
              value={form.name}
              onChange={(event) => setField("name", event.target.value)}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Country *</span>
            <select
              value={form.country}
              onChange={(event) => setField("country", event.target.value as SupplierCountry)}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            >
              {SUPPLIER_COUNTRIES.map((country) => (
                <option key={country} value={country}>
                  {country}
                </option>
              ))}
            </select>
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Currency *</span>
            <input
              value={currency}
              readOnly
              className="w-full rounded-lg border border-white/10 bg-slate-900/40 px-3 py-2 text-slate-300"
            />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Rate per shipment *</span>
            <input
              type="number"
              min="0.01"
              step="0.01"
              value={form.rate_per_shipment}
              onChange={(event) => setField("rate_per_shipment", event.target.value)}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            />
          </label>

          <label className="space-y-2 text-sm text-slate-300 md:col-span-2">
            <span>Status *</span>
            <div className="flex flex-wrap gap-3">
              {SUPPLIER_STATUSES.map((status) => {
                const active = form.status === status;
                return (
                  <button
                    type="button"
                    key={status}
                    onClick={() => setField("status", status)}
                    disabled={isSubmitting}
                    className={`rounded-lg border px-3 py-2 text-sm transition ${
                      active
                        ? "border-cyan-500/60 bg-cyan-500/20 text-cyan-200"
                        : "border-white/15 bg-slate-950/40 text-slate-300 hover:text-white"
                    }`}
                  >
                    {status}
                  </button>
                );
              })}
            </div>
          </label>

          <div className="space-y-2 text-sm text-slate-300 md:col-span-2">
            <span>Categories *</span>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {SUPPLIER_CATEGORIES.map((category) => {
                const checked = form.categories.includes(category);
                return (
                  <label
                    key={category}
                    className="flex items-center gap-2 rounded-lg border border-white/10 bg-slate-950/40 px-3 py-2"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleCategory(category)}
                      disabled={isSubmitting}
                    />
                    <span className="text-xs text-slate-200">{category}</span>
                  </label>
                );
              })}
            </div>
          </div>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Service zone</span>
            <input
              value={form.service_zone}
              onChange={(event) => setField("service_zone", event.target.value)}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Contact email</span>
            <input
              value={form.contact_email}
              onChange={(event) => setField("contact_email", event.target.value)}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            />
          </label>

          <label className="space-y-2 text-sm text-slate-300 md:col-span-2">
            <span>Notes</span>
            <textarea
              value={form.notes}
              onChange={(event) => setField("notes", event.target.value)}
              rows={3}
              className="w-full rounded-lg border border-white/10 bg-slate-950/50 px-3 py-2 text-white outline-none transition focus:border-cyan-500"
              disabled={isSubmitting}
            />
          </label>
        </div>

        {(localError || errorMessage) && (
          <p className="rounded-lg border border-rose-800/30 bg-rose-950/30 px-3 py-2 text-sm text-rose-300">
            {localError ?? errorMessage}
          </p>
        )}

        <button
          type="submit"
          disabled={isSubmitting}
          className={`rounded-lg border px-5 py-2.5 text-sm font-medium shadow-sm transition ${
            isSubmitting
              ? "cursor-not-allowed border-cyan-800/40 bg-cyan-900/30 text-cyan-600"
              : "cursor-pointer border-cyan-600/40 bg-cyan-600/20 text-cyan-300 hover:bg-cyan-600/30"
          }`}
        >
          {isSubmitting ? "Creating..." : "Create supplier"}
        </button>
      </form>
    </section>
  );
}
