"use client";

import { type FormEvent, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { ApiError, changePassword } from "@/lib/authApi";

export default function ChangePasswordPage(): React.ReactElement {
  // ── Form state ────────────────────────────────────────────────────
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const [isSubmitting, setIsSubmitting] = useState(false);

  // ── Feedback state ────────────────────────────────────────────────
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // ── Handle input changes ──────────────────────────────────────────
  const handleInputChange =
    (setter: (v: string) => void) =>
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setter(e.target.value);
    };

  // ── Submit ────────────────────────────────────────────────────────
  const handleSubmit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();

    // Clear previous feedback
    setErrorMessage(null);
    setSuccessMessage(null);

    // ── Local validation: passwords must match ──────────────────────
    if (newPassword !== confirmPassword) {
      setErrorMessage("New password and confirmation do not match.");
      return; // Do NOT call the API
    }

    setIsSubmitting(true);

    try {
      const result = await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setSuccessMessage(result.message ?? "Password changed successfully.");

      // Clear sensitive fields on success
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setErrorMessage(err.message);
      } else if (err instanceof Error) {
        setErrorMessage(err.message);
      } else {
        setErrorMessage("Failed to change password.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────
  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-7xl px-6 py-10">
          {/* ── Page header ── */}
          <div>
            <p className="text-sm font-semibold uppercase tracking-[0.25em] text-cyan-300">
              Account
            </p>
            <h2 className="mt-3 text-3xl font-bold text-white">
              Change Password
            </h2>
            <p className="mt-3 max-w-3xl text-slate-300">
              Update your account password. You must provide your current
              password to set a new one.
            </p>
          </div>

          {/* ── Success message ── */}
          {successMessage != null && (
            <div className="mt-8 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-5 py-4">
              <p className="text-sm font-medium text-emerald-200">
                {successMessage}
              </p>
            </div>
          )}

          {/* ── Error message ── */}
          {errorMessage != null && (
            <div className="mt-8 rounded-lg border border-rose-400/30 bg-rose-400/10 px-5 py-4">
              <p className="text-sm font-medium text-rose-200">
                {errorMessage}
              </p>
            </div>
          )}

          {/* ── Form card ── */}
          <div className="mt-8 max-w-lg">
            <div className="rounded-xl border border-white/10 bg-slate-900/50 p-6">
              <h3 className="mb-5 text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                Password
              </h3>

              <form onSubmit={handleSubmit}>
                <div className="space-y-5">
                  {/* Current password */}
                  <div>
                    <label
                      htmlFor="current-password"
                      className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300"
                    >
                      Current password
                    </label>
                    <input
                      id="current-password"
                      name="current_password"
                      type="password"
                      autoComplete="current-password"
                      value={currentPassword}
                      onChange={handleInputChange(setCurrentPassword)}
                      required
                      className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-800 px-3.5 py-2.5 text-lg text-white placeholder-slate-500 transition-colors focus:border-cyan-400 focus:outline-none focus:ring-1 focus:ring-cyan-400"
                    />
                  </div>

                  {/* New password */}
                  <div>
                    <label
                      htmlFor="new-password"
                      className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300"
                    >
                      New password
                    </label>
                    <input
                      id="new-password"
                      name="new_password"
                      type="password"
                      autoComplete="new-password"
                      value={newPassword}
                      onChange={handleInputChange(setNewPassword)}
                      required
                      className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-800 px-3.5 py-2.5 text-lg text-white placeholder-slate-500 transition-colors focus:border-cyan-400 focus:outline-none focus:ring-1 focus:ring-cyan-400"
                    />
                  </div>

                  {/* Confirm new password */}
                  <div>
                    <label
                      htmlFor="confirm-password"
                      className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300"
                    >
                      Confirm new password
                    </label>
                    <input
                      id="confirm-password"
                      name="confirm_password"
                      type="password"
                      autoComplete="new-password"
                      value={confirmPassword}
                      onChange={handleInputChange(setConfirmPassword)}
                      required
                      className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-800 px-3.5 py-2.5 text-lg text-white placeholder-slate-500 transition-colors focus:border-cyan-400 focus:outline-none focus:ring-1 focus:ring-cyan-400"
                    />
                  </div>
                </div>

                {/* ── Actions ── */}
                <div className="mt-6 flex gap-3">
                  <button
                    type="submit"
                    disabled={isSubmitting}
                    className="rounded-lg bg-cyan-500 px-5 py-2 text-sm font-semibold text-slate-950 transition-colors hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {isSubmitting ? "Changing password..." : "Change password"}
                  </button>
                </div>
              </form>
            </div>
          </div>
        </main>
      </div>
    </AuthGuard>
  );
}