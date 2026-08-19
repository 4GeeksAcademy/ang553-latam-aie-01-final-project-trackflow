/**
 * Minimal JWT token management utility for TrackFlow Backoffice.
 *
 * Stores and retrieves the access token from ``localStorage``.
 * All functions are SSR-safe — they guard against accessing
 * browser-only globals during server-side rendering in Next.js.
 */

/** LocalStorage key used for the JWT access token. */
const TOKEN_KEY = "trackflow_access_token";

/**
 * Retrieve the stored JWT, or ``null`` when no token exists.
 * Returns ``null`` during SSR (server-side rendering).
 */
export function getToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * Persist a JWT in ``localStorage``.
 * This operation is a no‑op during SSR.
 *
 * @param token - The raw JWT string to store.
 */
export function setToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  localStorage.setItem(TOKEN_KEY, token);
}

/**
 * Remove the stored JWT from ``localStorage``.
 * Safe to call even when no token exists or during SSR.
 */
export function removeToken(): void {
  if (typeof window === "undefined") {
    return;
  }
  localStorage.removeItem(TOKEN_KEY);
}