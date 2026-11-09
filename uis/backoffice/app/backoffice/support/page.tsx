"use client";

import { FormEvent, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import { askSupport, SupportApiError } from "@/lib/supportApi";
import type { SupportQueryResponse } from "@/types/support";

const statusLabels = {
  ok: "Disponible",
  not_found: "No encontrado o no visible",
  timeout: "Tiempo de espera agotado",
  unavailable: "No disponible",
};

export default function SupportPage() {
  const { user } = useAuth();
  const authorized = user?.role === "admin" || user?.role === "manager";
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<SupportQueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized) {
      setError("Escribe una consulta de soporte.");
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await askSupport(normalized));
    } catch (caught: unknown) {
      setError(
        caught instanceof SupportApiError
          ? caught.message
          : "No se pudo completar la consulta.",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-4xl px-6 py-10">
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">TrackFlow Internal</p>
          <h1 className="mt-2 text-3xl font-bold text-white">Asistente de soporte</h1>
          <p className="mt-3 text-sm text-slate-400">Consulta políticas, tickets e inventario autorizado. Incluye un identificador cuando solicites un registro operativo.</p>

          {!authorized ? (
            <section role="alert" className="mt-8 rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200">Tu rol no tiene acceso al asistente de soporte.</section>
          ) : (
            <form onSubmit={submit} className="mt-8 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
              <label htmlFor="support-question" className="block text-sm font-medium text-slate-200">Consulta</label>
              <textarea id="support-question" rows={5} maxLength={2000} disabled={loading} value={question} onChange={(event) => setQuestion(event.target.value)} className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950 px-4 py-3 text-white outline-none focus:border-cyan-400 disabled:opacity-60" placeholder="Ej.: Check ticket TRF-000482 and product 17 stock." />
              <div className="mt-4 flex items-center justify-between gap-4"><span className="text-xs text-slate-500">{question.length}/2000</span><button type="submit" disabled={loading} className="rounded-lg bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-60">{loading ? "Consultando…" : "Consultar"}</button></div>
            </form>
          )}

          {error && <section role="alert" className="mt-6 rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200">{error}</section>}
          {result && (
            <section aria-live="polite" className="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
              {result.partial && <p className="mb-4 rounded-lg border border-amber-400/30 bg-amber-950/30 p-3 text-sm text-amber-200">Respuesta parcial: una fuente no estuvo disponible.</p>}
              {result.needs_clarification && <p className="mb-4 text-sm font-medium text-amber-200">Se necesita más información.</p>}
              <h2 className="text-lg font-semibold text-white">Respuesta</h2>
              <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-200">{result.answer}</p>
              <p className="mt-4 text-xs uppercase tracking-wider text-slate-500">Ruta: {result.route}</p>
              <div className="mt-5 grid gap-4 md:grid-cols-2">
                {result.ticket && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Ticket</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.ticket.status]}</p>{result.ticket.incident_id && <p className="mt-1 text-sm text-slate-300">{result.ticket.incident_id} · {result.ticket.incident_status}</p>}</article>}
                {result.inventory && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Inventario</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.inventory.status]}</p>{result.inventory.product_id && <p className="mt-1 text-sm text-slate-300">{result.inventory.name} · SKU {result.inventory.sku} · {result.inventory.current_stock} unidades · {result.inventory.warehouse}</p>}</article>}
              </div>
              {result.knowledge && result.knowledge.sources.length > 0 && <div className="mt-5 border-t border-white/10 pt-4"><h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Fuentes</h3><ul className="mt-2 space-y-1 text-sm text-cyan-200">{result.knowledge.sources.map((source) => <li key={`${source.source_document}:${source.section}`}>{source.source_document} · {source.section}</li>)}</ul></div>}
            </section>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}
