export type RfpTicketStatus =
  | "analizando"
  | "descartado"
  | "analisis_completo"
  | "generando_borrador"
  | "en_evaluación"
  | "needs_human_review"
  | "esperando_aprobación"
  | "terminado";

export type RfpJobStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "succeeded"
  | "failed"
  | "dead_letter";

export interface RfpTicketAccepted {
  ticket_id: string;
  status: "analizando";
}

export interface RfpTicketListItem {
  ticket_id: string;
  rfp_id: string | null;
  status: RfpTicketStatus;
  job_status: RfpJobStatus | null;
  client_name: string | null;
  currency: "EUR" | "USD" | null;
  departments_needed: RfpDepartmentId[];
  created_at: string;
  updated_at: string;
}

export interface RfpServiceRequest {
  service_id: "warehouse" | "lastmile" | "reverse";
  requested: boolean;
  source_ref: RfpSourceReference | null;
}

export interface RfpMetadata {
  client_name: string | null;
  client_country_code: "ES" | "US" | null;
  client_country_original: string | null;
  operation_country_codes: Array<"ES" | "US"> | null;
  currency: "EUR" | "USD" | null;
  services_requested: RfpServiceRequest[];
  monthly_volume_value: number | null;
  monthly_volume_unit: string | null;
  deadline: string | null;
  onboarding_target: string | null;
  departments_needed: RfpDepartmentId[];
}

export type RfpDepartmentId = "warehouse" | "lastmile" | "reverse";

export interface RfpKeyAspect {
  fact: string;
  source_ref: RfpSourceReference;
  requested_action: string;
}

export interface RfpSourceReference {
  line_start: number;
  line_end: number;
}

export interface RfpOpenQuestion {
  question: string;
  reason: string;
  source_ref?: RfpSourceReference | null;
}

export interface RfpDetectedConflict {
  description: string;
  source_refs: RfpSourceReference[];
}

export interface RfpDepartmentSection {
  department_id: RfpDepartmentId;
  responsible: string;
  key_aspects: RfpKeyAspect[];
  open_questions: RfpOpenQuestion[];
  detected_conflicts: RfpDetectedConflict[];
}

export interface RfpHandoff {
  contract_version: "rfp-intake-v1";
  summary_for_sales: string;
  open_questions: RfpOpenQuestion[];
  detected_conflicts: RfpDetectedConflict[];
}

export interface RfpJobSummary {
  status: RfpJobStatus;
  attempt_count: number;
  max_attempts: number;
  last_error_code: string | null;
  last_error_summary: string | null;
}

export interface RfpTicketDetail extends RfpTicketListItem {
  discard_reason_code: string | null;
  discard_summary: string | null;
  metadata: RfpMetadata | null;
  department_sections: RfpDepartmentSection[];
  handoff: RfpHandoff | null;
  job: RfpJobSummary | null;
}

export interface RfpTicketListResponse {
  items: RfpTicketListItem[];
  total: number;
}

export type RfpResponseRunStatus =
  | "pending"
  | "generando_borrador"
  | "en_evaluación"
  | "ready_for_part3"
  | "needs_human_review"
  | "technical_failure";

export type RfpCarrierModel = "unknown" | "trackflow_managed" | "customer_managed";

export type RfpPreviewStatus = "passed" | "blocked" | "indeterminate" | "technical_error";

export type RfpGlobalComplianceStatus =
  | "not_started"
  | "en_evaluación"
  | "passed"
  | "failed"
  | "technical_failure";

export interface RfpResponseAccepted {
  ticket_id: string;
  run_id: string;
  job_id: string;
  status: "queued";
}

export interface RfpResponsePreview {
  gate_status: RfpPreviewStatus;
  content: string | null;
  reason_codes: string[];
}

export interface RfpResponseEvaluation {
  evaluation_id: string;
  overall_pass: boolean;
  terminal_status: string | null;
  actionable_feedback: string[];
  missing_aspect_ids: string[];
  contradicted_aspect_ids: string[];
  blocks_commercial_completeness: boolean;
  requires_human_input: boolean;
  blocking_rule_ids: string[];
}

export interface RfpPendingCommercialItem {
  code: string;
  description: string;
  owner: string;
}

