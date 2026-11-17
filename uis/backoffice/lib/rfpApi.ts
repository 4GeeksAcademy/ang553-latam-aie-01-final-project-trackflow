import { authFetch } from "@/lib/authFetch";
import type {
  RfpDepartmentId,
  RfpDepartmentSection,
  RfpDetectedConflict,
  RfpHandoff,
  RfpJobStatus,
  RfpJobSummary,
  RfpKeyAspect,
  RfpMetadata,
  RfpOpenQuestion,
  RfpServiceRequest,
  RfpSourceReference,
  RfpTicketAccepted,
  RfpTicketDetail,
  RfpTicketListItem,
  RfpTicketListResponse,
  RfpTicketStatus,
} from "@/types/rfp";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

const TICKET_STATUSES = new Set<RfpTicketStatus>([
  "analizando",
  "descartado",
  "analisis_completo",
]);
const JOB_STATUSES = new Set<RfpJobStatus>([
  "queued",
  "running",
  "retry_wait",
  "succeeded",
  "failed",
  "dead_letter",
]);
const DEPARTMENTS = new Set<RfpDepartmentId>([
  "warehouse",
  "lastmile",
  "reverse",
]);

export class RfpApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "RfpApiError";
    this.statusCode = statusCode;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw new Error("invalid string");
  return value;
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error("invalid number");
  }
  return value;
}

function parseTicketStatus(value: unknown): RfpTicketStatus {
  if (typeof value !== "string" || !TICKET_STATUSES.has(value as RfpTicketStatus)) {
    throw new Error("invalid ticket status");
  }
  return value as RfpTicketStatus;
}

function parseJobStatus(value: unknown): RfpJobStatus {
  if (typeof value !== "string" || !JOB_STATUSES.has(value as RfpJobStatus)) {
    throw new Error("invalid job status");
  }
  return value as RfpJobStatus;
}

function parseDepartment(value: unknown): RfpDepartmentId {
  if (typeof value !== "string" || !DEPARTMENTS.has(value as RfpDepartmentId)) {
    throw new Error("invalid department");
  }
  return value as RfpDepartmentId;
}

function parseDepartments(value: unknown): RfpDepartmentId[] {
  if (!Array.isArray(value)) throw new Error("invalid departments");
  const departments = value.map(parseDepartment);
  if (new Set(departments).size !== departments.length) {
    throw new Error("duplicate department");
  }
  return departments;
}

function parseListItem(value: unknown): RfpTicketListItem {
  if (!isRecord(value)) throw new Error("invalid ticket");
  if (
    typeof value.ticket_id !== "string" ||
    typeof value.created_at !== "string" ||
    typeof value.updated_at !== "string"
  ) {
    throw new Error("invalid ticket identity");
  }

  const currency = stringOrNull(value.currency);
  if (currency !== null && currency !== "EUR" && currency !== "USD") {
    throw new Error("invalid currency");
  }

  return {
    ticket_id: value.ticket_id,
    rfp_id: stringOrNull(value.rfp_id),
    status: parseTicketStatus(value.status),
    job_status: value.job_status == null ? null : parseJobStatus(value.job_status),
    client_name: stringOrNull(value.client_name),
    currency,
    departments_needed: parseDepartments(value.departments_needed ?? []),
    created_at: value.created_at,
    updated_at: value.updated_at,
  };
}

function parseService(value: unknown): RfpServiceRequest {
  if (!isRecord(value) || typeof value.requested !== "boolean") {
    throw new Error("invalid service");
  }
  return {
    service_id: parseDepartment(value.service_id),
    requested: value.requested,
    source_ref: value.source_ref == null ? null : parseSourceReference(value.source_ref),
  };
}

function parseMetadata(value: unknown): RfpMetadata | null {
  if (value == null) return null;
  if (!isRecord(value)) throw new Error("invalid metadata");
  const country = stringOrNull(value.client_country_code);
  if (country !== null && country !== "ES" && country !== "US") {
    throw new Error("invalid country");
  }
  const currency = stringOrNull(value.currency);
  if (currency !== null && currency !== "EUR" && currency !== "USD") {
    throw new Error("invalid currency");
  }
  const operationCountries = value.operation_country_codes;
  if (
    operationCountries !== null &&
    operationCountries !== undefined &&
    (!Array.isArray(operationCountries) ||
      operationCountries.some((item) => item !== "ES" && item !== "US"))
  ) {
    throw new Error("invalid operation countries");
  }
  if (!Array.isArray(value.services_requested)) {
    throw new Error("invalid services");
  }

  return {
    client_name: stringOrNull(value.client_name),
    client_country_code: country,
    client_country_original: stringOrNull(value.client_country_original),
    operation_country_codes:
      operationCountries == null
        ? null
        : (operationCountries as Array<"ES" | "US">),
    currency,
    services_requested: value.services_requested.map(parseService),
    monthly_volume_value: numberOrNull(value.monthly_volume_value),
    monthly_volume_unit: stringOrNull(value.monthly_volume_unit),
    deadline: stringOrNull(value.deadline),
    onboarding_target: stringOrNull(value.onboarding_target),
    departments_needed: parseDepartments(value.departments_needed ?? []),
  };
}

function parseQuestion(value: unknown): RfpOpenQuestion {
  if (
    !isRecord(value) ||
    typeof value.question !== "string" ||
    typeof value.reason !== "string"
  ) {
    throw new Error("invalid question");
  }
  return {
    question: value.question,
    reason: value.reason,
    source_ref: value.source_ref == null ? null : parseSourceReference(value.source_ref),
  };
}

