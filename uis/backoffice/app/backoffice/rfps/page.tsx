"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { RfpApprovalInbox } from "@/components/rfp/RfpApprovalInbox";
import { useAuth } from "@/lib/AuthContext";
import { getRfpFinalDocumentForTicket, getRfpResponse, getRfpTicket, listRfpTickets, RfpApiError, startRfpResponse, uploadRfp } from "@/lib/rfpApi";
import type { RfpCarrierModel, RfpDepartmentId, RfpFinalDocument, RfpJobStatus, RfpResponseEvaluation, RfpResponseGlobalTerms, RfpResponsePreview, RfpResponseRun, RfpSourceReference, RfpTicketDetail, RfpTicketListItem, RfpTicketStatus } from "@/types/rfp";

const POLL_INTERVAL_MS = 3000;
const SELECTED_TICKET_STORAGE_KEY = "trackflow.rfp.selected-ticket";
const DEPARTMENT_LABELS: Record<RfpDepartmentId, string> = {
  warehouse: "Warehouse Operations",
  lastmile: "Last Mile and Carrier Management",
  reverse: "Reverse Logistics",
};
const TICKET_LABELS: Record<RfpTicketStatus, string> = {
  analizando: "Analizando",
  descartado: "Descartado",
  analisis_completo: "Análisis completo",
  generando_borrador: "Generando borrador",
  en_evaluación: "En evaluación",
  needs_human_review: "Revisión humana necesaria",
  esperando_aprobación: "Esperando aprobación",
  terminado: "Terminado",
};
const JOB_LABELS: Record<RfpJobStatus, string> = {
  queued: "En cola",
  running: "En ejecución",
  retry_wait: "Esperando reintento",
  succeeded: "Finalizado",
  failed: "Fallo técnico",
  dead_letter: "Requiere intervención",
};
const CARRIER_MODEL_LABELS: Record<RfpCarrierModel, string> = {
  unknown: "Sin confirmar",
  trackflow_managed: "Gestionado por TrackFlow",
  customer_managed: "Gestionado por el cliente",
};
const RESPONSE_STATUS_LABELS: Record<RfpResponseRun["status"], string> = {
  pending: "Pendiente de inicio",
  generando_borrador: "Generando borrador",
  en_evaluación: "Evaluación automática en curso",
  ready_for_part3: "Evaluaciones automáticas aprobadas",
  needs_human_review: "Revisión humana necesaria",
  technical_failure: "Fallo técnico operativo",
};
const EVALUATION_STATUS_LABELS: Record<string, string> = {
  passed: "Aprobada automáticamente",
  ready_for_part3: "Aprobada automáticamente",
  needs_human_input: "Requiere información humana",
  iteration_limit_reached: "Límite de regeneraciones alcanzado",
  "en_evaluación": "En evaluación",
  not_started: "Todavía no iniciada",
};
const GLOBAL_COMPLIANCE_LABELS: Record<string, string> = {
  passed: "Evaluación global aprobada",
  failed: "Evaluación global no aprobada",
  "en_evaluación": "Evaluación global en curso",
  not_started: "Evaluación global todavía no ejecutada",
  technical_failure: "Evaluación global no ejecutada por fallo técnico",
};
const SECTION_STATUS_LABELS: Record<string, string> = {
  pending: "Pendiente",
  generando_borrador: "Generando borrador",
  "en_evaluación": "En evaluación",
  passed: "Evaluación automática aprobada",
  needs_human_input: "Requiere información humana",
  iteration_limit_reached: "Límite de regeneraciones alcanzado",
  technical_failure: "Fallo técnico operativo",
};

type GlobalPreviewPayload = {
  currency: { value: "EUR" | "USD" | null; status: "documented" | "pending" };
  sla: {
    applicability: "applicable" | "not_applicable" | "pending";
    percentage: number | null;
    measurement_period: string | null;
    exclusions: string[];
  };
  volume_discounts: Array<{
    volume_from: number | null;
    volume_to: number | null;
    discount_percentage: number | null;
    condition: string;
    status: "documented" | "pending";
  }>;
  pending_commercial_items: Array<{ code: string; description: string; owner: string }>;
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
  if (status === "terminado") return "border-emerald-400/30 bg-emerald-950/40 text-emerald-200";
  if (status === "analisis_completo" || status === "en_evaluación") return "border-emerald-400/30 bg-emerald-950/40 text-emerald-200";
  if (status === "needs_human_review" || status === "esperando_aprobación") return "border-amber-400/30 bg-amber-950/40 text-amber-200";
  if (status === "descartado") return "border-amber-400/30 bg-amber-950/40 text-amber-200";
  return "border-cyan-400/30 bg-cyan-950/40 text-cyan-200";
}

