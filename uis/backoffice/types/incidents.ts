/**
 * Types for the TrackFlow Incident Analysis API response.
 *
 * Matches the structure returned by
 * ``POST /api/incidents/analyze`` on the FastAPI backend.
 */

export interface IncidentAnalysisResult {
  total_records: number;
  valid_records: number;
  invalid_records: number;
  invalid_breakdown: Record<string, number>;
  category_breakdown: Record<string, number>;
  status_breakdown: Record<string, number>;
  country_breakdown: Record<string, number>;
  closed_scored: number;
  /** Keys arrive as strings from JSON: ``"1"``, ``"2"`` … ``"5"`` */
  score_distribution: Record<string, number>;
  average_satisfaction: number;
}

export interface OperationalClient {
  client_id: string;
  display_name: string;
  is_active: boolean;
}

export interface IncidentHistoryItem {
  version: number;
  event_type: "CREATED" | "STATUS_CHANGED";
  previous_status: string | null;
  new_status: string;
  reason: string | null;
  occurred_at: string;
}

export interface OperationalIncident {
  incident_id: string;
  incident_date: string;
  country: string;
  customer_type: string;
  tracking_number: string;
  carrier: string;
  category: string;
  description: string;
  status: "OPEN" | "CLOSED" | "DISCARDED";
  customer_email: string | null;
  source_channel: "email" | "whatsapp" | "phone" | "backoffice";
  created_at: string;
  updated_at: string;
  version: number;
  history: IncidentHistoryItem[];
}

export interface OperationalIncidentCreate {
  client_id: string;
  incident_date: string;
  country: string;
  customer_type: string;
  tracking_number: string;
  carrier: string;
  category: string;
  description: string;
  customer_email?: string;
  source_channel: "backoffice";
}
