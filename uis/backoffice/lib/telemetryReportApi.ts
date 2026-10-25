/**
 * API client for the Telemetry Report endpoint.
 *
 * Consumes ``GET /telemetry/report`` (proxied through Next.js rewrites).
 *
 * Uses the same ``authFetch`` pattern as other backoffice API clients.
 *
 * @see types/telemetry.ts for the full contract.
 */

import { authFetch } from "@/lib/authFetch";
import type { TelemetryReport } from "@/types/telemetry";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

/** Human-readable error with optional HTTP status code. */
export class ApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

/**
 * Fetch the cached telemetry report from the backend.
 *
 * Calls ``GET /telemetry/report`` with no parameters, consuming the
 * default 7-day window computed server-side.
 *
 * @returns The full ``TelemetryReport`` object.
 * @throws {@link ApiError} on network or HTTP errors.
 */
export async function getTelemetryReport(): Promise<TelemetryReport> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}/telemetry/report`, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
    }, { pathTemplate: "/telemetry/report" });
  } catch {
    throw new ApiError(
      "Could not reach the telemetry backend. Make sure the server is running.",
    );
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    const report: TelemetryReport = await response.json();
    return report;
  } catch {
    throw new ApiError(
      "Telemetry endpoint returned an invalid JSON response.",
      response.status,
    );
  }
}

/**
 * Extract a human-readable error message from a failed HTTP response.
 *
 * Handles FastAPI error shapes:
 * - ``{ "detail": "string" }``
 * - ``{ "detail": [{ "msg": "..." }] }``
 * - ``{ "message": "..." }``
 */
async function extractErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      detail?: string | Array<{ msg?: string }>;
      message?: string;
    };
    if (typeof body.detail === "string" && body.detail.trim().length > 0) {
      return body.detail;
    }
    if (Array.isArray(body.detail)) {
      const first = body.detail.find(
        (item) => typeof item?.msg === "string" && item.msg.trim().length > 0,
      );
      if (first?.msg) return first.msg;
    }
    if (typeof body.message === "string" && body.message.trim().length > 0) {
      return body.message;
    }
  } catch {
    // Ignore parse errors; fall back to status code.
  }
  return `Request failed with status ${response.status}`;
}