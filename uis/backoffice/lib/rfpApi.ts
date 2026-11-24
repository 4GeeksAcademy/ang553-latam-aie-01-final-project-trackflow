import { authFetch } from "@/lib/authFetch";
import type {
  RfpApprovalAction,
  RfpApprovalDecisionResult,
  RfpApprovalInboxItem,
  RfpDepartmentId,
  RfpDepartmentReview,
  RfpFinalDocument,
  RfpFinalDocumentLocator,
  RfpCarrierModel,
  RfpDepartmentSection,
  RfpDetectedConflict,
  RfpHandoff,
  RfpJobStatus,
  RfpJobSummary,
  RfpGlobalComplianceStatus,
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
  RfpOperationalFailure,
  RfpPendingCommercialItem,
  RfpPreviewStatus,
  RfpResponseAccepted,
  RfpResponseDepartment,
  RfpResponseEvaluation,
  RfpResponseGlobalTerms,
  RfpResponsePreview,
  RfpResponseRun,
  RfpResponseRunStatus,
} from "@/types/rfp";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

const TICKET_STATUSES = new Set<RfpTicketStatus>([
  "analizando",
  "descartado",
  "analisis_completo",
  "generando_borrador",
  "en_evaluación",
  "needs_human_review",
  "esperando_aprobación",
  "terminado",
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
const RESPONSE_STATUSES = new Set<RfpResponseRunStatus>([
  "pending", "generando_borrador", "en_evaluación", "ready_for_part3",
  "needs_human_review", "technical_failure",
]);
const PREVIEW_STATUSES = new Set<RfpPreviewStatus>([
  "passed", "blocked", "indeterminate", "technical_error",
]);
const CARRIER_MODELS = new Set<RfpCarrierModel>([
  "unknown", "trackflow_managed", "customer_managed",
]);
const GLOBAL_COMPLIANCE_STATUSES = new Set<RfpGlobalComplianceStatus>([
  "not_started", "en_evaluación", "passed", "failed", "technical_failure",
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

function parseStrings(value: unknown): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error("invalid string list");
  }
  return value as string[];
}

function parsePending(value: unknown): RfpPendingCommercialItem[] {
  if (!Array.isArray(value)) throw new Error("invalid pending items");
  return value.map((item) => {
    if (!isRecord(item) || typeof item.code !== "string" ||
        typeof item.description !== "string" || typeof item.owner !== "string") {
      throw new Error("invalid pending item");
    }
    return { code: item.code, description: item.description, owner: item.owner };
  });
}

function parsePreview(value: unknown): RfpResponsePreview | null {
  if (value == null) return null;
  if (!isRecord(value) || typeof value.gate_status !== "string" ||
      !PREVIEW_STATUSES.has(value.gate_status as RfpPreviewStatus)) {
    throw new Error("invalid preview");
  }
  const gateStatus = value.gate_status as RfpPreviewStatus;
  const content = stringOrNull(value.content);
  const reasonCodes = parseStrings(value.reason_codes);
  if ((gateStatus === "passed" && (!content || reasonCodes.length > 0)) ||
      (gateStatus !== "passed" && (content !== null || reasonCodes.length === 0))) {
    throw new Error("unsafe preview projection");
  }
  return { gate_status: gateStatus, content, reason_codes: reasonCodes };
}

function parseEvaluation(value: unknown): RfpResponseEvaluation | null {
  if (value == null) return null;
  if (!isRecord(value) || typeof value.evaluation_id !== "string" ||
      typeof value.overall_pass !== "boolean" ||
      typeof value.blocks_commercial_completeness !== "boolean" ||
      typeof value.requires_human_input !== "boolean") {
    throw new Error("invalid evaluation");
  }
  return {
    evaluation_id: value.evaluation_id,
    overall_pass: value.overall_pass,
    terminal_status: stringOrNull(value.terminal_status),
    actionable_feedback: parseStrings(value.actionable_feedback),
    missing_aspect_ids: parseStrings(value.missing_aspect_ids),
    contradicted_aspect_ids: parseStrings(value.contradicted_aspect_ids),
    blocks_commercial_completeness: value.blocks_commercial_completeness,
    requires_human_input: value.requires_human_input,
    blocking_rule_ids: parseStrings(value.blocking_rule_ids),
  };
}

function parseDepartmentResponse(value: unknown): RfpResponseDepartment {
  if (!isRecord(value) || typeof value.responsible !== "string" ||
      typeof value.status !== "string" || !Number.isInteger(value.iteration_count)) {
    throw new Error("invalid response department");
  }
  return {
    department: parseDepartment(value.department), responsible: value.responsible,
    status: value.status, iteration_count: value.iteration_count as number,
    preview: parsePreview(value.preview), evaluation: parseEvaluation(value.evaluation),
    pending_commercial_items: parsePending(value.pending_commercial_items),
  };
}

function parseGlobalResponse(value: unknown): RfpResponseGlobalTerms {
  if (!isRecord(value) || typeof value.status !== "string" ||
      !Number.isInteger(value.iteration_count)) throw new Error("invalid global terms");
  const complianceStatus = typeof value.compliance_status === "string"
    ? value.compliance_status : value.status;
  const compliancePass = value.compliance_pass == null
    ? null : typeof value.compliance_pass === "boolean" ? value.compliance_pass : undefined;
  if (!GLOBAL_COMPLIANCE_STATUSES.has(value.status as RfpGlobalComplianceStatus) ||
      !GLOBAL_COMPLIANCE_STATUSES.has(complianceStatus as RfpGlobalComplianceStatus) ||
      value.status !== complianceStatus || compliancePass === undefined ||
      (value.evaluation_executed != null && typeof value.evaluation_executed !== "boolean")) {
    throw new Error("invalid global compliance projection");
  }
  const evaluationExecuted = typeof value.evaluation_executed === "boolean"
    ? value.evaluation_executed : value.evaluation != null;
  if (evaluationExecuted !== (value.evaluation != null) ||
      (compliancePass === null && (evaluationExecuted || ["passed", "failed"].includes(complianceStatus))) ||
      (compliancePass !== null && (!evaluationExecuted || complianceStatus !== (compliancePass ? "passed" : "failed")))) {
    throw new Error("incoherent global compliance projection");
  }
  return {
    status: value.status as RfpGlobalComplianceStatus,
    compliance_status: complianceStatus as RfpGlobalComplianceStatus,
    compliance_pass: compliancePass,
    evaluation_executed: evaluationExecuted,
    iteration_count: value.iteration_count as number,
    preview: parsePreview(value.preview), evaluation: parseEvaluation(value.evaluation),
    pending_commercial_items: parsePending(value.pending_commercial_items),
  };
}

function parseOperationalFailure(value: unknown): RfpOperationalFailure {
  if (!isRecord(value) || typeof value.evaluator !== "string" ||
      !Number.isInteger(value.infrastructure_attempts) || typeof value.error_code !== "string" ||
      typeof value.terminal !== "boolean" || typeof value.occurred_at !== "string") {
    throw new Error("invalid operational failure");
  }
  return { evaluator: value.evaluator, infrastructure_attempts: value.infrastructure_attempts as number,
    error_code: value.error_code, terminal: value.terminal, occurred_at: value.occurred_at };
}

function parseResponseRun(value: unknown): RfpResponseRun {
  if (!isRecord(value) || typeof value.ticket_id !== "string" || typeof value.run_id !== "string" ||
      typeof value.status !== "string" || !RESPONSE_STATUSES.has(value.status as RfpResponseRunStatus) ||
      typeof value.carrier_model !== "string" ||
      !CARRIER_MODELS.has(value.carrier_model as RfpCarrierModel) ||
      typeof value.technical_failure !== "boolean" || !Array.isArray(value.departments) ||
      !Array.isArray(value.operational_failures) || typeof value.updated_at !== "string") {
    throw new Error("invalid response run");
  }
  const outcome = stringOrNull(value.outcome);
  if (outcome !== null && outcome !== "ready_for_part3" && outcome !== "needs_human_review") {
    throw new Error("invalid response outcome");
  }
  const job = parseJob(value.job);
  if (value.job != null && (!isRecord(value.job) || typeof value.job.job_id !== "string")) {
    throw new Error("invalid response job");
  }
  return {
    ticket_id: value.ticket_id, run_id: value.run_id,
    carrier_model: value.carrier_model as RfpCarrierModel,
    status: value.status as RfpResponseRunStatus, outcome,
    technical_failure: value.technical_failure,
    job: job && isRecord(value.job) ? { ...job, job_id: value.job.job_id as string } : null,
    departments: value.departments.map(parseDepartmentResponse),
    global_terms: parseGlobalResponse(value.global_terms),
    operational_failures: value.operational_failures.map(parseOperationalFailure),
    updated_at: value.updated_at,
  };
}

function publicErrorMessage(status: number): string {
  if (status === 400 || status === 415 || status === 422) {
    return "El archivo no es un PDF válido para recepción.";
  }
  if (status === 403) return "Tu rol no tiene acceso a la recepción de RFPs.";
  if (status === 404) return "No se encontró el ticket solicitado.";
  if (status === 409) return "El ticket todavía no es elegible para generar una respuesta.";
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

export async function startRfpResponse(
  ticketId: string,
  carrierModel: RfpCarrierModel,
): Promise<RfpResponseAccepted> {
  const value = await request(`/rfps/${encodeURIComponent(ticketId)}/response`,
    "/rfps/{ticket_id}/response", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ carrier_model: carrierModel }),
    });
  if (!isRecord(value) || typeof value.ticket_id !== "string" ||
      typeof value.run_id !== "string" || typeof value.job_id !== "string" ||
      value.status !== "queued") {
    throw new RfpApiError("El servicio devolvió una aceptación de respuesta inválida.");
  }
  return { ticket_id: value.ticket_id, run_id: value.run_id, job_id: value.job_id, status: "queued" };
}

