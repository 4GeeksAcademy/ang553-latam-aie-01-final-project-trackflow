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