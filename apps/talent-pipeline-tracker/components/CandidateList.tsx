import type { Candidate } from "@/types/candidate";

import { CandidateCard } from "./CandidateCard";

interface CandidateListProps {
  candidates: Candidate[];
}

export function CandidateList({ candidates }: CandidateListProps) {
  if (candidates.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-600">
        No hay candidaturas para mostrar.
      </div>
    );
  }

  return (
    <section className="grid gap-4" aria-label="Listado de candidaturas">
      {candidates.map((candidate) => (
        <CandidateCard key={candidate.id} candidate={candidate} />
      ))}
    </section>
  );
}