export async function getRfpResponse(ticketId: string, runId?: string): Promise<RfpResponseRun> {
  const suffix = runId ? `/response/${encodeURIComponent(runId)}` : "/response";
  const template = runId ? "/rfps/{ticket_id}/response/{run_id}" : "/rfps/{ticket_id}/response";
  const value = await request(`/rfps/${encodeURIComponent(ticketId)}${suffix}`, template);
  try { return parseResponseRun(value); }
  catch { throw new RfpApiError("El servicio devolvió un estado de respuesta inválido."); }
}

function parseApprovalInboxItem(value: unknown): RfpApprovalInboxItem {
  if (!isRecord(value) || typeof value.approval_run_id !== "string" ||
      typeof value.ticket_id !== "string" || typeof value.revision_id !== "string" ||
      typeof value.review_revision_no !== "number" ||
      value.status !== "pending_approval" || typeof value.content_hash !== "string" ||
      typeof value.validation_bundle_id !== "string" || typeof value.created_at !== "string") {
    throw new Error("invalid approval inbox item");
  }
  return {
    approval_run_id: value.approval_run_id,
    ticket_id: value.ticket_id,
    revision_id: value.revision_id,
    department: parseDepartment(value.department),
    review_revision_no: value.review_revision_no,
    status: "pending_approval",
    content_hash: value.content_hash,
    validation_bundle_id: value.validation_bundle_id,
    created_at: value.created_at,
  };
}

