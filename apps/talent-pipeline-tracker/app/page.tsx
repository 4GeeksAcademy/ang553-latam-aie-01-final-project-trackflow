import { Suspense } from "react";
import Link from "next/link";

import { CandidateDashboard } from "@/components/CandidateDashboard";
import { LoadingState } from "@/components/LoadingState";

export default function Home() {
  return (
    <main className="min-h-screen bg-slate-100 px-4 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto w-full max-w-5xl">
        <header className="mb-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
                TrackFlow People and Talent
              </p>
              <h1 className="mt-2 text-2xl font-bold text-slate-900">
                Talent Pipeline Tracker
              </h1>
              <p className="mt-2 text-sm text-slate-600">
                Vista general de candidaturas registradas en el proceso de seleccion.
              </p>
            </div>

            <Link
              href="/candidates/new"
              className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              Nueva candidatura
            </Link>
          </div>
        </header>

        <Suspense fallback={<LoadingState />}>
          <CandidateDashboard />
        </Suspense>
      </div>
    </main>
  );
}
