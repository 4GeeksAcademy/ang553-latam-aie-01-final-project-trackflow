/**
 * Shared navigation header for authenticated views.
 *
 * Renders links to the dashboard and new-candidate pages together
 * with a logout button.  Only rendered *inside* ``<AuthGuard>`` so it
 * is never visible to unauthenticated users.
 *
 * @remarks
 * - Does **not** manipulate ``localStorage`` directly — delegates
 *   session termination to the ``logout()`` method from
 *   ``AuthContext``.
 * - On logout the user is sent to ``/login`` via ``router.replace``.
 */

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/AuthContext";

export function Header() {
  const { logout } = useAuth();
  const router = useRouter();

  const handleLogout = () => {
    logout();
    router.replace("/login");
  };

  return (
    <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/95 backdrop-blur supports-[backdrop-filter]:bg-white/80">
      <div className="mx-auto flex h-14 w-full max-w-5xl items-center justify-between px-4 sm:px-8 lg:px-12">
        {/* ── Brand ──────────────────────────────────────────────── */}
        <Link
          href="/"
          className="text-sm font-semibold tracking-tight text-slate-900"
        >
          TrackFlow
        </Link>

        {/* ── Navigation ─────────────────────────────────────────── */}
        <nav className="flex items-center gap-6">
          <Link
            href="/"
            className="text-sm font-medium text-slate-600 hover:text-slate-900"
          >
            Dashboard
          </Link>
          <Link
            href="/candidates/new"
            className="text-sm font-medium text-slate-600 hover:text-slate-900"
          >
            New candidate
          </Link>
          <Link
            href="/account/profile"
            className="text-sm font-medium text-slate-600 hover:text-slate-900"
          >
            Profile
          </Link>
          <button
            type="button"
            onClick={handleLogout}
            className="text-sm font-medium text-slate-500 hover:text-red-600"
          >
            Logout
          </button>
        </nav>
      </div>
    </header>
  );
}