export async function listRfpApprovalInbox(): Promise<RfpApprovalInboxItem[]> {
  const value = await request("/rfps/approval/inbox", "/rfps/approval/inbox");
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new RfpApiError("El servicio devolvió una bandeja de aprobación inválida.");
  }
  try { return value.items.map(parseApprovalInboxItem); }
  catch { throw new RfpApiError("La bandeja contiene revisiones inválidas."); }
}

export async function getRfpDepartmentReview(
  item: RfpApprovalInboxItem,
): Promise<RfpDepartmentReview> {
  const prefix = `/rfps/${encodeURIComponent(item.ticket_id)}/approval/${encodeURIComponent(item.approval_run_id)}`;
  const suffix = `/departments/${encodeURIComponent(item.department)}/revisions/${encodeURIComponent(item.revision_id)}/review`;
  const value = await request(
    `${prefix}${suffix}`,
    "/rfps/{ticket_id}/approval/{approval_run_id}/departments/{department}/revisions/{revision_id}/review",
  );
  if (!isRecord(value) || typeof value.approval_run_id !== "string" ||
      typeof value.ticket_id !== "string" || typeof value.revision_id !== "string" ||
      typeof value.run_status !== "string" || typeof value.revision_status !== "string" ||
      typeof value.is_current !== "boolean" || typeof value.state_version !== "number" ||
      typeof value.content_hash !== "string" || typeof value.validation_bundle_id !== "string" ||
      typeof value.validated_at !== "string" ||
      typeof value.safe_preview_artifact_id !== "string" ||
      typeof value.safe_preview_payload_hash !== "string" ||
      typeof value.safe_preview !== "string" || !isRecord(value.validation) ||
      !Array.isArray(value.pending_commercial_items) || !Array.isArray(value.evidence) ||
      !Array.isArray(value.allowed_actions)) {
    throw new RfpApiError("El servicio devolvió una revisión inválida.");
  }
  const allowed = value.allowed_actions;
  if (allowed.some((action) => !["approve", "reject", "request_changes"].includes(String(action)))) {
    throw new RfpApiError("La revisión contiene acciones no permitidas.");
  }
  return {
    approval_run_id: value.approval_run_id,
    ticket_id: value.ticket_id,
    revision_id: value.revision_id,
    department: parseDepartment(value.department),
    run_status: value.run_status,
    revision_status: value.revision_status,
    is_current: value.is_current,
    state_version: value.state_version,
    content_hash: value.content_hash,
    validation_bundle_id: value.validation_bundle_id,
    validated_at: value.validated_at,
    safe_preview_artifact_id: value.safe_preview_artifact_id,
    safe_preview_payload_hash: value.safe_preview_payload_hash,
    safe_preview: value.safe_preview,
    validation: value.validation,
    pending_commercial_items: value.pending_commercial_items,
    evidence: value.evidence,
    provenance: value.provenance,
    allowed_actions: allowed as RfpApprovalAction[],
  };
}

