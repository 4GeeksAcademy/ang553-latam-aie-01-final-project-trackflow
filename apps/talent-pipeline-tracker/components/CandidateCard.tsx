import Link from "next/link";

import { STAGE_LABELS, STATUS_LABELS } from "@/lib/labels";
import type { Candidate } from "@/types/candidate";

interface CandidateCardProps {
  candidate: Candidate;
}

export function CandidateCard({ candidate }: CandidateCardProps) {
  return (
    <article className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">{candidate.full_name}</h2>
          <p className="mt-1 text-sm text-slate-600">{candidate.position}</p>
        </div>
        <Link
          href={`/candidates/${candidate.id}`}
          className="rounded-md bg-slate-900 px-3 py-2 text-xs font-medium text-white hover:bg-slate-700"
        >
          Ver detalle
        </Link>
      </div>

      <dl className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Estado</dt>
          <dd className="mt-1 text-sm font-medium text-slate-900">{STATUS_LABELS[candidate.status]}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Etapa</dt>
          <dd className="mt-1 text-sm font-medium text-slate-900">{STAGE_LABELS[candidate.stage]}</dd>
        </div>
      </dl>
    </article>
  );
}
