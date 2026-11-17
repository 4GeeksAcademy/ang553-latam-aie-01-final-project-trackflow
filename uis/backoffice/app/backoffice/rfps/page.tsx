"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import { getRfpTicket, listRfpTickets, RfpApiError, uploadRfp } from "@/lib/rfpApi";
import type { RfpDepartmentId, RfpJobStatus, RfpSourceReference, RfpTicketDetail, RfpTicketListItem, RfpTicketStatus } from "@/types/rfp";

const POLL_INTERVAL_MS = 3000;
const DEPARTMENT_LABELS: Record<RfpDepartmentId, string> = {
  warehouse: "Warehouse Operations",
  lastmile: "Last Mile and Carrier Management",
  reverse: "Reverse Logistics",
};
const TICKET_LABELS: Record<RfpTicketStatus, string> = {
  analizando: "Analizando",
  descartado: "Descartado",
  analisis_completo: "Análisis completo",
};
const JOB_LABELS: Record<RfpJobStatus, string> = {
  queued: "En cola",
  running: "En ejecución",
  retry_wait: "Esperando reintento",
  succeeded: "Finalizado",
  failed: "Fallo técnico",
  dead_letter: "Requiere intervención",
};

function displayDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("es-ES");
}

function displaySourceReference(reference: RfpSourceReference): string {
  return reference.line_start === reference.line_end
    ? `L${reference.line_start}`
    : `L${reference.line_start}–L${reference.line_end}`;
}

function isPollingTerminal(detail: RfpTicketDetail): boolean {
  return detail.status !== "analizando" || detail.job?.status === "succeeded" || detail.job?.status === "failed" || detail.job?.status === "dead_letter";
}

function statusClasses(status: RfpTicketStatus): string {
  if (status === "analisis_completo") return "border-emerald-400/30 bg-emerald-950/40 text-emerald-200";
  if (status === "descartado") return "border-amber-400/30 bg-amber-950/40 text-amber-200";
  return "border-cyan-400/30 bg-cyan-950/40 text-cyan-200";
}

