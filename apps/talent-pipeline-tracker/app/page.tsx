import { Suspense } from "react";

import { CandidateDashboard } from "@/components/CandidateDashboard";
import { LoadingState } from "@/components/LoadingState";

export default function Home() {
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

        <Suspense fallback={<LoadingState />}>
          <CandidateDashboard />
        </Suspense>
      </div>
    </main>
  );
}
