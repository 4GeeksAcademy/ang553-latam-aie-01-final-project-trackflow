"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { STAGE_OPTIONS, STATUS_OPTIONS } from "@/lib/labels";

export function CandidateFilters() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const searchValue = searchParams.get("search") ?? "";
  const statusValue = searchParams.get("status") ?? "";
  const stageValue = searchParams.get("stage") ?? "";

  function updateParams(key: "search" | "status" | "stage", value: string) {
    const nextParams = new URLSearchParams(searchParams.toString());

    if (value.trim()) {
      nextParams.set(key, value);
    } else {
      nextParams.delete(key);
    }

    const queryString = nextParams.toString();
    router.replace(queryString ? `${pathname}?${queryString}` : pathname, {
      scroll: false,
    });
  }

  function clearFilters() {
    const nextParams = new URLSearchParams(searchParams.toString());
    nextParams.delete("search");
    nextParams.delete("status");
    nextParams.delete("stage");

    const queryString = nextParams.toString();
    router.replace(queryString ? `${pathname}?${queryString}` : pathname, {
      scroll: false,
    });
  }

  return (
    <section className="mb-6 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
      <div className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end">
        <div>
          <label
            htmlFor="candidate-search"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Buscar
          </label>
          <input
            id="candidate-search"
            type="search"
            value={searchValue}
            onChange={(event) => updateParams("search", event.target.value)}
            placeholder="Nombre o email"
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 placeholder:text-slate-400 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
        </div>

        <div>
          <label
            htmlFor="status-filter"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Estado
          </label>
          <select
            id="status-filter"
            value={statusValue}
            onChange={(event) => updateParams("status", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          >
            <option value="">Todos</option>
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="stage-filter"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Etapa
          </label>
          <select
            id="stage-filter"
            value={stageValue}
            onChange={(event) => updateParams("stage", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          >
            <option value="">Todas</option>
            {STAGE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <button
          type="button"
          onClick={clearFilters}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
        >
          Limpiar filtros
        </button>
      </div>
    </section>
  );
}
