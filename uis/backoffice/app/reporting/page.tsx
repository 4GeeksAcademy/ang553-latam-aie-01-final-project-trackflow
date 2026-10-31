"use client";

import { useEffect, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import {
  getWeeklyPerformanceReport,
  ReportingApiError,
} from "@/lib/reportingApi";
import type { WeeklyPerformanceReport } from "@/types/reporting";

function StateMessage({ title, detail }: { title: string; detail: string }) {
  return (
    <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-8 text-center" role="status">
      <h3 className="text-lg font-semibold text-white">{title}</h3>
      <p className="mt-2 text-sm text-slate-400">{detail}</p>
    </section>
  );
}

function formatRate(value: number): string {
  return new Intl.NumberFormat("es-ES", {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(value);
}

export default function ReportingPage() {
  const [report, setReport] = useState<WeeklyPerformanceReport | null>(null);
  const [error, setError] = useState<ReportingApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;

    void getWeeklyPerformanceReport()
      .then((nextReport) => {
        if (!cancelled) setReport(nextReport);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setReport(null);
        setError(
          caught instanceof ReportingApiError
            ? caught
            : new ReportingApiError("No se pudo cargar el reporte semanal."),
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const retry = () => {
    setLoading(true);
    setError(null);
    setReloadKey((value) => value + 1);
  };

  const errorCopy =
    error?.statusCode === 404
      ? ["No hay una publicación disponible", "El pipeline todavía no ha publicado una semana válida."]
      : error?.statusCode === 401
        ? ["Autenticación requerida", "Tu sesión no es válida. Inicia sesión nuevamente."]
        : error?.statusCode === 403
          ? ["Permiso insuficiente", "Este reporte requiere un rol de administrador o manager."]
          : ["Error del servicio de reporting", error?.message ?? "El servicio no está disponible."];

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-7xl px-6 py-10">
          <header className="mb-8">
            <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">TrackFlow Internal</p>
            <h2 className="mt-2 text-2xl font-bold text-white">Reporte Semanal de Desempeño por Almacén y Cliente</h2>
            <p className="mt-2 text-sm text-slate-400">
              {report ? `Semana del ${report.week_start}` : "Última publicación semanal válida"}
            </p>
          </header>

          {loading && <StateMessage title="Cargando reporte" detail="Consultando la publicación semanal…" />}
          {!loading && error && (
            <div>
              <StateMessage title={errorCopy[0]} detail={errorCopy[1]} />
              {error.statusCode !== 401 && error.statusCode !== 403 && (
                <div className="mt-4 text-center">
                  <button
                    type="button"
                    onClick={retry}
                    className="rounded-lg border border-white/10 bg-slate-800 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
                  >
                    Reintentar
                  </button>
                </div>
              )}
            </div>
          )}
          {!loading && report && report.entries.length === 0 && (
            <StateMessage title="Publicación sin filas" detail="La semana fue publicada, pero no contiene combinaciones de almacén y cliente." />
          )}
          {!loading && report && report.entries.length > 0 && (
            <div className="overflow-x-auto rounded-2xl border border-white/10 bg-slate-900/70">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Indicadores semanales por almacén y cliente</caption>
                <thead className="border-b border-white/10 text-xs uppercase tracking-wider text-slate-400">
                  <tr>
                    {[
                      "Almacén",
                      "Cliente",
                      "Volumen de entrada",
                      "Throughput de salida",
                      "Frecuencia de quiebre de stock",
                      "Tasa de discrepancia",
                      "Eventos de discrepancia",
                    ].map((label) => <th key={label} className="px-4 py-3">{label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {report.entries.map((entry) => (
                    <tr key={`${entry.warehouse}:${entry.client_id}`} className="border-b border-white/5 text-slate-200 last:border-0">
                      <td className="px-4 py-3">{entry.warehouse}</td>
                      <td className="px-4 py-3">{entry.client_id}</td>
                      <td className="px-4 py-3 tabular-nums">{entry.inbound_units_count}</td>
                      <td className="px-4 py-3 tabular-nums">{entry.outbound_orders_count}</td>
                      <td className="px-4 py-3 tabular-nums">{entry.stockout_events_count}</td>
                      <td className="px-4 py-3 tabular-nums">{formatRate(entry.discrepancy_rate)}</td>
                      <td className="px-4 py-3 tabular-nums">{entry.discrepancy_events_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}
