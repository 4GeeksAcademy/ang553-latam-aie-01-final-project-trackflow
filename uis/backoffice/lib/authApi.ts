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
import type {
  AuthUser,
  LoginCredentials,
  LoginResponse,
  RegisterPayload,
  UpdateProfilePayload,
  UserProfile,
  ChangePasswordPayload,
  ChangePasswordResponse,
  ForgotPasswordPayload,
  ForgotPasswordResponse,
  ResetPasswordPayload,
  ResetPasswordResponse,
} from "@/types/auth";

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

/* ── Login ─────────────────────────────────────────────────────────── */

/**
 * Authenticate a user against the backend.
 *
 * Calls ``POST /auth/login`` with `application/x-www-form-urlencoded`
 * body containing ``username`` (the user's email) and ``password``.
 *
 * This is a **public** endpoint — no existing session is required, so
 * plain ``fetch`` is used instead of ``authFetch``.
 *
 * @param credentials - Email and password to authenticate.
 * @returns An object containing the JWT ``access_token`` and its type.
 * @throws {@link ApiError} on network failure or non-OK response.
 */
export async function login(
  credentials: LoginCredentials,
): Promise<LoginResponse> {
  const body = new URLSearchParams({
    username: credentials.email,
    password: credentials.password,
  });

  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/auth/login`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body,
    });
  } catch {
    throw new ApiError("Could not reach the authentication server. Make sure the backend is running.");
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as LoginResponse;
  } catch {
    throw new ApiError("Auth server returned an invalid JSON response.", response.status);
  }
}

/* ── Register ──────────────────────────────────────────────────────── */

/**
 * Register a new user in the backend.
 *
 * Calls ``POST /users`` with a JSON payload.
 * This is a **public** endpoint and does not create a session.
 *
 * @param payload - Registration data accepted by the backend.
 * @returns The created user as ``UserResponse``/``AuthUser``.
 * @throws {@link ApiError} on network failure or non-OK response.
 */
export async function register(payload: RegisterPayload): Promise<AuthUser> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/users`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
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

/* ── Profile ───────────────────────────────────────────────────────── */

/**
 * Fetch the authenticated user's profile.
 *
 * Calls ``GET /profiles/me`` with the stored JWT via ``authFetch``.
 *
 * @returns The authenticated user's profile data.
 * @throws {@link ApiError} on network failure, non-OK response, or
 *         profile-not-found (``404``).
 */
export async function getMyProfile(): Promise<UserProfile> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}/profiles/me`, {
      method: "GET",
    });
  } catch {
    throw new ApiError(
      "Could not reach the authentication server. Make sure the backend is running.",
    );
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as UserProfile;
  } catch {
    throw new ApiError(
      "Auth server returned an invalid JSON response.",
      response.status,
    );
  }
}

/**
 * Update (or create) the authenticated user's profile.
 *
 * Calls ``PUT /profiles/me`` with a JSON payload via ``authFetch``.
 *
 * If no profile exists yet the backend **creates** one automatically
 * (upsert behaviour).
 *
 * @param payload - Fields to update. Omit a field or pass ``null``
 *                  to leave it unchanged / clear it.
 * @returns The updated (or newly created) profile.
 * @throws {@link ApiError} on network failure or non-OK response.
 */
export async function updateMyProfile(
  payload: UpdateProfilePayload,
): Promise<UserProfile> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}/profiles/me`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError(
      "Could not reach the authentication server. Make sure the backend is running.",
    );
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as UserProfile;
  } catch {
    throw new ApiError(
      "Auth server returned an invalid JSON response.",
      response.status,
    );
  }
}

/* ── Change password ────────────────────────────────────────────────── */

/**
 * Change the authenticated user's password.
 *
 * Calls ``POST /auth/change-password`` with the stored JWT via
 * ``authFetch``.
 *
 * The user must provide their current password for verification.
 *
 * @param payload - Current and new password.
 * @returns A message confirming the password was changed.
 * @throws {@link ApiError} on network failure, wrong current password
 *         (400), or expired session (401 — handled by authFetch).
 */
export async function changePassword(
  payload: ChangePasswordPayload,
): Promise<ChangePasswordResponse> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}/auth/change-password`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError("Could not reach the authentication server. Make sure the backend is running.");
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as ChangePasswordResponse;
  } catch {
    throw new ApiError("Auth server returned an invalid JSON response.", response.status);
  }
}

/* ── Forgot password ────────────────────────────────────────────────── */

/**
 * Request a password-reset email.
 *
 * Calls ``POST /auth/forgot-password`` with a JSON body containing
 * the user's email.
 *
 * This is a **public** endpoint — no existing session is required, so
 * plain ``fetch`` is used instead of ``authFetch``.
 *
 * The backend always returns HTTP 200 with a generic success message
 * to prevent user enumeration.
 *
 * @param payload - Email address to send the reset link to.
 * @returns A generic message confirming an email was sent if the
 *          account exists.
 * @throws {@link ApiError} on network failure, 5xx infrastructure
 *         error, or non-OK response.
 */
export async function forgotPassword(
  payload: ForgotPasswordPayload,
): Promise<ForgotPasswordResponse> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/auth/forgot-password`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError("Could not reach the authentication server. Make sure the backend is running.");
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as ForgotPasswordResponse;
  } catch {
    throw new ApiError("Auth server returned an invalid JSON response.", response.status);
  }
}

/* ── Reset password ─────────────────────────────────────────────────── */

/**
 * Reset a forgotten password using a single-use reset token.
 *
 * Calls ``POST /auth/reset-password`` with a JSON body containing
 * the reset token and the new password.
 *
 * This is a **public** endpoint — no existing session is required, so
 * plain ``fetch`` is used instead of ``authFetch``.
 *
 * @param payload - Token and new password.
 * @returns A message confirming the password was reset.
 * @throws {@link ApiError} on network failure, invalid/expired/used
 *         token (400), or non-OK response.
 */
export async function resetPassword(
  payload: ResetPasswordPayload,
): Promise<ResetPasswordResponse> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/auth/reset-password`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError("Could not reach the authentication server. Make sure the backend is running.");
  }

  if (!response.ok) {
    const message = await extractErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as ResetPasswordResponse;
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