/**
 * Types for TrackFlow Backoffice Authentication API.
 *
 * Matches the structure returned by ``GET /auth/me``.
 * Fields use **snake_case** as returned by the backend — no
 * client-side transformation is performed at this layer.
 */

/** Role assigned to an authenticated user. */
export type AuthUserRole = "admin" | "manager" | "user";

/**
 * Authenticated user representation as returned by ``/auth/me``.
 */
export interface AuthUser {
  id: string;
  email: string;
  is_active: boolean;
  role: AuthUserRole;
  created_at: string;
}