function responseIsTerminal(response: RfpResponseRun | null): boolean {
  return response !== null && ["ready_for_part3", "needs_human_review", "technical_failure"].includes(response.status);
}

function parseGlobalPreview(content: string): GlobalPreviewPayload | null {
  try {
    const value = JSON.parse(content) as GlobalPreviewPayload;
    if (!value || typeof value !== "object" || !value.currency || !value.sla ||
        !Array.isArray(value.sla.exclusions) || !Array.isArray(value.volume_discounts) ||
        !Array.isArray(value.pending_commercial_items)) return null;
    return value;
  } catch {
    return null;
  }
}

function Preview({ preview }: { preview: RfpResponsePreview | null }) {
  if (!preview) return <p className="mt-3 text-sm text-slate-500">Preview todavía no disponible.</p>;
  if (preview.gate_status !== "passed") return (
    <div className="mt-3 rounded-lg border border-amber-400/20 bg-amber-950/20 p-3 text-sm text-amber-100">
      Preview no autorizado ({preview.gate_status}). No se muestra contenido.
    </div>
  );
  return <div className="mt-3 max-w-full whitespace-pre-wrap break-words overflow-hidden rounded-lg border border-emerald-400/20 bg-emerald-950/20 p-3 text-sm leading-6 text-slate-100">{preview.content}</div>;
}

function GlobalPreview({ preview }: { preview: RfpResponsePreview | null }) {
  if (!preview) return <p className="mt-3 text-sm text-slate-500">Preview todavía no disponible.</p>;
  if (preview.gate_status !== "passed" || !preview.content) return (
    <div className="mt-3 rounded-lg border border-amber-400/20 bg-amber-950/20 p-3 text-sm text-amber-100">
      Preview no autorizado. No se muestra contenido comercial.
    </div>
  );
  const value = parseGlobalPreview(preview.content);
  if (!value) return <p className="mt-3 text-sm text-rose-200">El preview público no tiene un formato verificable.</p>;
  const slaLabel = value.sla.applicability === "applicable" ? "Aplicable" : value.sla.applicability === "not_applicable" ? "No aplicable" : "Pendiente de confirmación";
  return <section className="mt-4 min-w-0 space-y-4 rounded-lg border border-emerald-400/20 bg-emerald-950/20 p-4 text-sm text-slate-100">
    <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
      <div><dt className="text-xs font-semibold uppercase text-slate-400">Moneda</dt><dd className="mt-1 break-words">{value.currency.value ?? "Pendiente"} · {value.currency.status === "documented" ? "Documentada" : "Pendiente"}</dd></div>
      <div><dt className="text-xs font-semibold uppercase text-slate-400">Aplicabilidad del SLA</dt><dd className="mt-1 break-words">{slaLabel}</dd></div>
      {value.sla.percentage !== null && <div><dt className="text-xs font-semibold uppercase text-slate-400">Porcentaje</dt><dd className="mt-1">{value.sla.percentage}%</dd></div>}
      {value.sla.measurement_period && <div><dt className="text-xs font-semibold uppercase text-slate-400">Periodo</dt><dd className="mt-1 break-words">{value.sla.measurement_period}</dd></div>}
    </dl>
    <div><h4 className="text-xs font-semibold uppercase text-slate-400">Exclusiones</h4>{value.sla.exclusions.length > 0 ? <ul className="mt-2 list-disc space-y-1 pl-5">{value.sla.exclusions.map((item, index) => <li className="break-words" key={`sla-exclusion-${index}`}>{item}</li>)}</ul> : <p className="mt-1 text-slate-400">Sin exclusiones aplicables.</p>}</div>
    <div className="min-w-0"><h4 className="text-xs font-semibold uppercase text-slate-400">Descuentos por volumen</h4><div className="mt-2 overflow-hidden rounded-lg border border-white/10"><table className="w-full table-fixed text-left text-xs"><thead className="bg-white/5 text-slate-300"><tr><th className="w-1/4 p-2">Volumen</th><th className="w-1/4 p-2">Descuento</th><th className="w-1/2 p-2">Condición</th></tr></thead><tbody>{value.volume_discounts.map((row, index) => <tr key={`discount-${index}`} className="border-t border-white/10 align-top"><td className="break-words p-2">{row.volume_from ?? "—"}–{row.volume_to ?? "∞"} m³</td><td className="break-words p-2">{row.discount_percentage === null ? "Pendiente" : `${row.discount_percentage}%`}</td><td className="break-words p-2">{row.condition}</td></tr>)}</tbody></table></div></div>
    <div><h4 className="text-xs font-semibold uppercase text-amber-300">Información comercial obligatoria pendiente</h4>{value.pending_commercial_items.length > 0 ? <ul className="mt-2 space-y-2 text-amber-100">{value.pending_commercial_items.map((item) => <li className="break-words" key={`global-preview-${item.code}`}>{item.description} <span className="text-amber-300">— {item.owner}</span></li>)}</ul> : <p className="mt-1 text-slate-400">No hay datos comerciales globales pendientes.</p>}</div>
  </section>;
}

