/**
 * Minimal API client for TrackFlow Incident Analysis.
 *
 * Uses ``NEXT_PUBLIC_API_URL`` when provided; otherwise same-origin routes.
 * No external dependencies — plain ``fetch`` + ``FormData``.
 */

import type { IncidentAnalysisResult } from "@/types/incidents";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * Errors thrown by ``analyzeIncidents`` carry a human-readable message
 * suitable for display in the UI.  The optional ``statusCode`` is provided
 * for debugging / logging but is **never** a stack trace.
 */
export class ApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

/**
 * Upload a TrackFlow incident CSV file for server-side analysis.
 *
 * @param file  A ``.csv`` file selected by the user.
 * @returns     The parsed ``IncidentAnalysisResult``.
 * @throws {@link ApiError} on any failure (network, HTTP, or bad response).
 */
export async function analyzeIncidents(
  file: File,
): Promise<IncidentAnalysisResult> {
  const formData = new FormData();
  formData.append("file", file);

  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/api/incidents/analyze`, {
      method: "POST",
      body: formData,
    });
  } catch {
    throw new ApiError(
      "Could not reach the analysis server. Make sure the backend is running.",
    );
  }

  if (!response.ok) {
    let detail = `Server returned ${response.status}`;
    try {
      const body = await response.json() as { detail?: string };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // ignore parse errors — fall back to status-based message
    }
    throw new ApiError(detail, response.status);
  }

  // ── Validate response shape — trust but verify ──
  let data: unknown;
  try {
    data = await response.json() as unknown;
  } catch {
    throw new ApiError("Server returned an invalid response (not JSON).");
  }

  if (!isValidResult(data)) {
    throw new ApiError("Server returned an unexpected response structure.");
  }

  return data;
}

/**
 * Download the last successful analysis results as a CSV file.
 *
 * Returns a ``Blob`` that the caller can turn into a downloadable link.
 *
 * @throws {@link ApiError} on any failure (network or HTTP).
 */
export async function downloadResultsCsv(): Promise<Blob> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/api/incidents/results/export`);
  } catch {
    throw new ApiError(
      "Could not reach the analysis server. Make sure the backend is running.",
    );
  }

  if (!response.ok) {
    let detail = `Server returned ${response.status}`;
    try {
      const body = await response.json() as { detail?: string };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // ignore parse errors — fall back to status-based message
    }
    throw new ApiError(detail, response.status);
  }

  return response.blob();
}

/* ── internal helpers ────────────────────────────────────────────────── */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNumber(n: unknown): n is number {
  return typeof n === "number" && !Number.isNaN(n);
}

function isValidResult(value: unknown): value is IncidentAnalysisResult {
  if (!isRecord(value)) return false;

  const required = [
    "total_records",
    "valid_records",
    "invalid_records",
    "average_satisfaction",
    "closed_scored",
  ] as const;

  for (const key of required) {
    if (!isNumber(value[key])) return false;
  }

  // Breakdowns must be objects with number values
  const dictKeys = [
    "invalid_breakdown",
    "category_breakdown",
    "status_breakdown",
    "country_breakdown",
    "score_distribution",
  ] as const;

  for (const key of dictKeys) {
    const val = value[key];
    if (!isRecord(val)) return false;
    for (const v of Object.values(val)) {
      if (typeof v !== "number") return false;
    }
  }

  return true;
}