function TechnicalStatus({ detail }: { detail: RfpTicketDetail }) {
  if (!detail.job) return null;
  const failed = detail.job.status === "failed" || detail.job.status === "dead_letter";
  return (
    <section aria-live="polite" className={`rounded-xl border p-4 ${failed ? "border-rose-400/30 bg-rose-950/30 text-rose-100" : "border-white/10 bg-slate-950/60 text-slate-200"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">Ejecución técnica</h3>
        <span className="text-sm">{JOB_LABELS[detail.job.status]}</span>
      </div>
      <p className="mt-2 text-xs text-slate-400">Intento {detail.job.attempt_count} de {detail.job.max_attempts}</p>
      {failed && (
        <p className="mt-3 text-sm">
          {detail.job.last_error_summary ?? "El procesamiento no pudo completarse. Un administrador debe revisar el job."}
          {detail.job.last_error_code ? ` (${detail.job.last_error_code})` : ""}
        </p>
      )}
    </section>
  );
}

function TicketDetail({ detail }: { detail: RfpTicketDetail }) {
  return (
    <section className="space-y-5 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><p className="text-xs uppercase tracking-wider text-slate-500">Ticket</p><h2 className="mt-1 break-all text-lg font-semibold text-white">{detail.ticket_id}</h2></div>
        <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusClasses(detail.status)}`}>{TICKET_LABELS[detail.status]}</span>
      </div>
      <TechnicalStatus detail={detail} />

      {detail.status === "descartado" && (
        <section className="rounded-xl border border-amber-400/30 bg-amber-950/30 p-4 text-amber-100">
          <h3 className="font-semibold">Documento descartado</h3>
          <p className="mt-2 text-sm">{detail.discard_summary ?? "El clasificador determinó que el documento no es una RFP de cliente."}</p>
          {detail.discard_reason_code && <p className="mt-2 text-xs text-amber-300">Motivo: {detail.discard_reason_code}</p>}
        </section>
      )}

      {detail.metadata && (
        <section>
          <h3 className="text-sm font-semibold uppercase tracking-wider text-cyan-300">Datos de la RFP</h3>
          <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
            <div><dt className="text-slate-500">Cliente</dt><dd className="text-white">{detail.metadata.client_name ?? "No identificado"}</dd></div>
            <div><dt className="text-slate-500">País / moneda</dt><dd className="text-white">{detail.metadata.client_country_code ?? "Pendiente"} / {detail.metadata.currency ?? "Pendiente"}</dd></div>
            <div><dt className="text-slate-500">Volumen mensual</dt><dd className="text-white">{detail.metadata.monthly_volume_value ?? "No indicado"}{detail.metadata.monthly_volume_unit ? ` ${detail.metadata.monthly_volume_unit}` : ""}</dd></div>
            <div><dt className="text-slate-500">Fecha límite</dt><dd className="text-white">{detail.metadata.deadline ?? "No indicada"}</dd></div>
            <div className="sm:col-span-2"><dt className="text-slate-500">Objetivo de onboarding</dt><dd className="text-white">{detail.metadata.onboarding_target ?? "No indicado"}</dd></div>
          </dl>
        </section>
      )}

      {detail.handoff && (
        <section className="rounded-xl border border-cyan-400/20 bg-cyan-950/20 p-4">
          <h3 className="text-sm font-semibold uppercase tracking-wider text-cyan-300">Resumen para Ventas</h3>
          <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-100">{detail.handoff.summary_for_sales}</p>
        </section>
      )}

      {detail.department_sections.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold uppercase tracking-wider text-cyan-300">Workstreams departamentales</h3>
          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            {detail.department_sections.map((section) => (
              <article key={section.department_id} className="rounded-xl border border-white/10 bg-slate-950/60 p-4">
                <h4 className="font-semibold text-white">{DEPARTMENT_LABELS[section.department_id]}</h4>
                <p className="mt-1 text-sm text-cyan-200">Responsable: {section.responsible}</p>
                <h5 className="mt-4 text-xs font-semibold uppercase tracking-wider text-slate-400">Aspectos clave</h5>
                <ul className="mt-2 space-y-3 text-sm text-slate-200">
                  {section.key_aspects.map((aspect, index) => (
                    <li key={`${section.department_id}-aspect-${index}`} className="rounded-lg bg-white/5 p-3">
                      <p>{aspect.fact}</p><p className="mt-1 text-cyan-200">Acción: {aspect.requested_action}</p><p className="mt-1 text-xs text-slate-500">Fuente: {displaySourceReference(aspect.source_ref)}</p>
                    </li>
                  ))}
                </ul>
                {section.open_questions.length > 0 && (
                  <><h5 className="mt-4 text-xs font-semibold uppercase tracking-wider text-amber-300">Preguntas abiertas</h5><ul className="mt-2 list-disc space-y-2 pl-5 text-sm text-amber-100">
                    {section.open_questions.map((question, index) => <li key={`${section.department_id}-question-${index}`}>{question.question} <span className="text-amber-300">— {question.reason}</span>{question.source_ref && <span className="text-xs text-slate-500"> ({displaySourceReference(question.source_ref)})</span>}</li>)}
                  </ul></>
                )}
                {section.detected_conflicts.length > 0 && (
                  <><h5 className="mt-4 text-xs font-semibold uppercase tracking-wider text-rose-300">Conflictos detectados</h5><ul className="mt-2 list-disc space-y-2 pl-5 text-sm text-rose-100">
                    {section.detected_conflicts.map((conflict, index) => <li key={`${section.department_id}-conflict-${index}`}>{conflict.description}{conflict.source_refs.length > 0 && <span className="text-xs text-slate-500"> ({conflict.source_refs.map(displaySourceReference).join(", ")})</span>}</li>)}
                  </ul></>
                )}
              </article>
            ))}
          </div>
        </section>
      )}
      <p className="text-xs text-slate-500">Actualizado: {displayDate(detail.updated_at)}</p>
    </section>
  );
}

