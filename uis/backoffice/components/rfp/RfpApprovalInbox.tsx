"use client";

import { useCallback, useEffect, useState } from "react";
import { getRfpDepartmentReview, getRfpFinalDocument, listRfpApprovalInbox, submitRfpApprovalDecision } from "@/lib/rfpApi";
import type { RfpApprovalAction, RfpApprovalInboxItem, RfpDepartmentReview, RfpFinalDocument } from "@/types/rfp";

const DEPARTMENT_LABELS: Record<RfpApprovalInboxItem["department"], string> = {
  warehouse: "Warehouse Operations",
  lastmile: "Last Mile and Carrier Management",
  reverse: "Reverse Logistics",
};
const ACTION_LABELS: Record<RfpApprovalAction, string> = {
  approve: "Aprobar revisión",
  reject: "Rechazar",
  request_changes: "Solicitar cambios",
};

function stringifySafe(value: unknown): string {
  if (value == null) return "Sin datos adicionales.";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return "Contenido no representable."; }
}

export function RfpApprovalInbox() {
  const [items, setItems] = useState<RfpApprovalInboxItem[]>([]);
  const [selected, setSelected] = useState<RfpApprovalInboxItem | null>(null);
  const [review, setReview] = useState<RfpDepartmentReview | null>(null);
  const [action, setAction] = useState<RfpApprovalAction>("approve");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [finalDocument, setFinalDocument] = useState<RfpFinalDocument | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const next = await listRfpApprovalInbox();
      setItems(next);
      if (selected && !next.some((item) => item.revision_id === selected.revision_id)) {
        setSelected(null); setReview(null);
      }
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : "No se pudo cargar la bandeja de aprobación.");
    } finally { setLoading(false); }
  }, [selected]);

  useEffect(() => { void refresh(); }, [refresh]);

  const openReview = async (item: RfpApprovalInboxItem) => {
    setSelected(item); setReview(null); setAcknowledged(false); setReason("");
    setAction("approve"); setError(null); setMessage(null); setFinalDocument(null);
    try { setReview(await getRfpDepartmentReview(item)); }
    catch (caught: unknown) { setError(caught instanceof Error ? caught.message : "No se pudo cargar la evidencia de la revisión."); }
  };

  const submit = async () => {
    if (!selected || !review || !acknowledged) return;
    if (action !== "approve" && !reason.trim()) { setError("Explica el motivo para rechazar o solicitar cambios."); return; }
    setSubmitting(true); setError(null); setMessage(null);
    try {
      const decidedItem = selected;
      const result = await submitRfpApprovalDecision(
        decidedItem,
        action,
        reason.trim() || null,
        acknowledged,
      );
      setMessage(`Decisión aplicada. Estado del hilo: ${result.status ?? "sin transición adicional"}.`);
      setSelected(null); setReview(null); setAcknowledged(false); setReason("");
      if (typeof result.coordination?.final_document_id === "string") {
        try {
          setFinalDocument(await getRfpFinalDocument(
            decidedItem.ticket_id,
            decidedItem.approval_run_id,
          ));
        } catch (caught: unknown) {
          setError(caught instanceof Error ? caught.message : "No se pudo cargar el documento final.");
        }
      }
      await refresh();
    } catch (caught: unknown) { setError(caught instanceof Error ? caught.message : "No se pudo registrar la decisión."); }
    finally { setSubmitting(false); }
  };

  const markdownUrl = (document: RfpFinalDocument) => URL.createObjectURL(
    new Blob([document.markdown], { type: "text/markdown;charset=utf-8" }),
  );

  const openFinalDocument = () => {
    if (!finalDocument) return;
    const url = markdownUrl(finalDocument);
    window.open(url, "_blank", "noopener,noreferrer");
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  const downloadFinalDocument = () => {
    if (!finalDocument) return;
    const url = markdownUrl(finalDocument);
    const link = document.createElement("a");
    link.href = url;
    link.download = finalDocument.download_filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  return <section aria-labelledby="rfp-approval-heading" className="rounded-2xl border border-cyan-400/20 bg-slate-900/70 p-6">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-[0.25em] text-cyan-300">Human in the loop</p><h2 id="rfp-approval-heading" className="mt-2 text-xl font-semibold text-white">Aprobaciones departamentales</h2><p className="mt-2 max-w-3xl text-sm text-slate-400">Solo aparecen revisiones vigentes para las que tu identidad autenticada conserva un permiso departamental efectivo.</p></div><button type="button" onClick={() => void refresh()} disabled={loading || submitting} className="text-sm font-medium text-cyan-300 hover:text-cyan-200 disabled:opacity-50">{loading ? "Actualizando…" : "Actualizar"}</button></div>
    {error && <p role="alert" className="mt-4 rounded-xl border border-rose-400/30 bg-rose-950/30 p-3 text-sm text-rose-200">{error}</p>}
    {message && <p role="status" className="mt-4 rounded-xl border border-emerald-400/30 bg-emerald-950/30 p-3 text-sm text-emerald-200">{message}</p>}
    {finalDocument && <aside className="mt-4 rounded-xl border border-emerald-400/30 bg-emerald-950/20 p-4" aria-label="Documento final disponible">
      <p className="text-sm font-semibold text-emerald-100">Documento final disponible</p>
      <p className="mt-1 text-sm text-slate-300">{finalDocument.title}</p>
      <div className="mt-3 flex flex-wrap gap-3">
        <button type="button" onClick={openFinalDocument} className="rounded-lg border border-emerald-300/30 px-4 py-2 text-sm font-medium text-emerald-100 hover:bg-emerald-900/40">Abrir Markdown</button>
        <button type="button" onClick={downloadFinalDocument} className="rounded-lg bg-emerald-300 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-emerald-200">Descargar .md</button>
      </div>
      <p className="mt-2 break-all text-xs text-slate-500">Documento {finalDocument.final_document_id} · hash {finalDocument.markdown_hash}</p>
    </aside>}
    <div className="mt-6 grid min-w-0 gap-5 lg:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)]">
      <div className="min-w-0">{items.length === 0 && !loading ? <p className="rounded-xl border border-dashed border-white/15 p-5 text-sm text-slate-400">No hay revisiones pendientes autorizadas para tu identidad.</p> : <ul className="space-y-2">{items.map((item) => <li key={item.revision_id}><button type="button" onClick={() => void openReview(item)} className={`w-full rounded-xl border p-4 text-left ${selected?.revision_id === item.revision_id ? "border-cyan-400/50 bg-cyan-950/30" : "border-white/10 bg-slate-950/50 hover:border-white/20"}`}><span className="block font-medium text-white">{DEPARTMENT_LABELS[item.department]}</span><span className="mt-1 block text-xs text-slate-400">Revisión {item.review_revision_no} · ticket {item.ticket_id}</span><span className="mt-2 block break-all text-xs text-slate-500">Hash: {item.content_hash}</span></button></li>)}</ul>}</div>
      <div className="min-w-0">{!selected ? <p className="rounded-xl border border-dashed border-white/15 p-5 text-sm text-slate-400">Selecciona una revisión para consultar su preview seguro, evidencia y procedencia.</p> : !review ? <p className="rounded-xl border border-white/10 p-5 text-sm text-slate-400">Cargando evidencia verificable…</p> : <article className="min-w-0 space-y-5 rounded-xl border border-white/10 bg-slate-950/60 p-5">
        <div><h3 className="font-semibold text-white">{DEPARTMENT_LABELS[review.department]} · revisión {selected.review_revision_no}</h3><p className="mt-1 text-xs text-slate-400">Validada: {new Date(review.validated_at).toLocaleString("es-ES")}</p></div>
        <div><h4 className="text-xs font-semibold uppercase tracking-wider text-cyan-300">Preview seguro</h4><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-slate-950 p-4 text-sm text-slate-200">{review.safe_preview}</pre></div>
        {review.pending_commercial_items.length > 0 && <div className="rounded-lg border border-amber-400/30 bg-amber-950/30 p-4"><h4 className="text-xs font-semibold uppercase tracking-wider text-amber-200">Propuesta indicativa · pendientes comerciales</h4><p className="mt-2 text-sm text-amber-100">Estos valores no están documentados como condiciones negociadas. Aprobar requiere reconocer expresamente que permanecerán pendientes en el documento.</p><pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs text-amber-100">{stringifySafe(review.pending_commercial_items)}</pre></div>}
        <div className="grid gap-4 md:grid-cols-2"><div><h4 className="text-xs font-semibold uppercase tracking-wider text-cyan-300">Evidencia</h4><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-slate-950 p-3 text-xs text-slate-300">{stringifySafe(review.evidence)}</pre></div><div><h4 className="text-xs font-semibold uppercase tracking-wider text-cyan-300">Procedencia</h4><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-slate-950 p-3 text-xs text-slate-300">{stringifySafe(review.provenance)}</pre></div></div>
        <label className="flex items-start gap-3 rounded-lg border border-amber-400/20 bg-amber-950/20 p-3 text-sm text-amber-100"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} className="mt-1" /><span>{review.pending_commercial_items.length > 0 ? "Confirmo que revisé y reconozco los pendientes comerciales de esta propuesta indicativa, además de su evidencia y procedencia." : "Confirmo que revisé el contenido, su evidencia y procedencia antes de decidir."}</span></label>
        <div><label htmlFor="rfp-approval-action" className="text-sm font-medium text-slate-200">Decisión</label><select id="rfp-approval-action" value={action} onChange={(event) => setAction(event.target.value as RfpApprovalAction)} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 p-3 text-sm text-slate-200">{review.allowed_actions.map((allowed) => <option key={allowed} value={allowed}>{ACTION_LABELS[allowed]}</option>)}</select></div>
        <div><label htmlFor="rfp-approval-reason" className="text-sm font-medium text-slate-200">Motivo {action === "approve" ? "(opcional)" : "(obligatorio)"}</label><textarea id="rfp-approval-reason" value={reason} onChange={(event) => setReason(event.target.value)} rows={3} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 p-3 text-sm text-slate-200" /></div>
        <button type="button" onClick={() => void submit()} disabled={submitting || !acknowledged || (action !== "approve" && !reason.trim())} className="w-full rounded-xl bg-cyan-400 px-5 py-3 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-50">{submitting ? "Registrando decisión…" : ACTION_LABELS[action]}</button>
        <p className="break-all text-xs text-slate-500">Bundle: {review.validation_bundle_id} · contenido: {review.content_hash}</p>
      </article>}</div>
    </div>
  </section>;
}
