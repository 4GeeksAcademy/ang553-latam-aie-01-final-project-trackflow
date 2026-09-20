/**
 * Global authentication state for TrackFlow Backoffice.
 *
 * Provides ``AuthProvider`` (a client component) and the ``useAuth()``
 * hook so that any descendant page or component can access the current
 * session without prop drilling.
 *
 * @remarks
 * - Hydration is driven by the stored JWT — if a token exists on mount
 *   the provider calls ``GET /auth/me`` to validate it.
 * - ``isAuthenticated`` derives from the actual user object, **not**
 *   from the mere presence of a token.  This guarantees that after
 *   hydration the flag reflects a truly valid session.
 * - The provider is intentionally **non‑blocking**: public pages like
 *   ``/login`` and ``/register`` can be mounted inside this provider
 *   without being redirected or gated.
 */

"use client";

import {
  createContext,
  useCallback,
  useContext,
  useState,
  useEffect,
} from "react";
import { getToken, removeToken, setToken } from "@/lib/auth";
import { getCurrentUser } from "@/lib/authApi";
import type { AuthUser } from "@/types/auth";
import { telemetryService } from "@/lib/telemetry";

const TELEMETRY_SESSION_STORAGE_KEY = "trackflow_telemetry_session_id";

function createTelemetrySessionId(): string | null {
  try {
    if (typeof globalThis.crypto?.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }

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

function readTelemetrySessionId(): string | null {
  try {
    if (typeof window === "undefined") {
      return null;
    }

    const sessionId = window.sessionStorage.getItem(
      TELEMETRY_SESSION_STORAGE_KEY,
    );
    return sessionId && sessionId.trim() !== "" ? sessionId : null;
  } catch {
    return null;
  }
}

function writeTelemetrySessionId(sessionId: string): void {
  try {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(
        TELEMETRY_SESSION_STORAGE_KEY,
        sessionId,
      );
    }
  } catch {
    // Telemetry session persistence is best-effort.
  }
}

function removeTelemetrySessionId(): void {
  try {
    if (typeof window !== "undefined") {
      window.sessionStorage.removeItem(TELEMETRY_SESSION_STORAGE_KEY);
    }
  } catch {
    // Telemetry session cleanup is best-effort.
  }
}

function syncTelemetryIdentity(userId: string, sessionId: string | null): void {
  try {
    telemetryService.setIdentity(userId, sessionId);
  } catch {
    // Telemetry must never affect authentication.
  }
}

function clearTelemetryIdentity(): void {
  try {
    telemetryService.setIdentity(null, null);
  } catch {
    // Telemetry must never affect authentication.
  }
  removeTelemetrySessionId();
}

function establishHydratedTelemetryIdentity(userId: string): void {
  const existingSessionId = readTelemetrySessionId();
  const sessionId = existingSessionId ?? createTelemetrySessionId();

  if (sessionId !== null && existingSessionId === null) {
    writeTelemetrySessionId(sessionId);
  }

  syncTelemetryIdentity(userId, sessionId);
}

// ── Context value shape ───────────────────────────────────────────────

interface AuthContextValue {
  /** The authenticated user, or ``null`` when no valid session exists. */
  user: AuthUser | null;

  /**
   * ``true`` while the provider is performing the initial hydration
   * (checking token validity via ``/auth/me``).
   */
  isLoading: boolean;

  /**
   * Whether a **validated** authenticated session is active.
   *
   * Derives from the resolved ``user`` object — it is **not** based
   * solely on token existence, because a stored JWT may be expired or
   * otherwise invalid.
   */
  isAuthenticated: boolean;

  /**
   * Establish a new session after a successful login.
   *
   * 1. Persists the ``accessToken`` via ``setToken()``.
   * 2. Fetches the user profile via ``getCurrentUser()``.
   * 3. Updates the global state accordingly.
   *
   * @param accessToken - The raw JWT returned by the login endpoint.
   * @returns The authenticated user on success.
   * @throws The original error from ``getCurrentUser()`` on failure.
   */
  setSession: (accessToken: string) => Promise<AuthUser>;

  /**
   * Re‑fetch the current user's profile from ``/auth/me``.
   *
   * Useful for refreshing user data after a profile update without
   * requiring a full login.
   */
  refreshUser: () => Promise<void>;

  /**
   * Terminate the current session.
   *
   * Removes the stored JWT and resets the user state to ``null``.
   * Does **not** perform navigation — redirects will be handled by
   * a future route guard layer.
   */
  logout: () => void;
}

// ── Context (default value is intentionally ``null``) ────────────────

const AuthContext = createContext<AuthContextValue | null>(null);

// ── Provider ──────────────────────────────────────────────────────────

export function AuthProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // ── Hydration — validate stored JWT on mount ──────────────────────
  useEffect(() => {
    const token = getToken();

    if (!token) {
      clearTelemetryIdentity();
      setIsLoading(false);
      return;
    }

    getCurrentUser()
      .then((fetchedUser) => {
        setUser(fetchedUser);
        establishHydratedTelemetryIdentity(fetchedUser.id);
      })
      .catch(() => {
        // `authFetch` already removed the token on 401; just clear state.
        setUser(null);
        clearTelemetryIdentity();
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, []);

  // ── setSession ────────────────────────────────────────────────────
  const setSession = useCallback(
    async (accessToken: string): Promise<AuthUser> => {
      setToken(accessToken);
      setIsLoading(true);

      try {
        const fetchedUser = await getCurrentUser();
        setUser(fetchedUser);
        const sessionId = createTelemetrySessionId();
        if (sessionId !== null) {
          writeTelemetrySessionId(sessionId);
        } else {
          removeTelemetrySessionId();
        }
        syncTelemetryIdentity(fetchedUser.id, sessionId);
        return fetchedUser;
      } catch (error) {
        removeToken();
        setUser(null);
        clearTelemetryIdentity();
        throw error;
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  // ── refreshUser ───────────────────────────────────────────────────
  const refreshUser = useCallback(async (): Promise<void> => {
    try {
      const fetchedUser = await getCurrentUser();
      setUser(fetchedUser);
      establishHydratedTelemetryIdentity(fetchedUser.id);
    } catch {
      // Only clear the user if the token was actually removed (401).
      // On transient errors (network, 5xx) the JWT may still be valid,
      // so we preserve the current user state to avoid logging out the
      // user because of a temporary server issue.
      if (!getToken()) {
        setUser(null);
        clearTelemetryIdentity();
      }
    }
  }, []);

  // ── logout ────────────────────────────────────────────────────────
  const logout = useCallback((): void => {
    clearTelemetryIdentity();
    removeToken();
    setUser(null);
  }, []);

  // ── Derived values ────────────────────────────────────────────────
  const isAuthenticated = user !== null;

  const value: AuthContextValue = {
    user,
    isLoading,
    isAuthenticated,
    setSession,
    refreshUser,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ── Hook ──────────────────────────────────────────────────────────────

/**
 * Access the current authentication context.
 *
 * @throws If called outside of an ``AuthProvider``.
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an <AuthProvider />");
  }
  return context;
}