/**
 * Client-side route guard for TrackFlow Backoffice.
 *
 * Wraps protected views to ensure the user has a valid authentication
 * session before rendering.  Delegates all auth state to the existing
 * ``AuthProvider`` via the ``useAuth()`` hook — it never accesses
 * ``localStorage`` directly, never calls ``/auth/me``, and never
 * validates JWTs on its own.
 *
 * @remarks
 * - **While hydrating** (``isLoading === true``): renders nothing so
 *   that protected content is never flashed before the session check
 *   completes.
 * - **Unauthenticated** (``isLoading === false && isAuthenticated === false``):
 *   redirects to ``/login`` via ``router.replace()`` inside a
 *   ``useEffect`` and renders nothing.
 * - **Authenticated** (``isLoading === false && isAuthenticated === true``):
 *   renders ``children`` normally.
 *
 * @example
 * ```tsx
 * // app/dashboard/page.tsx
 * import { AuthGuard } from "@/components/layout/AuthGuard";
 *
 * export default function DashboardPage() {
 *   return (
 *     <AuthGuard>
 *       <Dashboard />
 *     </AuthGuard>
 *   );
 * }
 * ```
 */

"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";

export function AuthGuard({
  children,
}: {
  children: React.ReactNode;
}): React.ReactNode {
  const router = useRouter();
  const { isLoading, isAuthenticated } = useAuth();

  // ── Redirect unauthenticated users inside an effect ──────────────
  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [isLoading, isAuthenticated, router]);

  // ── Still hydrating — don't render anything yet ──────────────────
  if (isLoading) {
    return null;
  }

  // ── No valid session — wait for the effect redirect ──────────────
  if (!isAuthenticated) {
    return null;
  }

  // ── Authenticated — render children ──────────────────────────────
  return <>{children}</>;
}