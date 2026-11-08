"use client";

import { FormEvent, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import { askKnowledgeBase, KnowledgeApiError } from "@/lib/ragApi";
import type { KnowledgeQueryResponse } from "@/types/rag";

export default function KnowledgeBasePage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<KnowledgeQueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { user } = useAuth();
  const isAuthorized = user?.role === "admin" || user?.role === "manager";

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized) {
      setError("Escribe una pregunta sobre las políticas de TrackFlow.");
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setResult(null);
    try {
      setResult(await askKnowledgeBase(normalized));
    } catch (caught: unknown) {
      if (caught instanceof KnowledgeApiError && caught.statusCode === 403) {
        setError("Tu rol no tiene acceso a la base de conocimiento.");
      } else {
        setError(
          caught instanceof Error
            ? caught.message
            : "No se pudo consultar la base de conocimiento.",
        );
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-4xl px-6 py-10">
          <header className="mb-8">
            <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">
              TrackFlow Internal
            </p>
            <h1 className="mt-2 text-3xl font-bold text-white">
              Base de conocimiento
            </h1>
            <p className="mt-3 text-sm text-slate-400">
              Consulta políticas de entregas, devoluciones, transportistas y
              almacenamiento. Las respuestas se limitan al corpus aprobado.
            </p>
          </header>

          {!isAuthorized ? (
            <section
              role="alert"
              className="rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200"
            >
              Tu rol no tiene acceso a la base de conocimiento.
            </section>
          ) : (
            <>
          <form
            onSubmit={submit}
            className="rounded-2xl border border-white/10 bg-slate-900/70 p-6"
          >
            <label
              htmlFor="knowledge-question"
              className="block text-sm font-medium text-slate-200"
            >
              Pregunta
            </label>
            <textarea
              id="knowledge-question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              rows={5}
              maxLength={2000}
              disabled={isSubmitting}
              className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950 px-4 py-3 text-white outline-none focus:border-cyan-400 disabled:opacity-60"
              placeholder="Ej.: ¿Se garantiza el SLA durante Black Friday en España?"
            />
            <div className="mt-4 flex items-center justify-between gap-4">
              <span className="text-xs text-slate-500">
                {question.length}/2000
              </span>
              <button
                type="submit"
                disabled={isSubmitting}
                className="rounded-lg bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {isSubmitting ? "Consultando…" : "Consultar"}
              </button>
            </div>
          </form>

          {error && (
            <section
              role="alert"
              className="mt-6 rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200"
            >
              {error}
            </section>
          )}

          {result && (
            <section
              aria-live="polite"
              className="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-6"
            >
              <h2 className="text-lg font-semibold text-white">Respuesta</h2>
              <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-200">
                {result.answer}
              </p>
              {result.sources.length > 0 && (
                <div className="mt-5 border-t border-white/10 pt-4">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Fuentes
                  </h3>
                  <ul className="mt-2 space-y-1 text-sm text-cyan-200">
                    {result.sources.map((source) => (
                      <li key={`${source.source_document}:${source.section}`}>
                        {source.source_document} · {source.section}
                        {source.retrieval_kind === "complementary"
                          ? " · contexto complementario"
                          : " · recuperación primaria"}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          )}
            </>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}
