"use client";

import Link from "next/link";
import { useState } from "react";

import { CandidateForm } from "@/components/CandidateForm";
import { createCandidate } from "@/services/candidatesApi";
import type { CandidateCreatePayload } from "@/types/candidate";

export default function NewCandidatePage() {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [createdCandidateId, setCreatedCandidateId] = useState<string | null>(null);

  async function handleCreate(payload: CandidateCreatePayload) {
    setIsSubmitting(true);
    setError(null);
    setSuccessMessage(null);
    setCreatedCandidateId(null);

    try {
      const created = await createCandidate(payload);
      setCreatedCandidateId(created.id);
      setSuccessMessage("Candidatura registrada correctamente.");
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "No se pudo crear la candidatura. Intentalo de nuevo.";
      setError(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto w-full max-w-4xl space-y-4">
        <Link href="/" className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900">
          Volver al listado
        </Link>

        <article className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <header className="mb-5 border-b border-slate-200 pb-4">
            <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
              TrackFlow People and Talent
            </p>
            <h1 className="mt-2 text-2xl font-bold text-slate-900">Nueva candidatura</h1>
            <p className="mt-1 text-sm text-slate-600">
              Registra una nueva candidatura en el pipeline de seleccion.
            </p>
          </header>

          <CandidateForm
            submitLabel="Crear candidatura"
            isSubmitting={isSubmitting}
            onSubmit={handleCreate}
          />

          {error ? <p className="mt-3 text-sm font-medium text-rose-700">{error}</p> : null}
          {successMessage ? (
            <div className="mt-3 space-y-2">
              <p className="text-sm font-medium text-emerald-700">{successMessage}</p>
              {createdCandidateId ? (
                <Link
                  href={`/candidates/${createdCandidateId}`}
                  className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900"
                >
                  Ir al detalle de la candidatura creada
                </Link>
              ) : null}
            </div>
          ) : null}
        </article>
      </div>
    </main>
  );
}