export async function submitRfpApprovalDecision(
  item: RfpApprovalInboxItem,
  action: RfpApprovalAction,
  reason: string | null,
  acknowledgePendingCommercialItems: boolean,
): Promise<RfpApprovalDecisionResult> {
  const prefix = `/rfps/${encodeURIComponent(item.ticket_id)}/approval/${encodeURIComponent(item.approval_run_id)}`;
  const suffix = `/departments/${encodeURIComponent(item.department)}/revisions/${encodeURIComponent(item.revision_id)}/decision`;
  const value = await request(
    `${prefix}${suffix}`,
    "/rfps/{ticket_id}/approval/{approval_run_id}/departments/{department}/revisions/{revision_id}/decision",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action,
        reason,
        acknowledge_pending_commercial_items: acknowledgePendingCommercialItems,
      }),
    },
  );
  if (!isRecord(value) || typeof value.thread_id !== "string" ||
      (value.status !== null && typeof value.status !== "string") ||
      typeof value.interrupted !== "boolean") {
    throw new RfpApiError("El servicio devolvió un resultado de aprobación inválido.");
  }
  return {
    thread_id: value.thread_id,
    status: value.status as string | null,
    interrupted: value.interrupted,
    decision: isRecord(value.decision) ? value.decision : null,
    application: isRecord(value.application) ? value.application : null,
    coordination: isRecord(value.coordination) ? value.coordination : null,
  };
}