function RfpPageContent() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [file, setFile] = useState<File | null>(null);
  const [inputKey, setInputKey] = useState(0);
  const [tickets, setTickets] = useState<RfpTicketListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RfpTicketDetail | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isLoadingList, setIsLoadingList] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acceptance, setAcceptance] = useState<string | null>(null);

  const loadList = useCallback(async () => {
    if (!isAdmin) return;
    setIsLoadingList(true);
    try { setTickets((await listRfpTickets()).items); }
    catch (caught: unknown) { setError(caught instanceof Error ? caught.message : "No se pudo cargar la lista de RFPs."); }
    finally { setIsLoadingList(false); }
  }, [isAdmin]);

  const loadDetail = useCallback(async (ticketId: string, showError = true) => {
    try { const value = await getRfpTicket(ticketId); setDetail(value); return value; }
    catch (caught: unknown) { if (showError) setError(caught instanceof Error ? caught.message : "No se pudo cargar el ticket."); return null; }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void loadList(), 0);
    return () => clearTimeout(timer);
  }, [loadList]);
  useEffect(() => {
    if (!selectedId || !isAdmin) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = async () => {
      const value = await loadDetail(selectedId, !cancelled);
      if (cancelled || (value && isPollingTerminal(value))) return;
      timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [isAdmin, loadDetail, selectedId]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file) { setError("Selecciona un archivo PDF."); return; }
    setIsUploading(true); setError(null); setAcceptance(null);
    try {
      const accepted = await uploadRfp(file);
      setAcceptance(`Ticket ${accepted.ticket_id} aceptado para análisis.`);
      setSelectedId(accepted.ticket_id); setFile(null); setInputKey((value) => value + 1);
      await loadList();
    } catch (caught: unknown) {
      setError(caught instanceof RfpApiError ? caught.message : "No se pudo cargar la RFP.");
    } finally { setIsUploading(false); }
  };

  if (!isAdmin) return <section role="alert" className="rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200">Tu rol no tiene acceso a la recepción de RFPs. Esta función está limitada a administradores durante QA.</section>;

  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
      <div className="space-y-6">
        <form onSubmit={submit} className="rounded-2xl border border-white/10 bg-slate-900/70 p-6">
          <h2 className="text-lg font-semibold text-white">Nueva recepción</h2><p className="mt-2 text-sm text-slate-400">Cada carga crea un ticket independiente y continúa en segundo plano.</p>
          <label htmlFor="rfp-file" className="mt-5 block text-sm font-medium text-slate-200">RFP en PDF</label>
          <input key={inputKey} id="rfp-file" type="file" accept="application/pdf,.pdf" disabled={isUploading} onChange={(event) => setFile(event.target.files?.[0] ?? null)} className="mt-2 block w-full rounded-xl border border-white/10 bg-slate-950 p-3 text-sm text-slate-200 file:mr-4 file:rounded-lg file:border-0 file:bg-cyan-400 file:px-3 file:py-2 file:font-semibold file:text-slate-950" />
          <button type="submit" disabled={isUploading || !file} className="mt-4 rounded-lg bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-60">{isUploading ? "Aceptando…" : "Crear ticket"}</button>
        </form>
        {acceptance && <p role="status" className="rounded-xl border border-emerald-400/30 bg-emerald-950/30 p-4 text-sm text-emerald-200">{acceptance}</p>}
        {error && <p role="alert" className="rounded-xl border border-rose-400/30 bg-rose-950/30 p-4 text-sm text-rose-200">{error}</p>}
        <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-5">
          <div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold text-white">Tickets</h2><button type="button" onClick={() => void loadList()} disabled={isLoadingList} className="text-sm font-medium text-cyan-300 hover:text-cyan-200 disabled:opacity-60">{isLoadingList ? "Actualizando…" : "Actualizar"}</button></div>
          {tickets.length === 0 && !isLoadingList ? <p className="mt-4 text-sm text-slate-400">Todavía no hay tickets disponibles.</p> : (
            <ul className="mt-4 space-y-2">{tickets.map((ticket) => <li key={ticket.ticket_id}><button type="button" onClick={() => { setError(null); setSelectedId(ticket.ticket_id); }} className={`w-full rounded-xl border p-3 text-left transition ${selectedId === ticket.ticket_id ? "border-cyan-400/50 bg-cyan-950/30" : "border-white/10 bg-slate-950/50 hover:border-white/20"}`}>
              <span className="block truncate text-sm font-medium text-white">{ticket.client_name ?? ticket.rfp_id ?? ticket.ticket_id}</span><span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400"><span>{TICKET_LABELS[ticket.status]}</span>{ticket.job_status && <span>{JOB_LABELS[ticket.job_status]}</span>}<span>{displayDate(ticket.created_at)}</span></span>
            </button></li>)}</ul>
          )}
        </section>
      </div>
      <div>{detail && selectedId === detail.ticket_id ? <TicketDetail detail={detail} /> : <section className="rounded-2xl border border-dashed border-white/15 bg-slate-900/40 p-8 text-center text-sm text-slate-400">Selecciona un ticket para consultar su estado y resultado.</section>}</div>
    </div>
  );
}

export default function RfpPage() {
  return <AuthGuard><div className="min-h-screen"><BackofficeHeader /><main className="mx-auto max-w-7xl px-6 py-10"><header className="mb-8"><p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">Commercial intake</p><h1 className="mt-2 text-3xl font-bold text-white">Recepción de RFPs</h1><p className="mt-3 max-w-3xl text-sm text-slate-400">Carga solicitudes de propuesta, consulta el procesamiento asíncrono y revisa los workstreams antes de continuar a la siguiente etapa comercial.</p></header><RfpPageContent /></main></div></AuthGuard>;
}
