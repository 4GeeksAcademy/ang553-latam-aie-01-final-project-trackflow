/**
 * Client-side authentication guard for Talent Pipeline Tracker.
 *
 * Wraps internal pages that require a valid session.  If the user is
 * not authenticated after hydration completes, it redirects to
 * ``/login``.
 *
 * If hydration failed due to a transient error (network, 5xx) and the
 * stored token remains valid, a friendly error is shown together with
 * a "Retry" button so the user can re‑attempt session verification
 * instead of being silently redirected to the login page.
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
  const { isLoading, isAuthenticated, authError, retryHydration } = useAuth();

  // ── Redirect unauthenticated users to login ────────────────────
  useEffect(() => {
    if (!isLoading && !isAuthenticated && !authError) {
      router.replace("/login");
    }
  }, [isLoading, isAuthenticated, authError, router]);

  // ── Show nothing during hydration ──────────────────────────────
  if (isLoading) {
    return null;
  }

  // ── Transient error during hydration ───────────────────────────
  if (authError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-100 px-4">
        <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-amber-100">
            <span className="text-2xl">⚠️</span>
          </div>
          <h2 className="mb-2 text-lg font-semibold text-slate-900">
            Connection issue
          </h2>
          <p className="mb-6 text-sm text-slate-600">{authError}</p>
          <button
            onClick={retryHydration}
            className="rounded-lg bg-blue-600 px-6 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // ── Show nothing while redirecting ─────────────────────────────
  if (!isAuthenticated) {
    return null;
  }

  // ── Authenticated — render children ────────────────────────────
  return <>{children}</>;
}