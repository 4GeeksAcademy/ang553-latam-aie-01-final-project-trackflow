import type { RfpDepartmentId, RfpJobStatus, RfpTicketStatus } from "@/types/rfp";

export const DEPARTMENT_LABELS: Record<RfpDepartmentId, string> = {
  warehouse: "Warehouse Operations",
  lastmile: "Last Mile and Carrier Management",
  reverse: "Reverse Logistics",
};

export const TICKET_LABELS: Record<RfpTicketStatus, string> = {
  analizando: "Analizando",
  descartado: "Descartado",
  analisis_completo: "Análisis completo",
  generando_borrador: "Generando borrador",
  en_evaluación: "En evaluación",
  needs_human_review: "Revisión humana necesaria",
  esperando_aprobación: "Esperando aprobación",
  terminado: "Terminado",
};

export const JOB_LABELS: Record<RfpJobStatus, string> = {
  queued: "En cola",
  running: "En ejecución",
  retry_wait: "Esperando reintento",
  succeeded: "Finalizado",
  failed: "Fallo técnico",
  dead_letter: "Requiere intervención",
};

export function displayRfpDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("es-ES");
}
