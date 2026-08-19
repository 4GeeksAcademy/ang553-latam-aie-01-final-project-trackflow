"use client";

import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useState,
} from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import { ApiError, getMyProfile, updateMyProfile } from "@/lib/authApi";
import type { UpdateProfilePayload, UserProfile } from "@/types/auth";

/** Friendly label for potentially null profile values. */
const NOT_PROVIDED = (
  <span className="italic text-slate-500">Not provided</span>
);

/** Render a single profile field row in read-only mode. */
function ProfileField({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}): React.ReactElement {
  return (
    <div className="border-b border-white/10 pb-4 last:border-0 last:pb-0">
      <dt className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300">
        {label}
      </dt>
      <dd className="mt-1.5 text-lg text-white">
        {value != null && value.trim().length > 0 ? value : NOT_PROVIDED}
      </dd>
    </div>
  );
}

/** Render a single editable input row. */
function EditableField({
  label,
  name,
  value,
  onChange,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
}): React.ReactElement {
  return (
    <div className="border-b border-white/10 pb-4 last:border-0 last:pb-0">
      <label
        htmlFor={`profile-${name}`}
        className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300"
      >
        {label}
      </label>
      <input
        id={`profile-${name}`}
        name={name}
        type="text"
        value={value}
        onChange={onChange}
        className="mt-1.5 block w-full rounded-lg border border-white/10 bg-slate-800 px-3.5 py-2.5 text-lg text-white placeholder-slate-500 transition-colors focus:border-cyan-400 focus:outline-none focus:ring-1 focus:ring-cyan-400"
      />
    </div>
  );
}

export default function ProfilePage(): React.ReactElement {
  const { user } = useAuth();

  // ── Data state ────────────────────────────────────────────────────
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [isProfileLoading, setIsProfileLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ── Edit mode state ───────────────────────────────────────────────
  const [isEditing, setIsEditing] = useState(false);
  const [formValues, setFormValues] = useState({
    name: "",
    phone: "",
    address: "",
  });
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

        // 404 means the user has no profile yet — that's not a fatal error.
        if (err instanceof ApiError && err.statusCode === 404) {
          setProfile(null);
          return;
        }

        if (err instanceof ApiError) {
          setLoadError(err.message);
        } else if (err instanceof Error) {
          setLoadError(err.message);
        } else {
          setLoadError("Failed to load profile.");
        }
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

  /** Initialise form values from the current profile (or empty strings). */
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
    initForm(profile); // Discard local changes
  }, [profile, initForm]);

  // ── Handle input changes ──────────────────────────────────────────
  const handleInputChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const { name, value } = e.target;
      setFormValues((prev) => ({ ...prev, [name]: value }));
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

      // Build payload — send all three editable fields.
      // The backend checks ``if payload.name is not None: update``,
      // sending an empty string ``""`` updates the field to empty.
      // We always send all fields since the form is fully controlled.
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
        if (err instanceof ApiError) {
          setSaveError(err.message);
        } else if (err instanceof Error) {
          setSaveError(err.message);
        } else {
          setSaveError("Failed to save profile.");
        }
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
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-7xl px-6 py-10">
          {/* ── Page header ── */}
          <div>
            <p className="text-sm font-semibold uppercase tracking-[0.25em] text-cyan-300">
              Account
            </p>
            <h2 className="mt-3 text-3xl font-bold text-white">Profile</h2>
            <p className="mt-3 max-w-3xl text-slate-300">
              Your account details and profile information.
            </p>
          </div>

          {/* ── Load error (fatal) ── */}
          {loadError != null && (
            <div className="mt-8 rounded-lg border border-rose-400/30 bg-rose-400/10 px-5 py-4">
              <p className="text-sm font-medium text-rose-200">{loadError}</p>
            </div>
          )}

          {/* ── Loading state ── */}
          {isProfileLoading && (
            <div className="mt-8">
              <p className="text-sm text-slate-400">Loading profile...</p>
            </div>
          )}

          {/* ── Success message ── */}
          {successMessage != null && (
            <div className="mt-8 rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-5 py-4">
              <p className="text-sm font-medium text-emerald-200">
                {successMessage}
              </p>
            </div>
          )}

          {/* ── Save error ── */}
          {saveError != null && isEditing && (
            <div className="mt-8 rounded-lg border border-rose-400/30 bg-rose-400/10 px-5 py-4">
              <p className="text-sm font-medium text-rose-200">{saveError}</p>
            </div>
          )}

          {/* ── Main card area ── */}
          {showContent && (
            <div className="mt-8 grid gap-8 md:grid-cols-2">
              {/* ── Email (always read-only, from AuthContext) ── */}
              <div className="rounded-xl border border-white/10 bg-slate-900/50 p-6">
                <h3 className="mb-5 text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                  Account
                </h3>
                <dl className="space-y-4">
                  <ProfileField label="Email" value={user?.email ?? null} />
                </dl>
              </div>

              {/* ── Profile Details (read or edit) ── */}
              <div className="rounded-xl border border-white/10 bg-slate-900/50 p-6">
                <div className="mb-5 flex items-center justify-between">
                  <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-400">
                    Profile Details
                  </h3>

                  {!isEditing && (
                    <button
                      type="button"
                      onClick={handleStartEditing}
                      className="rounded-lg border border-cyan-400/30 bg-cyan-400/10 px-3.5 py-1.5 text-xs font-medium text-cyan-200 transition-colors hover:bg-cyan-400/20 hover:text-cyan-100"
                    >
                      Edit profile
                    </button>
                  )}
                </div>

                {isEditing ? (
                  <form onSubmit={handleSubmit}>
                    <dl className="space-y-4">
                      <EditableField
                        label="Name"
                        name="name"
                        value={formValues.name}
                        onChange={handleInputChange}
                      />
                      <EditableField
                        label="Phone"
                        name="phone"
                        value={formValues.phone}
                        onChange={handleInputChange}
                      />
                      <EditableField
                        label="Address"
                        name="address"
                        value={formValues.address}
                        onChange={handleInputChange}
                      />
                    </dl>

                    <div className="mt-6 flex gap-3">
                      <button
                        type="submit"
                        disabled={isSaving}
                        className="rounded-lg bg-cyan-500 px-5 py-2 text-sm font-semibold text-slate-950 transition-colors hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {isSaving ? "Saving..." : "Save"}
                      </button>
                      <button
                        type="button"
                        onClick={handleCancel}
                        disabled={isSaving}
                        className="rounded-lg border border-white/10 px-5 py-2 text-sm font-medium text-slate-300 transition-colors hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                ) : (
                  <dl className="space-y-4">
                    <ProfileField label="Name" value={profile?.name ?? null} />
                    <ProfileField
                      label="Phone"
                      value={profile?.phone ?? null}
                    />
                    <ProfileField
                      label="Address"
                      value={profile?.address ?? null}
                    />
                  </dl>
                )}
              </div>
            </div>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}