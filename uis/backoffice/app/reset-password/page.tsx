"use client";

import { Suspense, useState, type FormEvent } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import Link from "next/link";
import { resetPassword, ApiError } from "@/lib/authApi";

/**
 * Inner form component that reads the reset token from query parameters.
 *
 * Extracted into its own component so it can use
 * ``useSearchParams()`` and be wrapped in a ``<Suspense>`` boundary
 * as required by Next.js.
 */
function ResetPasswordForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── Token missing → show error + link to request new one ─────────
  if (!token || token.trim().length === 0) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="w-full max-w-sm">
          <div className="mb-10 text-center">
            <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">
              TrackFlow Internal
            </p>
            <h1 className="mt-3 text-2xl font-bold text-white">
              Reset your password
            </h1>
          </div>

          <div className="rounded-xl border border-white/10 bg-slate-900/60 p-8 backdrop-blur">
            <div className="mb-6 rounded-lg border border-red-400/30 bg-red-400/10 px-4 py-3 text-sm text-red-200">
              Missing password reset link.
            </div>

            <p className="mb-6 text-center text-sm text-slate-400">
              <Link
                href="/forgot-password"
                className="font-medium text-cyan-300 transition-colors hover:text-cyan-200"
              >
                Request a new reset link
              </Link>
            </p>

            <p className="mt-6 text-center text-sm text-slate-400">
              <Link
                href="/login"
                className="font-medium text-cyan-300 transition-colors hover:text-cyan-200"
              >
                Back to sign in
              </Link>
            </p>
          </div>
        </div>
      </div>
    );
  }

  // Token is guaranteed to be a non-empty string below this point.
  const resetToken: string = token;

  // ── Submit handler ────────────────────────────────────────────────
  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();

    if (submitting) {
      return;
    }

    setError(null);

    // Local validation: passwords must match
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);

    try {
      await resetPassword({ token: resetToken, new_password: newPassword });
      // Success — redirect to login
      router.replace("/login");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.statusCode === 400) {
          setError("Invalid or expired password reset link.");
        } else if (err.statusCode != null && err.statusCode >= 500) {
          setError(
            "The password reset service is temporarily unavailable. " +
              "Please try again later.",
          );
        } else {
          setError(err.message);
        }
      } else {
        setError("An unexpected error occurred. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* ── Branding ───────────────────────────────────────────── */}
        <div className="mb-10 text-center">
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">
            TrackFlow Internal
          </p>
          <h1 className="mt-3 text-2xl font-bold text-white">
            Reset your password
          </h1>
        </div>

        {/* ── Card ───────────────────────────────────────────────── */}
        <div className="rounded-xl border border-white/10 bg-slate-900/60 p-8 backdrop-blur">
          {/* Error banner */}
          {error && (
            <div className="mb-6 rounded-lg border border-red-400/30 bg-red-400/10 px-4 py-3 text-sm text-red-200">
              {error}
              {error === "Invalid or expired password reset link." && (
                <span className="block mt-2">
                  <Link
                    href="/forgot-password"
                    className="font-medium text-cyan-300 underline transition-colors hover:text-cyan-200"
                  >
                    Request a new reset link
                  </Link>
                </span>
              )}
            </div>
          )}

          <p className="mb-6 text-sm text-slate-400">
            Enter your new password below.
          </p>

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* New password */}
            <div>
              <label
                htmlFor="new-password"
                className="mb-1.5 block text-sm font-medium text-slate-300"
              >
                New password
              </label>
              <input
                id="new-password"
                type="password"
                required
                autoComplete="new-password"
                placeholder="Min. 8 characters"
                minLength={8}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={submitting}
                className="w-full rounded-lg border border-white/10 bg-slate-800/60 px-4 py-2.5 text-sm text-white placeholder-slate-500 transition-colors focus:border-cyan-400/50 focus:outline-none focus:ring-2 focus:ring-cyan-400/20 disabled:opacity-50"
              />
            </div>

            {/* Confirm new password */}
            <div>
              <label
                htmlFor="confirm-password"
                className="mb-1.5 block text-sm font-medium text-slate-300"
              >
                Confirm new password
              </label>
              <input
                id="confirm-password"
                type="password"
                required
                autoComplete="new-password"
                placeholder="Re-enter new password"
                minLength={8}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                disabled={submitting}
                className="w-full rounded-lg border border-white/10 bg-slate-800/60 px-4 py-2.5 text-sm text-white placeholder-slate-500 transition-colors focus:border-cyan-400/50 focus:outline-none focus:ring-2 focus:ring-cyan-400/20 disabled:opacity-50"
              />
            </div>

            {/* Submit */}
            <button
              type="submit"
              disabled={submitting}
              className="w-full rounded-lg bg-cyan-500 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? "Resetting..." : "Reset password"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-400">
            <Link
              href="/login"
              className="font-medium text-cyan-300 transition-colors hover:text-cyan-200"
            >
              Back to sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Reset password page.
 *
 * Reads the ``token`` query parameter from the URL and allows the
 * user to set a new password.
 *
 * The ``<Suspense>`` boundary is required by Next.js when using
 * ``useSearchParams()`` — it prevents the build from failing with
 * a missing suspense boundary error.
 */
export default function ResetPasswordPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center px-4">
          <div className="text-sm text-slate-400">Loading...</div>
        </div>
      }
    >
      <ResetPasswordForm />
    </Suspense>
  );
}