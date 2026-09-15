/**
 * Reusable authenticated HTTP client for TrackFlow Backoffice.
 *
 * Wraps the native ``fetch`` API to automatically attach the stored JWT
 * as a ``Bearer`` token and handle ``401`` responses.
 *
 * Uses the token management utilities from ``@/lib/auth`` for SSR-safe
 * ``localStorage`` access.
 *
 * @remarks
 * - Does **not** set ``Content-Type`` — the caller is responsible for it.
 *   This allows ``FormData`` (multipart) requests to work without
 *   interfering with the browser's automatic ``boundary`` header.
 * - Preserves all standard ``RequestInit`` options (``method``, ``body``,
 *   ``headers``, ``signal``, etc.).
 * - On a ``401`` response the stored token is **removed** and the
 *   browser is redirected to ``/login``.
 */

import { getToken, removeToken } from "@/lib/auth";

function createRequestId(): string | null {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    try {
      return globalThis.crypto.randomUUID();
    } catch {
      // Fall through to the cryptographic getRandomValues fallback.
    }
  }

  try {
    if (typeof globalThis.crypto?.getRandomValues !== "function") {
      return null;
    }

    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;

    const hexadecimal = Array.from(bytes, (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");

    return `${hexadecimal.slice(0, 8)}-${hexadecimal.slice(
      8,
      12,
    )}-${hexadecimal.slice(12, 16)}-${hexadecimal.slice(
      16,
      20,
    )}-${hexadecimal.slice(20)}`;
  } catch {
    return null;
  }
}

/**
 * Extended ``fetch`` that injects the current JWT as a ``Bearer`` token.
 *
 * @param url   The full request URL (the caller is responsible for
 *              prepending ``NEXT_PUBLIC_API_URL`` when needed).
 * @param init  Standard ``fetch`` options.  Any existing ``headers`` are
 *              preserved and merged with the ``Authorization`` header.
 * @returns     The native ``Response`` object.
 *
 * @example
 * ```ts
 * import { authFetch } from "@/lib/authFetch";
 * import type { Supplier } from "@/types/suppliers";
 *
 * const res = await authFetch(`${BASE_URL}/api/suppliers`, {
 *   headers: { Accept: "application/json" },
 * });
 * const suppliers: Supplier[] = await res.json();
 * ```
 */
export async function authFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const token = getToken();
  const requestId = createRequestId();

  // ── Build headers — preserve caller headers, then add Bearer token ──
  const headers = new Headers(init?.headers);
  headers.delete("X-Request-ID");

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  if (requestId !== null) {
    headers.set("X-Request-ID", requestId);
  }

  // ── Perform the request ───────────────────────────────────────────
  const response = await fetch(url, {
    ...init,
    headers,
  });

  // ── Handle 401 — remove stale token, redirect to login ──────────
  if (response.status === 401) {
    removeToken();

    if (typeof window !== "undefined") {
      window.location.replace("/login");
    }
  }

  return response;
}