export async function getRfpFinalDocument(
  ticketId: string,
  approvalRunId: string,
): Promise<RfpFinalDocument> {
  let value: unknown;
  try {
    value = await request(
      `/rfps/${encodeURIComponent(ticketId)}/approval/${encodeURIComponent(approvalRunId)}/document`,
      "/rfps/{ticket_id}/approval/{approval_run_id}/document",
    );
  } catch (caught: unknown) {
    if (caught instanceof RfpApiError && caught.statusCode === 403) {
      throw new RfpApiError(
        "Tu identidad ya no tiene permiso para consultar el documento final.",
        403,
      );
    }
    if (caught instanceof RfpApiError && caught.statusCode === 404) {
      throw new RfpApiError("El documento final todavía no está disponible.", 404);
    }
    throw caught;
  }

  try {
    if (!isRecord(value) ||
        value.contract_version !== "rfp-public-final-document-v1" ||
        typeof value.final_document_id !== "string" ||
        typeof value.title !== "string" ||
        typeof value.executive_summary_markdown !== "string" ||
        !Array.isArray(value.sections) || !isRecord(value.currency) ||
        !isRecord(value.sla) || !Array.isArray(value.volume_discounts) ||
        typeof value.markdown !== "string" ||
        typeof value.markdown_hash !== "string" ||
        typeof value.download_filename !== "string" ||
        !/^[A-Za-z0-9._-]+\.md$/.test(value.download_filename)) {
      throw new Error("invalid final document");
    }
    const sections = value.sections.map((section) => {
      if (!isRecord(section) || typeof section.title !== "string" ||
          typeof section.markdown !== "string") {
        throw new Error("invalid final document section");
      }
      return {
        department: parseDepartment(section.department),
        title: section.title,
        markdown: section.markdown,
      };
    });
    const currencyValue = stringOrNull(value.currency.value);
    if ((currencyValue !== null && currencyValue !== "EUR" && currencyValue !== "USD") ||
        (value.currency.status !== "documented" && value.currency.status !== "pending")) {
      throw new Error("invalid final currency");
    }
    if (!Array.isArray(value.sla.scope) || !Array.isArray(value.sla.exclusions)) {
      throw new Error("invalid final SLA");
    }
    return {
      ...(value as unknown as RfpFinalDocument),
      sections,
      currency: {
        value: currencyValue as "EUR" | "USD" | null,
        status: value.currency.status,
      },
    };
  } catch {
    throw new RfpApiError("El servicio devolvió un documento final inválido.");
  }
}

export async function getRfpFinalDocumentForTicket(
  ticketId: string,
): Promise<RfpFinalDocument> {
  let value: unknown;
  try {
    value = await request(
      `/rfps/${encodeURIComponent(ticketId)}/approval/document-locator`,
      "/rfps/{ticket_id}/approval/document-locator",
    );
  } catch (caught: unknown) {
    if (caught instanceof RfpApiError && caught.statusCode === 403) {
      throw new RfpApiError(
        "Tu identidad no tiene permiso para consultar el documento final.",
        403,
      );
    }
    if (caught instanceof RfpApiError && caught.statusCode === 404) {
      throw new RfpApiError("El ticket todavía no tiene documento final.", 404);
    }
    throw caught;
  }
  if (!isRecord(value) || value.ticket_id !== ticketId ||
      typeof value.approval_run_id !== "string" ||
      typeof value.final_document_id !== "string") {
    throw new RfpApiError("El servicio devolvió un locator de documento inválido.");
  }
  const locator: RfpFinalDocumentLocator = {
    ticket_id: value.ticket_id,
    approval_run_id: value.approval_run_id,
    final_document_id: value.final_document_id,
  };
  const document = await getRfpFinalDocument(
    locator.ticket_id,
    locator.approval_run_id,
  );
  if (document.final_document_id !== locator.final_document_id) {
    throw new RfpApiError("El documento final no coincide con el locator autenticado.");
  }
  return document;
}
