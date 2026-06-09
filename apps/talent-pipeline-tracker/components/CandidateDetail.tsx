"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ErrorState } from "@/components/ErrorState";
import { LoadingState } from "@/components/LoadingState";
import {
  STAGE_LABELS,
  STAGE_OPTIONS,
  STATUS_LABELS,
  STATUS_OPTIONS,
} from "@/lib/labels";
import { getCandidateById, patchCandidate } from "@/services/candidatesApi";
import type {
  Candidate,
  CandidatePatchPayload,
  CandidateStage,
  CandidateStatus,
} from "@/types/candidate";

interface CandidateDetailProps {
  id: string;
}

type SaveState = "idle" | "saving" | "success" | "error";
type SaveField = "status" | "stage" | null;

function formatDate(dateIso: string): string {
  const parsed = new Date(dateIso);
  if (Number.isNaN(parsed.getTime())) {
    return dateIso;
  }

  return parsed.toLocaleString("es-ES", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-sm font-medium text-slate-900">{value}</dd>
    </div>
  );
}

export function CandidateDetail({ id }: CandidateDetailProps) {
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveField, setSaveField] = useState<SaveField>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadCandidate() {
      setIsLoading(true);
      setError(null);

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
            : "Ocurrio un error al cargar el detalle de la candidatura.";
        setError(message);
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

  async function updateCandidateField(field: "status" | "stage", value: string) {
    if (!candidate) {
      return;
    }

    setSaveState("saving");
    setSaveField(field);
    setSaveMessage(null);

    const payload: CandidatePatchPayload =
      field === "status"
        ? { status: value as CandidateStatus }
        : { stage: value as CandidateStage };

    try {
      const updated = await patchCandidate(id, payload);
      setCandidate(updated);
      setSaveState("success");
      setSaveMessage(
        field === "status"
          ? "Estado actualizado correctamente."
          : "Etapa actualizada correctamente.",
      );
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "No se pudo guardar el cambio. Intentalo de nuevo.";
      setSaveState("error");
      setSaveMessage(message);
    } finally {
      setSaveField(null);
    }
  }

  const saveMessageClass = useMemo(() => {
    if (saveState === "error") {
      return "text-rose-700";
    }
    if (saveState === "success") {
      return "text-emerald-700";
    }
    return "text-slate-500";
  }, [saveState]);

  if (isLoading) {
    return <LoadingState />;
  }

  if (error) {
    return (
      <div className="space-y-4">
        <Link href="/" className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900">
          Volver al listado
        </Link>
        <ErrorState message={error} />
      </div>
    );
  }

  if (!candidate) {
    return (
      <div className="space-y-4">
        <Link href="/" className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900">
          Volver al listado
        </Link>
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-600 shadow-sm">
          No se encontro la candidatura solicitada.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Link href="/" className="inline-flex text-sm font-medium text-slate-700 hover:text-slate-900">
        Volver al listado
      </Link>

      <article className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <header className="border-b border-slate-200 pb-4">
          <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">
            TrackFlow People and Talent
          </p>
          <h1 className="mt-2 text-2xl font-bold text-slate-900">{candidate.full_name}</h1>
          <p className="mt-1 text-sm text-slate-600">{candidate.position}</p>
        </header>

        <section className="mt-5 grid gap-4 sm:grid-cols-2">
          <DetailRow label="Email" value={candidate.email} />
          <DetailRow label="Telefono" value={candidate.phone} />
          <DetailRow
            label="LinkedIn"
            value={candidate.linkedin_url ?? "No disponible"}
          />
          <DetailRow label="CV" value={candidate.cv_url ?? "No disponible"} />
          <DetailRow
            label="Experiencia"
            value={`${candidate.experience_years} anos`}
          />
          <DetailRow label="Estado" value={STATUS_LABELS[candidate.status]} />
          <DetailRow label="Etapa" value={STAGE_LABELS[candidate.stage]} />
          <DetailRow label="Aplicado" value={formatDate(candidate.applied_at)} />
          <DetailRow
            label="Actualizado"
            value={formatDate(candidate.updated_at)}
          />
        </section>

        <section className="mt-6 grid gap-4 rounded-lg border border-slate-200 bg-slate-50 p-4 md:grid-cols-2">
          <div>
            <label
              htmlFor="candidate-status"
              className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
            >
              Cambiar estado
            </label>
            <select
              id="candidate-status"
              value={candidate.status}
              onChange={(event) => updateCandidateField("status", event.target.value)}
              disabled={saveState === "saving"}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {saveState === "saving" && saveField === "status" ? (
              <p className="mt-2 text-xs text-slate-500">Guardando estado...</p>
            ) : null}
          </div>

          <div>
            <label
              htmlFor="candidate-stage"
              className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500"
            >
              Cambiar etapa
            </label>
            <select
              id="candidate-stage"
              value={candidate.stage}
              onChange={(event) => updateCandidateField("stage", event.target.value)}
              disabled={saveState === "saving"}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 focus:border-slate-500 focus:ring-2 focus:ring-slate-300 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {STAGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {saveState === "saving" && saveField === "stage" ? (
              <p className="mt-2 text-xs text-slate-500">Guardando etapa...</p>
            ) : null}
          </div>
        </section>

        {saveMessage ? (
          <p className={`mt-3 text-sm font-medium ${saveMessageClass}`}>{saveMessage}</p>
        ) : null}
      </article>
    </div>
  );
}
