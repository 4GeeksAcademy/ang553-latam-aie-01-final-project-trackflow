/**
 * Client-side authentication guard for Talent Pipeline Tracker.
 *
 * Wraps internal pages that require a valid session.  If the user is
 * not authenticated after hydration completes, it redirects to
 * ``/login``.
 *
 * @remarks
 * - Consumes the existing ``AuthContext`` — it does **not** read
 *   localStorage, call ``/auth/me``, or duplicate session state.
 * - Navigation is performed inside a ``useEffect``, never during
 *   render, to avoid React state-update-on-unmounted-component
 *   warnings.
 * - Returns ``null`` both during hydration and when unauthenticated,
 *   preventing flash of protected content before the redirect fires.
 */

"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";

export function AuthGuard({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement | null {
  const router = useRouter();
  const { isLoading, isAuthenticated } = useAuth();

  // ── Redirect unauthenticated users to login ────────────────────
  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [isLoading, isAuthenticated, router]);

  // ── Show nothing during hydration ──────────────────────────────
  if (isLoading) {
    return null;
  }

  // ── Show nothing while redirecting ─────────────────────────────
  if (!isAuthenticated) {
    return null;
  }

  // ── Authenticated — render children ────────────────────────────
  return <>{children}</>;
}