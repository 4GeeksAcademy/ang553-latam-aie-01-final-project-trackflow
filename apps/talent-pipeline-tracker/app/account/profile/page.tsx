/**
 * Profile page for Talent Pipeline Tracker.
 *
 * Displays the authenticated user's email (from ``AuthContext``) and
 * profile data (from ``GET /profiles/me``).  Supports editing ``name``,
 * ``phone`` and ``address`` via ``PUT /profiles/me``.
 *
 * @remarks
 * - A ``404`` response from ``GET /profiles/me`` is treated as a
 *   missing profile — the user can still create one via the edit form.
 * - The email field is **always read-only** and sourced from
 *   ``AuthContext``, never from the profile endpoint.
 */

"use client";

import { type ChangeEvent, type FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { AuthGuard } from "@/components/AuthGuard";
import { Header } from "@/components/Header";
import { useAuth } from "@/lib/AuthContext";
import { ApiError, getMyProfile, updateMyProfile } from "@/lib/authApi";
import type { UpdateProfilePayload, UserProfile } from "@/types/auth";

// ── Helpers ──────────────────────────────────────────────────────────

function notProvided(value: string | null | undefined): boolean {
  return value == null || value.trim().length === 0;
}

/** Read-only display of a single profile field. */
function ProfileField({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  return (
    <div className="border-b border-slate-200 pb-4 last:border-0 last:pb-0">
      <dt className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
        {label}
      </dt>
      <dd className="mt-1 text-base text-slate-900">
        {notProvided(value) ? (
          <span className="italic text-slate-400">Not provided</span>
        ) : (
          value
        )}
      </dd>
    </div>
  );
}

/** Editable input for a single profile field. */
function EditableField({
  label,
  name,
  value,
  onChange,
  disabled,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  disabled: boolean;
}) {
  return (
    <div>
      <label
        htmlFor={`profile-${name}`}
        className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500"
      >
        {label}
      </label>
      <input
        id={`profile-${name}`}
        name={name}
        type="text"
        value={value}
        onChange={onChange}
        disabled={disabled}
        className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-base text-slate-900 placeholder-slate-400 transition-colors focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-500"
      />
    </div>
  );
}

// ── Page component ───────────────────────────────────────────────────

export default function ProfilePage() {
  const { user } = useAuth();

  // ── Data state ────────────────────────────────────────────────────
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [isProfileLoading, setIsProfileLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ── Edit state ────────────────────────────────────────────────────
  const [isEditing, setIsEditing] = useState(false);
  const [formValues, setFormValues] = useState<{ name: string; phone: string; address: string }>({ name: "", phone: "", address: "" });
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // ── Fetch profile on mount ────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    setIsProfileLoading(true);
    setLoadError(null);

    getMyProfile()
      .then((data) => {
        if (!cancelled) {
          setProfile(data);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;

        if (err instanceof ApiError && err.statusCode === 404) {
          // No profile yet — that's okay, the edit form will create one.
          setProfile(null);
          return;
        }

        setLoadError(
          err instanceof Error ? err.message : "Failed to load profile.",
        );
      })
      .finally(() => {
        if (!cancelled) {
          setIsProfileLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  /** Initialise form values from a profile (or empty strings). */
  const initForm = useCallback((p: UserProfile | null) => {
    setFormValues({
      name: p?.name ?? "",
      phone: p?.phone ?? "",
      address: p?.address ?? "",
    });
  }, []);

  // ── Enter edit mode ───────────────────────────────────────────────
  const handleStartEditing = useCallback(() => {
    initForm(profile);
    setSaveError(null);
    setSuccessMessage(null);
    setIsEditing(true);
  }, [profile, initForm]);

  // ── Cancel editing ────────────────────────────────────────────────
  const handleCancel = useCallback(() => {
    setIsEditing(false);
    setSaveError(null);
    initForm(profile); // discard local changes
  }, [profile, initForm]);

  // ── Input change handler ──────────────────────────────────────────
  const handleInputChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const { name, value } = e.target;
      setFormValues((prev: { name: string; phone: string; address: string }) => ({ ...prev, [name]: value }));
    },
    [],
  );

  // ── Save (PUT /profiles/me) ───────────────────────────────────────
  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setIsSaving(true);
      setSaveError(null);
      setSuccessMessage(null);

      const payload: UpdateProfilePayload = {
        name: formValues.name,
        phone: formValues.phone,
        address: formValues.address,
      };

      try {
        const updatedProfile = await updateMyProfile(payload);
        setProfile(updatedProfile);
        setIsEditing(false);
        setSuccessMessage("Profile saved successfully.");
      } catch (err: unknown) {
        setSaveError(
          err instanceof Error ? err.message : "Failed to save profile.",
        );
        // Stay in edit mode — preserve user's typed values.
      } finally {
        setIsSaving(false);
      }
    },
    [formValues],
  );

  // ── Render ────────────────────────────────────────────────────────
  const showContent = !isProfileLoading && loadError == null;

  return (
    <AuthGuard>
      <Header />
      <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
        <div className="mx-auto w-full max-w-3xl">
          {/* ── Page header ── */}
          <div className="mb-6">
            <Link
              href="/"
              className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900"
            >
              &larr; Dashboard
            </Link>
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
            <header className="mb-6 border-b border-slate-200 pb-5">
              <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
                Account
              </p>
              <h1 className="mt-2 text-2xl font-bold text-slate-900">
                Profile
              </h1>
              <p className="mt-1 text-sm text-slate-600">
                Your account details and profile information.
              </p>
            </header>

            {/* ── Load error (fatal) ── */}
            {loadError != null && (
              <div className="mb-6 rounded-lg border border-rose-300 bg-rose-50 px-4 py-3">
                <p className="text-sm font-medium text-rose-700">{loadError}</p>
              </div>
            )}

            {/* ── Loading state ── */}
            {isProfileLoading && (
              <p className="text-sm text-slate-500">Loading profile...</p>
            )}

            {/* ── Success message ── */}
            {successMessage != null && (
              <div className="mb-6 rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3">
                <p className="text-sm font-medium text-emerald-700">
                  {successMessage}
                </p>
              </div>
            )}

            {/* ── Save error (only in edit mode) ── */}
            {saveError != null && isEditing && (
              <div className="mb-6 rounded-lg border border-rose-300 bg-rose-50 px-4 py-3">
                <p className="text-sm font-medium text-rose-700">{saveError}</p>
              </div>
            )}

            {/* ── Main card area ── */}
            {showContent && (
              <div className="space-y-8">
                {/* ── Email (read-only, from AuthContext) ── */}
                <section>
                  <h2 className="mb-3 text-sm font-semibold uppercase tracking-[0.15em] text-slate-500">
                    Email
                  </h2>
                  <p className="text-base text-slate-900">
                    {user?.email ?? (
                      <span className="italic text-slate-400">Not available</span>
                    )}
                  </p>
                </section>

                {/* ── Profile fields (view / edit) ── */}
                <section>
                  <h2 className="mb-4 text-sm font-semibold uppercase tracking-[0.15em] text-slate-500">
                    Profile details
                  </h2>

                  {isEditing ? (
                    <form onSubmit={handleSubmit} className="space-y-4">
                      <EditableField
                        label="Name"
                        name="name"
                        value={formValues.name}
                        onChange={handleInputChange}
                        disabled={isSaving}
                      />
                      <EditableField
                        label="Phone"
                        name="phone"
                        value={formValues.phone}
                        onChange={handleInputChange}
                        disabled={isSaving}
                      />
                      <EditableField
                        label="Address"
                        name="address"
                        value={formValues.address}
                        onChange={handleInputChange}
                        disabled={isSaving}
                      />

                      <div className="flex gap-3 pt-2">
                        <button
                          type="submit"
                          disabled={isSaving}
                          className="rounded-md bg-slate-900 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {isSaving ? "Saving..." : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={handleCancel}
                          disabled={isSaving}
                          className="rounded-md border border-slate-300 px-5 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          Cancel
                        </button>
                      </div>
                    </form>
                  ) : (
                    <>
                      <dl className="space-y-4">
                        <ProfileField label="Name" value={profile?.name} />
                        <ProfileField label="Phone" value={profile?.phone} />
                        <ProfileField label="Address" value={profile?.address} />
                      </dl>

                      <button
                        type="button"
                        onClick={handleStartEditing}
                        className="mt-5 rounded-md bg-slate-900 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700"
                      >
                        Edit profile
                      </button>
                    </>
                  )}
                </section>
              </div>
            )}
          </div>
        </div>
      </main>
    </AuthGuard>
  );
}