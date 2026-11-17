export type RfpTicketStatus = "analizando" | "descartado" | "analisis_completo";

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
