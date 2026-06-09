"use client";

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { CandidateFilters } from "@/components/CandidateFilters";
import { CandidateList } from "@/components/CandidateList";
import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import { getCandidates } from "@/services/candidatesApi";
import type { Candidate, CandidateStage, CandidateStatus } from "@/types/candidate";

const CANDIDATE_STATUS: CandidateStatus[] = [
  "received",
  "in_progress",
  "selected",
  "discarded",
];

const CANDIDATE_STAGE: CandidateStage[] = [
  "pending",
  "review",
  "personal_interview",
  "technical_interview",
  "offer_presented",
];

function isCandidateStatus(value: string | null): value is CandidateStatus {
  return value !== null && CANDIDATE_STATUS.includes(value as CandidateStatus);
}

function isCandidateStage(value: string | null): value is CandidateStage {
  return value !== null && CANDIDATE_STAGE.includes(value as CandidateStage);
}

export function CandidateDashboard() {
  const searchParams = useSearchParams();
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const search = (searchParams.get("search") ?? "").trim().toLowerCase();
  const statusParam = searchParams.get("status");
  const stageParam = searchParams.get("stage");

  const statusFilter = isCandidateStatus(statusParam) ? statusParam : null;
  const stageFilter = isCandidateStage(stageParam) ? stageParam : null;

  const filteredCandidates = useMemo(() => {
    return candidates.filter((candidate) => {
      if (statusFilter && candidate.status !== statusFilter) {
        return false;
      }

      if (stageFilter && candidate.stage !== stageFilter) {
        return false;
      }

      if (!search) {
        return true;
      }

      const normalizedName = candidate.full_name.toLowerCase();
      const normalizedEmail = candidate.email.toLowerCase();
      return normalizedName.includes(search) || normalizedEmail.includes(search);
    });
  }, [candidates, search, stageFilter, statusFilter]);

  const hasActiveFilters = Boolean(search || statusFilter || stageFilter);

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
    <>
      {!isLoading && !error ? <CandidateFilters /> : null}

      {isLoading ? <LoadingState /> : null}
      {!isLoading && error ? <ErrorState message={error} /> : null}
      {!isLoading && !error ? (
        <CandidateList
          candidates={filteredCandidates}
          emptyMessage={
            hasActiveFilters
              ? "No hay candidaturas que coincidan con los filtros."
              : "No hay candidaturas para mostrar."
          }
        />
      ) : null}
    </>
  );
}