function Evaluation({ evaluation, label }: { evaluation: RfpResponseEvaluation | null; label: string }) {
  if (!evaluation) return null;
  const pending = evaluation.blocks_commercial_completeness || evaluation.requires_human_input;
  const qualityFeedback = evaluation.actionable_feedback.filter((item) => !(
    item.startsWith("Required commercial term is not documented:") ||
    item.startsWith("Commercial inputs remain pending:") ||
    item === "Proposal currency is pending" ||
    item === "SLA applicability is pending" ||
    item.startsWith("A required volume-discount")
  ));
  return <div className="mt-3 min-w-0 text-sm">
    <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">{label}</p>
    <p className={`mt-1 ${evaluation.overall_pass ? "text-emerald-300" : "text-amber-200"}`}>{evaluation.overall_pass ? "Aprobada automáticamente" : (EVALUATION_STATUS_LABELS[evaluation.terminal_status ?? ""] ?? "No aprobada")}{pending ? " · requiere información humana" : ""}</p>
    {qualityFeedback.length > 0 && <div className="mt-3"><p className="text-xs font-semibold uppercase text-cyan-300">Recomendaciones de calidad y legibilidad</p><ul className="mt-2 list-disc space-y-1 pl-5 text-slate-300">{qualityFeedback.map((item, index) => <li className="break-words" key={`${evaluation.evaluation_id}-feedback-${index}`}>{item}</li>)}</ul></div>}
  </div>;
}

function GlobalEvaluation({ terms }: { terms: RfpResponseGlobalTerms }) {
  const label = GLOBAL_COMPLIANCE_LABELS[terms.compliance_status] ?? "Evaluación global pendiente";
  const approved = terms.compliance_pass === true;
  const heading = approved ? "Aprobados" : terms.compliance_pass === false ? "No aprobados" : "Pendientes";
  return <>
    <div className="flex min-w-0 flex-wrap justify-between gap-2">
      <h3 className="font-semibold text-white">Términos globales: {heading}</h3>
      <span className={`break-words text-right text-xs ${approved ? "text-emerald-300" : "text-cyan-200"}`}>{label}{terms.iteration_count > 0 ? ` · generación global ${terms.iteration_count}/3` : ""}</span>
    </div>
    {!terms.evaluation_executed && terms.compliance_status === "en_evaluación" && <p className="mt-3 text-sm text-cyan-100">La evaluación global está en curso.</p>}
    {terms.evaluation_executed && terms.evaluation && terms.evaluation.actionable_feedback.length > 0 && <div className="mt-3 text-sm"><p className="text-xs font-semibold uppercase text-cyan-300">Hallazgos de compliance global</p><ul className="mt-2 list-disc space-y-1 pl-5 text-slate-300">{terms.evaluation.actionable_feedback.map((item, index) => <li className="break-words" key={`${terms.evaluation?.evaluation_id}-global-${index}`}>{item}</li>)}</ul></div>}
  </>;
}

