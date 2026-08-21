"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { forgotPassword, ApiError } from "@/lib/authApi";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();

    if (submitting) {
      return;
    }

    setError(null);
    setSubmitting(true);

    try {
      const response = await forgotPassword({ email: email.trim() });
      setSuccessMessage(response.message);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.statusCode != null && err.statusCode >= 500) {
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

  const isDone = successMessage !== null;

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
          {/* Success banner */}
          {successMessage && (
            <div className="mb-6 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-4 py-3 text-sm text-emerald-200">
              {successMessage}
            </div>
          )}

          {/* Error banner */}
          {error && (
            <div className="mb-6 rounded-lg border border-red-400/30 bg-red-400/10 px-4 py-3 text-sm text-red-200">
              {error}
            </div>
          )}

          <p className="mb-6 text-sm text-slate-400">
            Enter the email address linked to your account and we&apos;ll send
            you a link to reset your password.
          </p>

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Email */}
            <div>
              <label
                htmlFor="email"
                className="mb-1.5 block text-sm font-medium text-slate-300"
              >
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={submitting || isDone}
                className="w-full rounded-lg border border-white/10 bg-slate-800/60 px-4 py-2.5 text-sm text-white placeholder-slate-500 transition-colors focus:border-cyan-400/50 focus:outline-none focus:ring-2 focus:ring-cyan-400/20 disabled:opacity-50"
              />
            </div>

            {/* Submit */}
            <button
              type="submit"
              disabled={submitting || isDone}
              className="w-full rounded-lg bg-cyan-500 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting
                ? "Sending..."
                : isDone
                  ? "Link sent"
                  : "Send reset link"}
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