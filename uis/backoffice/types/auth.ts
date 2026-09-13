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
}

/* ── Login types ──────────────────────────────────────────────────── */

/** Credentials required to authenticate via ``POST /auth/login``. */
export interface LoginCredentials {
  email: string;
  password: string;
}

/** Successful login response returned by ``POST /auth/login``. */
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

/* ── Register types ───────────────────────────────────────────────── */

/** Payload accepted by ``POST /users`` for public registration. */
export interface RegisterPayload {
  email: string;
  password: string;
  name?: string | null;
  phone?: string | null;
  address?: string | null;
}

export interface RegistrationResponse {
  message: string;
}

/* ── Profile types ────────────────────────────────────────────────── */

/**
 * Authenticated user's profile as returned by ``GET /profiles/me``.
 *
 * The projection contains only ``name``, ``phone``, and ``address``;
 * each field may be null according to the HTTP contract.
 */
export interface UserProfile {
  name: string | null;
  phone: string | null;
  address: string | null;
}

/**
 * Payload accepted by ``PUT /profiles/me``.
 *
 * Every field is optional. Set a field to ``null`` explicitly to
 * clear its value, or omit it entirely to leave it unchanged.
 */
export interface UpdateProfilePayload {
  name?: string | null;
  phone?: string | null;
  address?: string | null;
}

/* ── Change password types ─────────────────────────────────────────── */

/** Payload accepted by ``POST /auth/change-password``. */
export interface ChangePasswordPayload {
  current_password: string;
  new_password: string;
}

/** Response returned by ``POST /auth/change-password``. */
export interface ChangePasswordResponse {
  message: string;
}

/* ── Forgot password types ─────────────────────────────────────────── */

/** Payload accepted by ``POST /auth/forgot-password``. */
export interface ForgotPasswordPayload {
  email: string;
}

/** Response returned by ``POST /auth/forgot-password``. */
export interface ForgotPasswordResponse {
  message: string;
}

/* ── Reset password types ──────────────────────────────────────────── */

/** Payload accepted by ``POST /auth/reset-password``. */
export interface ResetPasswordPayload {
  token: string;
  new_password: string;
}

/** Response returned by ``POST /auth/reset-password``. */
export interface ResetPasswordResponse {
  message: string;
}