function ResponseProgress({ response }: { response: RfpResponseRun }) {
  const heading = response.technical_failure ? "Fallo técnico operativo" : response.outcome === "ready_for_part3" ? "Evaluaciones automáticas aprobadas" : response.outcome === "needs_human_review" ? "Revisión humana necesaria" : "Generación en curso";
  return <section aria-live="polite" className="min-w-0 space-y-4 overflow-hidden rounded-2xl border border-cyan-400/20 bg-slate-900/70 p-6">
    <div><p className="text-xs uppercase tracking-wider text-cyan-300">Resultado de generación de respuesta</p><h2 className="mt-1 text-lg font-semibold text-white">{heading}</h2><p className="mt-1 text-sm text-slate-300">Estado: {RESPONSE_STATUS_LABELS[response.status]}</p><p className="mt-1 text-xs text-slate-400">Modelo de transportista: {CARRIER_MODEL_LABELS[response.carrier_model]}</p></div>
    {response.technical_failure && <p className="rounded-lg border border-rose-400/30 bg-rose-950/30 p-3 text-sm text-rose-100">El worker no pudo completar el flujo. No existe un handoff empresarial consumible.</p>}
    <div className="grid min-w-0 gap-4 lg:grid-cols-2">{response.departments.map((section) => <article key={section.department} className="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-slate-950/60 p-4">
      <div className="flex min-w-0 flex-wrap justify-between gap-2"><h3 className="font-semibold text-white">{DEPARTMENT_LABELS[section.department]}</h3><span className="break-words text-right text-xs text-cyan-200">{SECTION_STATUS_LABELS[section.status] ?? "Estado departamental pendiente"} · generación {section.iteration_count}/3</span></div>
      <p className="mt-1 text-xs text-slate-400">Responsable: {section.responsible}</p>
      <Evaluation evaluation={section.evaluation} label="Resultado de evaluación departamental" />
      {section.pending_commercial_items.length > 0 && <div className="mt-3"><p className="text-xs font-semibold uppercase text-amber-300">Información comercial obligatoria pendiente</p><ul className="mt-2 space-y-2 text-sm text-amber-100">{section.pending_commercial_items.map((item) => <li className="break-words" key={`${section.department}-${item.code}`}>{item.description} <span className="text-amber-300">— {item.owner}</span></li>)}</ul></div>}
      <Preview preview={section.preview} />
    </article>)}</div>
    <article className="min-w-0 overflow-hidden rounded-xl border border-white/10 bg-slate-950/60 p-4"><GlobalEvaluation terms={response.global_terms} />
      <GlobalPreview preview={response.global_terms.preview} /></article>
    {response.operational_failures.length > 0 && <div className="min-w-0 overflow-hidden rounded-xl border border-rose-400/20 bg-rose-950/20 p-4"><h3 className="font-semibold text-rose-100">Incidencias técnicas</h3><ul className="mt-2 space-y-1 text-sm text-rose-200">{response.operational_failures.map((failure, index) => <li className="break-words" key={`${failure.evaluator}-${failure.occurred_at}-${index}`}>El evaluador {failure.evaluator} no pudo completarse tras {failure.infrastructure_attempts} intento(s) de infraestructura.</li>)}</ul></div>}
  </section>;
}

