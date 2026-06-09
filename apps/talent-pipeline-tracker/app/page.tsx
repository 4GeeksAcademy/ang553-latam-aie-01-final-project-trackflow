"use client";

import { useEffect, useState } from "react";

import { CandidateList } from "@/components/CandidateList";
import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import { getCandidates } from "@/services/candidatesApi";
import type { Candidate } from "@/types/candidate";

export default function Home() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadCandidates() {
      setIsLoading(true);
      setError(null);

      try {
        const data = await getCandidates();
        if (!isMounted) {
          return;
        }
        setCandidates(data);
      } catch (err) {
        if (!isMounted) {
          return;
        }
        const message =
          err instanceof Error
            ? err.message
            : "Ocurrio un error al cargar las candidaturas.";
        setError(message);
      } finally {
        if (isMounted) {
          setIsLoading(false);
        }
      }
    }

    void loadCandidates();

    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto w-full max-w-5xl">
        <header className="mb-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
            TrackFlow People and Talent
          </p>
          <h1 className="mt-2 text-2xl font-bold text-slate-900">
            Talent Pipeline Tracker
          </h1>
          <p className="mt-2 text-sm text-slate-600">
            Vista general de candidaturas registradas en el proceso de seleccion.
          </p>
        </header>

        {isLoading ? <LoadingState /> : null}
        {!isLoading && error ? <ErrorState message={error} /> : null}
        {!isLoading && !error ? <CandidateList candidates={candidates} /> : null}
      </div>
    </main>
  );
}
