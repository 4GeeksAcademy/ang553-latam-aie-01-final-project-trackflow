"use client";

import { useState } from "react";

import type { CandidateCreatePayload } from "@/types/candidate";

interface CandidateFormValues {
  full_name: string;
  email: string;
  phone: string;
  position: string;
  experience_years: string;
  linkedin_url: string;
  cv_url: string;
}

interface CandidateFormProps {
  initialValues?: Partial<CandidateCreatePayload>;
  onSubmit: (payload: CandidateCreatePayload) => Promise<void>;
  submitLabel: string;
  isSubmitting: boolean;
}

type ValidationErrors = Partial<Record<keyof CandidateFormValues, string>>;

function toFormValues(initialValues?: Partial<CandidateCreatePayload>): CandidateFormValues {
  return {
    full_name: initialValues?.full_name ?? "",
    email: initialValues?.email ?? "",
    phone: initialValues?.phone ?? "",
    position: initialValues?.position ?? "",
    experience_years:
      initialValues?.experience_years !== undefined
        ? String(initialValues.experience_years)
        : "",
    linkedin_url: initialValues?.linkedin_url ?? "",
    cv_url: initialValues?.cv_url ?? "",
  };
}

export function CandidateForm({
  initialValues,
  onSubmit,
  submitLabel,
  isSubmitting,
}: CandidateFormProps) {
  const [values, setValues] = useState<CandidateFormValues>(() =>
    toFormValues(initialValues),
  );
  const [validationErrors, setValidationErrors] = useState<ValidationErrors>({});

  function setFieldValue<K extends keyof CandidateFormValues>(
    key: K,
    value: CandidateFormValues[K],
  ) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  function validate(current: CandidateFormValues): ValidationErrors {
    const errors: ValidationErrors = {};

    if (!current.full_name.trim()) {
      errors.full_name = "El nombre completo es obligatorio.";
    }

    if (!current.email.trim()) {
      errors.email = "El email es obligatorio.";
    }

    if (!current.phone.trim()) {
      errors.phone = "El telefono es obligatorio.";
    }

    if (!current.position.trim()) {
      errors.position = "La posicion es obligatoria.";
    }

    const parsedYears = Number(current.experience_years);
    if (!current.experience_years.trim()) {
      errors.experience_years = "Los anos de experiencia son obligatorios.";
    } else if (!Number.isFinite(parsedYears) || parsedYears < 0) {
      errors.experience_years =
        "Los anos de experiencia deben ser un numero valido y no negativo.";
    }

    return errors;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const errors = validate(values);
    setValidationErrors(errors);

    if (Object.keys(errors).length > 0) {
      return;
    }

    await onSubmit({
      full_name: values.full_name.trim(),
      email: values.email.trim(),
      phone: values.phone.trim(),
      position: values.position.trim(),
      experience_years: Number(values.experience_years),
      linkedin_url: values.linkedin_url.trim() ? values.linkedin_url.trim() : null,
      cv_url: values.cv_url.trim() ? values.cv_url.trim() : null,
    });
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <fieldset disabled={isSubmitting} className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <label
            htmlFor="full_name"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Nombre completo *
          </label>
          <input
            id="full_name"
            value={values.full_name}
            onChange={(event) => setFieldValue("full_name", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
          {validationErrors.full_name ? (
            <p className="mt-1 text-xs text-rose-700">{validationErrors.full_name}</p>
          ) : null}
        </div>

        <div>
          <label
            htmlFor="email"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Email *
          </label>
          <input
            id="email"
            type="email"
            value={values.email}
            onChange={(event) => setFieldValue("email", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
          {validationErrors.email ? (
            <p className="mt-1 text-xs text-rose-700">{validationErrors.email}</p>
          ) : null}
        </div>

        <div>
          <label
            htmlFor="phone"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Telefono *
          </label>
          <input
            id="phone"
            value={values.phone}
            onChange={(event) => setFieldValue("phone", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
          {validationErrors.phone ? (
            <p className="mt-1 text-xs text-rose-700">{validationErrors.phone}</p>
          ) : null}
        </div>

        <div>
          <label
            htmlFor="position"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Posicion *
          </label>
          <input
            id="position"
            value={values.position}
            onChange={(event) => setFieldValue("position", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
          {validationErrors.position ? (
            <p className="mt-1 text-xs text-rose-700">{validationErrors.position}</p>
          ) : null}
        </div>

        <div>
          <label
            htmlFor="experience_years"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            Anos de experiencia *
          </label>
          <input
            id="experience_years"
            type="number"
            min="0"
            step="0.1"
            value={values.experience_years}
            onChange={(event) =>
              setFieldValue("experience_years", event.target.value)
            }
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
          {validationErrors.experience_years ? (
            <p className="mt-1 text-xs text-rose-700">
              {validationErrors.experience_years}
            </p>
          ) : null}
        </div>

        <div>
          <label
            htmlFor="linkedin_url"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            LinkedIn
          </label>
          <input
            id="linkedin_url"
            type="url"
            value={values.linkedin_url}
            onChange={(event) => setFieldValue("linkedin_url", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
        </div>

        <div className="sm:col-span-2">
          <label
            htmlFor="cv_url"
            className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
          >
            URL de CV
          </label>
          <input
            id="cv_url"
            type="url"
            value={values.cv_url}
            onChange={(event) => setFieldValue("cv_url", event.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          />
        </div>
      </fieldset>

      <button
        type="submit"
        disabled={isSubmitting}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-70"
      >
        {isSubmitting ? "Guardando..." : submitLabel}
      </button>
    </form>
  );
}