function parseSourceReference(value: unknown): RfpSourceReference {
  if (
    !isRecord(value) ||
    !Number.isInteger(value.line_start) ||
    !Number.isInteger(value.line_end) ||
    (value.line_start as number) < 1 ||
    (value.line_end as number) < (value.line_start as number)
  ) {
    throw new Error("invalid source reference");
  }
  return {
    line_start: value.line_start as number,
    line_end: value.line_end as number,
  };
}

function parseAspect(value: unknown): RfpKeyAspect {
  if (
    !isRecord(value) ||
    typeof value.fact !== "string" ||
    typeof value.requested_action !== "string"
  ) {
    throw new Error("invalid key aspect");
  }
  return {
    fact: value.fact,
    source_ref: parseSourceReference(value.source_ref),
    requested_action: value.requested_action,
  };
}

function parseConflict(value: unknown): RfpDetectedConflict {
  if (
    !isRecord(value) ||
    typeof value.description !== "string" ||
    !Array.isArray(value.source_refs)
  ) {
    throw new Error("invalid conflict");
  }
  return {
    description: value.description,
    source_refs: value.source_refs.map(parseSourceReference),
  };
}

function parseConflicts(value: unknown): RfpDetectedConflict[] {
  if (!Array.isArray(value)) throw new Error("invalid conflicts");
  return value.map(parseConflict);
}

function parseSection(value: unknown): RfpDepartmentSection {
  if (
    !isRecord(value) ||
    typeof value.responsible !== "string" ||
    !Array.isArray(value.key_aspects) ||
    !Array.isArray(value.open_questions)
  ) {
    throw new Error("invalid department section");
  }
  return {
    department_id: parseDepartment(value.department_id),
    responsible: value.responsible,
    key_aspects: value.key_aspects.map(parseAspect),
    open_questions: value.open_questions.map(parseQuestion),
    detected_conflicts: parseConflicts(value.detected_conflicts ?? []),
  };
}

function parseHandoff(value: unknown): RfpHandoff | null {
  if (value == null) return null;
  if (
    !isRecord(value) ||
    value.contract_version !== "rfp-intake-v1" ||
    typeof value.summary_for_sales !== "string" ||
    !Array.isArray(value.open_questions)
  ) {
    throw new Error("invalid handoff");
  }
  return {
    contract_version: "rfp-intake-v1",
    summary_for_sales: value.summary_for_sales,
    open_questions: value.open_questions.map(parseQuestion),
    detected_conflicts: parseConflicts(value.detected_conflicts ?? []),
  };
}

function parseJob(value: unknown): RfpJobSummary | null {
  if (value == null) return null;
  if (
    !isRecord(value) ||
    typeof value.attempt_count !== "number" ||
    typeof value.max_attempts !== "number"
  ) {
    throw new Error("invalid job");
  }
  return {
    status: parseJobStatus(value.status),
    attempt_count: value.attempt_count,
    max_attempts: value.max_attempts,
    last_error_code: stringOrNull(value.last_error_code),
    last_error_summary: stringOrNull(value.last_error_summary),
  };
}

function publicErrorMessage(status: number): string {
  if (status === 400 || status === 415 || status === 422) {
    return "El archivo no es un PDF válido para recepción.";
  }
  if (status === 403) return "Tu rol no tiene acceso a la recepción de RFPs.";
  if (status === 404) return "No se encontró el ticket solicitado.";
  if (status === 413) return "El PDF supera el tamaño permitido.";
  if (status >= 500) return "El servicio de RFPs no está disponible temporalmente.";
  return "No se pudo completar la operación de RFPs.";
}

async function request(path: string, pathTemplate: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await authFetch(`${BASE_URL}${path}`, init, { pathTemplate });
  } catch {
    throw new RfpApiError("No se pudo conectar con el servicio de RFPs.");
  }
  if (!response.ok) {
    throw new RfpApiError(publicErrorMessage(response.status), response.status);
  }
  try {
    return await response.json() as unknown;
  } catch {
    throw new RfpApiError("El servicio de RFPs devolvió una respuesta inválida.");
  }
}

export async function uploadRfp(file: File): Promise<RfpTicketAccepted> {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    throw new RfpApiError("Selecciona un archivo PDF.");
  }
  const formData = new FormData();
  formData.append("file", file);
  const value = await request("/rfps", "/rfps", { method: "POST", body: formData });
  if (!isRecord(value) || typeof value.ticket_id !== "string" || value.status !== "analizando") {
    throw new RfpApiError("El servicio de RFPs devolvió una aceptación inválida.");
  }
  return { ticket_id: value.ticket_id, status: "analizando" };
}

export async function listRfpTickets(): Promise<RfpTicketListResponse> {
  const value = await request("/rfps", "/rfps");
  if (!isRecord(value) || !Array.isArray(value.items) || typeof value.total !== "number") {
    throw new RfpApiError("El servicio de RFPs devolvió una lista inválida.");
  }
  try {
    return { items: value.items.map(parseListItem), total: value.total };
  } catch {
    throw new RfpApiError("El servicio de RFPs devolvió tickets con formato inválido.");
  }
}

export async function getRfpTicket(ticketId: string): Promise<RfpTicketDetail> {
  const value = await request(
    `/rfps/${encodeURIComponent(ticketId)}`,
    "/rfps/{ticket_id}",
  );
  try {
    const item = parseListItem(value);
    if (!isRecord(value) || !Array.isArray(value.department_sections)) {
      throw new Error("invalid detail");
    }
    return {
      ...item,
      discard_reason_code: stringOrNull(value.discard_reason_code),
      discard_summary: stringOrNull(value.discard_summary),
      metadata: parseMetadata(value.metadata),
      department_sections: value.department_sections.map(parseSection),
      handoff: parseHandoff(value.handoff),
      job: parseJob(value.job),
    };
  } catch {
    throw new RfpApiError("El servicio de RFPs devolvió un detalle inválido.");
  }
}
