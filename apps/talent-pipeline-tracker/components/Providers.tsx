/**
 * Client-side providers wrapper for Talent Pipeline Tracker.
 *
 * Aggregates all context providers that require ``"use client"`` so that
 * the root ``layout.tsx`` can remain a **server component**.
 *
 * Currently wraps the app in ``AuthProvider`` for global session state.
 */

"use client";

import { AuthProvider } from "@/lib/AuthContext";

export function Providers({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  return <AuthProvider>{children}</AuthProvider>;
}