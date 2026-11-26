import type { KnowledgeSource } from "@/types/rag";

export type SupportComponentStatus =
  | "ok"
  | "not_found"
  | "timeout"
  | "unavailable";

export type KnowledgeCoverage = "complete" | "partial" | "insufficient";

export interface KnowledgeProjection {
  coverage?: KnowledgeCoverage;
  sources: KnowledgeSource[];
}

export interface SupportTicketResult {
  status: SupportComponentStatus;
  incident_id: string | null;
  incident_status: string | null;
  category: string | null;
  version: number | null;
  updated_at: string | null;
}

export interface SupportInventoryResult {
  status: SupportComponentStatus;
  product_id: number | null;
  name: string | null;
  sku: string | null;
  warehouse: string | null;
  current_stock: number | null;
}

export interface SupportKnowledgeResult {
  status: SupportComponentStatus;
  answer: string | null;
  insufficient_context: boolean;
  coverage?: KnowledgeCoverage | null;
  sources: KnowledgeSource[];
}

export type SupportMemoryScope = "client" | "global";
export type SupportMemoryCategory =
  | "carrier_coverage"
  | "recurring_incident"
  | "b2b_report_preference";

export interface SupportMemoryProposal {
  id: string;
  conversation_id: string;
  scope: SupportMemoryScope;
  client_id: string | null;
  category: SupportMemoryCategory;
  content: Record<string, unknown>;
  verification_status: "user_reported";
  expires_at: string;
}

export interface SupportMemoryEvaluation {
  status: string;
  candidate_category: SupportMemoryCategory | null;
  evaluator_called: boolean;
}

export interface SupportConversationalConsent {
  action: SupportMemoryAction;
  disposition:
    | "approved"
    | "rejected"
    | "replacement_pending"
    | "discarded"
    | "expired";
  continue_support_question: boolean;
}

export interface SupportQueryResponse {
  answer: string;
  route: "clarification" | "knowledge" | "ticket" | "inventory" | "mixed";
  partial: boolean;
  needs_clarification: boolean;
  ticket: SupportTicketResult | null;
  inventory: SupportInventoryResult | null;
  knowledge: SupportKnowledgeResult | null;
  conversation_id?: string | null;
  memory_proposal?: SupportMemoryProposal | null;
  memory_evaluation?: SupportMemoryEvaluation | null;
  memory_read_status?: string | null;
  memory_consent?: SupportConversationalConsent | null;
}

export interface SupportQueryOptions {
  conversationId?: string | null;
  clientId?: string | null;
  memoryScope?: SupportMemoryScope | null;
}

export type SupportMemoryAction =
  | "approve"
  | "reject"
  | "edit"
  | "ambiguous"
  | "topic_change";

export interface SupportMemoryDecisionRequest {
  conversation_id: string;
  proposal_id: string;
  idempotency_key: string;
  action: SupportMemoryAction;
  edited_content?: Record<string, unknown>;
  user_text?: string;
}

export interface SupportMemoryDecisionResponse {
  conversation_id: string;
  disposition: string;
  proposal: SupportMemoryProposal | null;
  memory_active: boolean;
  continue_support_question: boolean;
}

export interface SupportPendingMemoryResponse {
  conversation_id: string;
  proposal: SupportMemoryProposal | null;
}
