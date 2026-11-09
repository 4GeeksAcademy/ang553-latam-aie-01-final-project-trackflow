import type { KnowledgeSource } from "@/types/rag";

export type SupportComponentStatus =
  | "ok"
  | "not_found"
  | "timeout"
  | "unavailable";

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
  sources: KnowledgeSource[];
}

export interface SupportQueryResponse {
  answer: string;
  route: "clarification" | "knowledge" | "ticket" | "inventory" | "mixed";
  partial: boolean;
  needs_clarification: boolean;
  ticket: SupportTicketResult | null;
  inventory: SupportInventoryResult | null;
  knowledge: SupportKnowledgeResult | null;
}
