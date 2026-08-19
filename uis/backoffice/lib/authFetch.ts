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

  // ── Build headers — preserve caller headers, then add Bearer token ──
  const headers = new Headers(init?.headers);

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
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