/**
 * Auth API client for TrackFlow Backoffice.
 *
 * Provides a function to fetch the currently authenticated user's
 * profile from ``GET /auth/me`` using the shared ``authFetch`` helper.
 *
 * @remarks
 * - ``authFetch`` automatically injects the ``Bearer`` token and
 *   removes it on ``401``, so this layer does **not** duplicate
 *   that responsibility.
 * - No navigation, no global state, no redirects — this is a pure
 *   data-fetching function.
 */

import { authFetch } from "@/lib/authFetch";
import type { AuthUser } from "@/types/auth";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

/**
 * Error thrown by auth API functions.
 * Carries a human-readable message and an optional HTTP status code.
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
 * Fetch the currently authenticated user from the backend.
 *
 * Calls ``GET /auth/me`` with the stored JWT via ``authFetch``.
 *
 * @returns The authenticated user's data.
 * @throws {@link ApiError} on network failure or non-OK response.
 */
export async function getCurrentUser(): Promise<AuthUser> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}/auth/me`, {
      method: "GET",
    });
  } catch {
    throw new ApiError("Could not reach the authentication server. Make sure the backend is running.");
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as AuthUser;
  } catch {
    throw new ApiError("Auth server returned an invalid JSON response.", response.status);
  }
}

/* ── internal helpers ──────────────────────────────────────────────── */

async function extractErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      detail?: string;
      message?: string;
    };
    if (typeof body.detail === "string" && body.detail.trim().length > 0) {
      return body.detail;
    }
    if (typeof body.message === "string" && body.message.trim().length > 0) {
      return body.message;
    }
  } catch {
    // Ignore parse errors — fall back to status-based message.
  }
  return `Request failed with status ${response.status}`;
}