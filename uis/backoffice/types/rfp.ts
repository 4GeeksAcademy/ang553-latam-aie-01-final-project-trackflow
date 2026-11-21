export type RfpTicketStatus =
  | "analizando"
  | "descartado"
  | "analisis_completo"
  | "generando_borrador"
  | "en_evaluación"
  | "needs_human_review";

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