export interface RfpResponseDepartment {
  department: RfpDepartmentId;
  responsible: string;
  status: string;
  iteration_count: number;
  preview: RfpResponsePreview | null;
  evaluation: RfpResponseEvaluation | null;
  pending_commercial_items: RfpPendingCommercialItem[];
}

export interface RfpResponseGlobalTerms {
  status: RfpGlobalComplianceStatus;
  compliance_status: RfpGlobalComplianceStatus;
  compliance_pass: boolean | null;
  evaluation_executed: boolean;
  iteration_count: number;
  preview: RfpResponsePreview | null;
  evaluation: RfpResponseEvaluation | null;
  pending_commercial_items: RfpPendingCommercialItem[];
}

export interface RfpOperationalFailure {
  evaluator: string;
  infrastructure_attempts: number;
  error_code: string;
  terminal: boolean;
  occurred_at: string;
}

export interface RfpResponseRun {
  ticket_id: string;
  run_id: string;
  carrier_model: RfpCarrierModel;
  status: RfpResponseRunStatus;
  outcome: "ready_for_part3" | "needs_human_review" | null;
  technical_failure: boolean;
  job: (RfpJobSummary & { job_id: string }) | null;
  departments: RfpResponseDepartment[];
  global_terms: RfpResponseGlobalTerms;
  operational_failures: RfpOperationalFailure[];
  updated_at: string;
}

export type RfpApprovalAction = "approve" | "reject" | "request_changes";

export interface RfpApprovalInboxItem {
  approval_run_id: string;
  ticket_id: string;
  revision_id: string;
  department: RfpDepartmentId;
  review_revision_no: number;
  status: "pending_approval";
  content_hash: string;
  validation_bundle_id: string;
  created_at: string;
}

export interface RfpDepartmentReview {
  approval_run_id: string;
  ticket_id: string;
  revision_id: string;
  department: RfpDepartmentId;
  run_status: string;
  revision_status: string;
  is_current: boolean;
  state_version: number;
  content_hash: string;
  validation_bundle_id: string;
  validated_at: string;
  safe_preview_artifact_id: string;
  safe_preview_payload_hash: string;
  safe_preview: string;
  validation: Record<string, unknown>;
  pending_commercial_items: unknown[];
  evidence: unknown[];
  provenance: unknown;
  allowed_actions: RfpApprovalAction[];
}

export interface RfpApprovalDecisionResult {
  thread_id: string;
  status: string | null;
  interrupted: boolean;
  decision: Record<string, unknown> | null;
  application: Record<string, unknown> | null;
  coordination: Record<string, unknown> | null;
}

export interface RfpFinalDocumentSection {
  department: RfpDepartmentId;
  title: string;
  markdown: string;
}

export interface RfpFinalDocumentLocator {
  ticket_id: string;
  approval_run_id: string;
  final_document_id: string;
}

export interface RfpFinalDocument {
  contract_version: "rfp-public-final-document-v1";
  final_document_id: string;
  title: string;
  executive_summary_markdown: string;
  sections: RfpFinalDocumentSection[];
  currency: {
    value: "EUR" | "USD" | null;
    status: "documented" | "pending";
  };
  sla: {
    applicability: "applicable" | "not_applicable" | "pending";
    percentage: number | null;
    measurement_period: string | null;
    scope: string[];
    exclusions: string[];
  };
  volume_discounts: Array<{
    volume_from: number | null;
    volume_to: number | null;
    discount_percentage: number | null;
    condition: string;
    status: "documented" | "pending";
  }>;
  markdown: string;
  markdown_hash: string;
  download_filename: string;
}

export interface RfpTicketCreated {
  ticket_id: string;
  rfp_id: string | null;
  client_name: string | null;
  client_country: "ES" | "US" | null;
  services_requested: RfpDepartmentId[];
  status: "analyzing";
  created_at: string;
}

export interface RfpCommercialTicket extends Omit<RfpTicketCreated, "status"> {
  status: RfpTicketStatus;
  job_status: RfpJobStatus | null;
  updated_at: string;
}

export interface RfpCommercialSnapshot {
  items: RfpCommercialTicket[];
  total: number;
  cursor: string;
}
