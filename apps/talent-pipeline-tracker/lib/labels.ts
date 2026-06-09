import type { CandidateStage, CandidateStatus } from "@/types/candidate";

export const STATUS_LABELS: Record<CandidateStatus, string> = {
  received: "Recibida",
  in_progress: "En proceso",
  selected: "Seleccionada",
  discarded: "Descartada",
};

export const STAGE_LABELS: Record<CandidateStage, string> = {
  pending: "Pendiente de revisión",
  review: "En revisión",
  personal_interview: "Entrevista personal",
  technical_interview: "Entrevista técnica",
  offer_presented: "Oferta presentada",
};

export const STATUS_OPTIONS: Array<{ value: CandidateStatus; label: string }> =
  Object.entries(STATUS_LABELS).map(([value, label]) => ({
    value: value as CandidateStatus,
    label,
  }));

export const STAGE_OPTIONS: Array<{ value: CandidateStage; label: string }> =
  Object.entries(STAGE_LABELS).map(([value, label]) => ({
    value: value as CandidateStage,
    label,
  }));