function TechnicalStatus({ detail }: { detail: RfpTicketDetail }) {
  if (!detail.job) return null;
  const failed = detail.job.status === "failed" || detail.job.status === "dead_letter";
  return (
    <section aria-live="polite" className={`rounded-xl border p-4 ${failed ? "border-rose-400/30 bg-rose-950/30 text-rose-100" : "border-white/10 bg-slate-950/60 text-slate-200"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">Estado técnico del job</h3>
        <span className="text-sm">{JOB_LABELS[detail.job.status]}</span>
      </div>
      <p className="mt-2 text-xs text-slate-400">Intento {detail.job.attempt_count} de {detail.job.max_attempts}</p>
      {failed && (
        <p className="mt-3 text-sm">
          {detail.job.last_error_summary ?? "El procesamiento no pudo completarse. Un administrador debe revisar el job."}
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

function FinalDocumentAccess({ document }: { document: RfpFinalDocument }) {
  const markdownUrl = () => URL.createObjectURL(
    new Blob([document.markdown], { type: "text/markdown;charset=utf-8" }),
  );
  const openDocument = () => {
    const url = markdownUrl();
    window.open(url, "_blank", "noopener,noreferrer");
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };
  const downloadDocument = () => {
    const url = markdownUrl();
    const link = window.document.createElement("a");
    link.href = url;
    link.download = document.download_filename;
    window.document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  return <section className="rounded-2xl border border-emerald-400/30 bg-emerald-950/20 p-5" aria-label="Documento final del ticket">
    <h3 className="font-semibold text-emerald-100">Documento final</h3>
    <p className="mt-1 text-sm text-slate-300">{document.title}</p>
    <div className="mt-3 flex flex-wrap gap-3">
      <button type="button" onClick={openDocument} className="rounded-lg border border-emerald-300/30 px-4 py-2 text-sm font-medium text-emerald-100 hover:bg-emerald-900/40">Abrir Markdown</button>
      <button type="button" onClick={downloadDocument} className="rounded-lg bg-emerald-300 px-4 py-2 text-sm font-semibold text-slate-950 hover:bg-emerald-200">Descargar .md</button>
    </div>
    <p className="mt-2 break-all text-xs text-slate-500">Documento {document.final_document_id} · hash {document.markdown_hash}</p>
  </section>;
}

function RfpPageContent() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [file, setFile] = useState<File | null>(null);
  const [inputKey, setInputKey] = useState(0);
  const [tickets, setTickets] = useState<RfpTicketListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RfpTicketDetail | null>(null);
  const [response, setResponse] = useState<RfpResponseRun | null>(null);
  const [finalDocument, setFinalDocument] = useState<RfpFinalDocument | null>(null);
  const [finalDocumentError, setFinalDocumentError] = useState<string | null>(null);
  const [isLoadingFinalDocument, setIsLoadingFinalDocument] = useState(false);
  const [isStartingResponse, setIsStartingResponse] = useState(false);
  const [carrierModel, setCarrierModel] = useState<RfpCarrierModel | "">("");
  const [pollKey, setPollKey] = useState(0);
  const [isUploading, setIsUploading] = useState(false);
  const [isLoadingList, setIsLoadingList] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acceptance, setAcceptance] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    const restored = window.sessionStorage.getItem(SELECTED_TICKET_STORAGE_KEY);
    if (restored) setSelectedId(restored);
  }, [isAdmin]);
  useEffect(() => {
    if (!isAdmin || !selectedId) return;
    window.sessionStorage.setItem(SELECTED_TICKET_STORAGE_KEY, selectedId);
  }, [isAdmin, selectedId]);

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

  const loadResponse = useCallback(async (ticketId: string, showError = true) => {
    try { const value = await getRfpResponse(ticketId); setResponse(value); return value; }
    catch (caught: unknown) {
      if (caught instanceof RfpApiError && caught.statusCode === 404) { setResponse(null); return null; }
      if (showError) setError(caught instanceof Error ? caught.message : "No se pudo cargar la respuesta.");
      return null;
    }
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
      const [value, responseValue] = await Promise.all([loadDetail(selectedId, !cancelled), loadResponse(selectedId, !cancelled)]);
      if (cancelled || (value && isPollingTerminal(value) && (!responseValue || responseIsTerminal(responseValue)))) return;
      timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [isAdmin, loadDetail, loadResponse, pollKey, selectedId]);
  useEffect(() => {
    setFinalDocument(null);
    setFinalDocumentError(null);
    if (!isAdmin || !detail || detail.status !== "terminado" ||
        selectedId !== detail.ticket_id) return;
    let cancelled = false;
    setIsLoadingFinalDocument(true);
    void getRfpFinalDocumentForTicket(detail.ticket_id)
      .then((document) => { if (!cancelled) setFinalDocument(document); })
      .catch((caught: unknown) => {
        if (!cancelled) setFinalDocumentError(
          caught instanceof Error ? caught.message : "No se pudo cargar el documento final.",
        );
      })
      .finally(() => { if (!cancelled) setIsLoadingFinalDocument(false); });
    return () => { cancelled = true; };
  }, [detail, isAdmin, selectedId]);

  const beginResponse = async () => {
    if (!selectedId || !carrierModel) return;
    setIsStartingResponse(true); setError(null); setAcceptance(null);
    try {
      const accepted = await startRfpResponse(selectedId, carrierModel);
      setAcceptance(`Generación de respuesta iniciada (run ${accepted.run_id}).`);
      await Promise.all([loadDetail(selectedId), loadResponse(selectedId)]);
      setPollKey((value) => value + 1);
      await loadList();
    } catch (caught: unknown) { setError(caught instanceof Error ? caught.message : "No se pudo iniciar la respuesta."); }
    finally { setIsStartingResponse(false); }
  };

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

  if (!isAdmin) return <RfpApprovalInbox />;

  return (
    <div className="space-y-8">
      <RfpApprovalInbox />
      <div className="grid min-w-0 max-w-full gap-8 overflow-hidden xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
      <div className="min-w-0 space-y-6 overflow-hidden">
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
            <ul className="mt-4 space-y-2">{tickets.map((ticket) => <li key={ticket.ticket_id}><button type="button" onClick={() => { setError(null); setResponse(null); setCarrierModel(""); setSelectedId(ticket.ticket_id); }} className={`w-full rounded-xl border p-3 text-left transition ${selectedId === ticket.ticket_id ? "border-cyan-400/50 bg-cyan-950/30" : "border-white/10 bg-slate-950/50 hover:border-white/20"}`}>
              <span className="block truncate text-sm font-medium text-white">{ticket.client_name ?? ticket.rfp_id ?? ticket.ticket_id}</span><span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-400"><span>{TICKET_LABELS[ticket.status]}</span>{ticket.job_status && <span>{JOB_LABELS[ticket.job_status]}</span>}<span>{displayDate(ticket.created_at)}</span></span>
            </button></li>)}</ul>
          )}
        </section>
      </div>
      <div className="min-w-0 space-y-5 overflow-hidden">{detail && selectedId === detail.ticket_id ? <><TicketDetail detail={detail} />
        {isLoadingFinalDocument && <p className="rounded-xl border border-white/10 p-4 text-sm text-slate-400">Comprobando documento final…</p>}
        {finalDocumentError && <p role="alert" className="rounded-xl border border-amber-400/30 bg-amber-950/20 p-4 text-sm text-amber-100">{finalDocumentError}</p>}
        {finalDocument && <FinalDocumentAccess document={finalDocument} />}
        {detail.status === "analisis_completo" && !response && <section className="rounded-2xl border border-white/10 bg-slate-900/70 p-5"><label htmlFor="carrier-model" className="block text-sm font-semibold text-white">Modelo de transportista confirmado</label><p className="mt-1 text-xs text-slate-400">Selecciona explícitamente el modelo operativo. Esta confirmación administrativa no procede del documento RFP.</p><select id="carrier-model" value={carrierModel} onChange={(event) => setCarrierModel(event.target.value as RfpCarrierModel | "")} disabled={isStartingResponse} className="mt-4 w-full rounded-xl border border-white/10 bg-slate-950 p-3 text-sm text-slate-200"><option value="">Selecciona una opción…</option><option value="unknown">Sin confirmar</option><option value="trackflow_managed">Gestionado por TrackFlow</option><option value="customer_managed">Gestionado por el cliente</option></select><button type="button" onClick={() => void beginResponse()} disabled={isStartingResponse || !carrierModel} className="mt-4 w-full rounded-xl bg-cyan-400 px-5 py-3 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-60">{isStartingResponse ? "Iniciando…" : "Iniciar generación de respuesta"}</button></section>}
        {response && response.ticket_id === detail.ticket_id && <ResponseProgress response={response} />}</> : <section className="rounded-2xl border border-dashed border-white/15 bg-slate-900/40 p-8 text-center text-sm text-slate-400">Selecciona un ticket para consultar su estado y resultado.</section>}</div>
      </div>
    </div>
  );
}

export default function RfpPage() {
  return <AuthGuard><div className="min-h-screen max-w-full overflow-x-hidden"><BackofficeHeader /><main className="mx-auto min-w-0 max-w-7xl px-4 py-10 sm:px-6"><header className="mb-8"><p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">Commercial intake</p><h1 className="mt-2 text-3xl font-bold text-white">Recepción de RFPs</h1><p className="mt-3 max-w-3xl text-sm text-slate-400">Carga solicitudes de propuesta, consulta el procesamiento asíncrono y revisa los workstreams antes de continuar a la siguiente etapa comercial.</p></header><RfpPageContent /></main></div></AuthGuard>;
}
