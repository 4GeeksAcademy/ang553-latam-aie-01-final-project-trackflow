"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { CandidateForm } from "@/components/CandidateForm";
import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import { getCandidateById, updateCandidate } from "@/services/candidatesApi";
import type { Candidate, CandidateCreatePayload } from "@/types/candidate";

interface EditCandidatePageProps {
  params: {
    id: string;
  };
}

export default function EditCandidatePage({ params }: EditCandidatePageProps) {
  const { id } = params;

  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadCandidate() {
      setIsLoading(true);
      setLoadError(null);

      try {
        const data = await getCandidateById(id);
        if (!isMounted) {
          return;
        }
        setCandidate(data);
      } catch (err) {
        if (!isMounted) {
          return;
        }
        const message =
          err instanceof Error
            ? err.message
            : "No se pudo cargar la candidatura para editar.";
        setLoadError(message);
      } finally {
        if (isMounted) {
          setIsLoading(false);
        }
      }
    }

    void loadCandidate();

    return () => {
      isMounted = false;
    };
  }, [id]);

  async function handleUpdate(payload: CandidateCreatePayload) {
    setIsSubmitting(true);
    setSaveError(null);
    setSaveSuccess(null);

    try {
      const updated = await updateCandidate(id, payload);
      setCandidate(updated);
      setSaveSuccess("Candidatura actualizada correctamente.");
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "No se pudo actualizar la candidatura. Intentalo de nuevo.";
      setSaveError(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto w-full max-w-4xl space-y-4">
        <Link
          href={`/candidates/${id}`}
          className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900"
        >
          Volver al detalle
        </Link>

        {isLoading ? <LoadingState /> : null}
        {!isLoading && loadError ? <ErrorState message={loadError} /> : null}

        {!isLoading && !loadError && candidate ? (
          <article className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
            <header className="mb-5 border-b border-slate-200 pb-4">
              <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
                TrackFlow People and Talent
              </p>
              <h1 className="mt-2 text-2xl font-bold text-slate-900">Editar candidatura</h1>
              <p className="mt-1 text-sm text-slate-600">{candidate.full_name}</p>
            </header>

            <CandidateForm
              initialValues={candidate}
              submitLabel="Guardar cambios"
              isSubmitting={isSubmitting}
              onSubmit={handleUpdate}
            />

            {saveError ? (
              <p className="mt-3 text-sm font-medium text-rose-700">{saveError}</p>
            ) : null}
            {saveSuccess ? (
              <div className="mt-3 space-y-2">
                <p className="text-sm font-medium text-emerald-700">{saveSuccess}</p>
                <Link
                  href={`/candidates/${id}`}
                  className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900"
                >
                  Ir al detalle actualizado
                </Link>
              </div>
            ) : null}
          </article>
        ) : null}
      </div>
    </main>
  );
}
