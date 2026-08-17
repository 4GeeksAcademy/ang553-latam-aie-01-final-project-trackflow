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
   */
  setSession: (accessToken: string) => Promise<void>;

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
      setIsLoading(false);
      return;
    }

    getCurrentUser()
      .then((fetchedUser) => {
        setUser(fetchedUser);
      })
      .catch(() => {
        // `authFetch` already removed the token on 401; just clear state.
        setUser(null);
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, []);

  // ── setSession ────────────────────────────────────────────────────
  const setSession = useCallback(
    async (accessToken: string): Promise<void> => {
      setToken(accessToken);
      setIsLoading(true);

      try {
        const fetchedUser = await getCurrentUser();
        setUser(fetchedUser);
      } catch {
        removeToken();
        setUser(null);
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
    } catch {
      // Only clear the user if the token was actually removed (401).
      // On transient errors (network, 5xx) the JWT may still be valid,
      // so we preserve the current user state to avoid logging out the
      // user because of a temporary server issue.
      if (!getToken()) {
        setUser(null);
      }
    }
  }, []);

  // ── logout ────────────────────────────────────────────────────────
  const logout = useCallback((): void => {
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