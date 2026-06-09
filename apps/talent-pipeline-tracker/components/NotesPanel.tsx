"use client";

import { useEffect, useMemo, useState } from "react";

import {
  createCandidateNote,
  deleteCandidateNote,
  getCandidateNotes,
} from "@/services/candidatesApi";
import type { CandidateNote } from "@/types/candidate";

interface NotesPanelProps {
  candidateId: string;
}

type FeedbackState = "idle" | "success" | "error";

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

export function NotesPanel({ candidateId }: NotesPanelProps) {
  const [notes, setNotes] = useState<CandidateNote[]>([]);
  const [isLoadingNotes, setIsLoadingNotes] = useState(true);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [newNote, setNewNote] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [deletingNoteId, setDeletingNoteId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [feedbackState, setFeedbackState] = useState<FeedbackState>("idle");

  useEffect(() => {
    let isMounted = true;

    async function loadNotes() {
      setIsLoadingNotes(true);
      setNotesError(null);

      try {
        const data = await getCandidateNotes(candidateId);
        if (!isMounted) {
          return;
        }
        setNotes(data);
      } catch (err) {
        if (!isMounted) {
          return;
        }
        const message =
          err instanceof Error
            ? err.message
            : "No se pudieron cargar las notas internas.";
        setNotesError(message);
      } finally {
        if (isMounted) {
          setIsLoadingNotes(false);
        }
      }
    }

    void loadNotes();

    return () => {
      isMounted = false;
    };
  }, [candidateId]);

  async function handleCreateNote(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const content = newNote.trim();
    if (!content) {
      setFeedbackState("error");
      setFeedback("La nota no puede estar vacia.");
      return;
    }

    setIsCreating(true);
    setFeedback(null);
    setFeedbackState("idle");

    try {
      const created = await createCandidateNote(candidateId, { content });
      setNotes((prev) => [created, ...prev]);
      setNewNote("");
      setFeedbackState("success");
      setFeedback("Nota creada correctamente.");
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "No se pudo crear la nota.";
      setFeedbackState("error");
      setFeedback(message);
    } finally {
      setIsCreating(false);
    }
  }

  async function handleDeleteNote(noteId: string) {
    setDeletingNoteId(noteId);
    setFeedback(null);
    setFeedbackState("idle");

    try {
      await deleteCandidateNote(candidateId, noteId);
      setNotes((prev) => prev.filter((note) => note.id !== noteId));
      setFeedbackState("success");
      setFeedback("Nota eliminada correctamente.");
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "No se pudo eliminar la nota.";
      setFeedbackState("error");
      setFeedback(message);
    } finally {
      setDeletingNoteId(null);
    }
  }

  const feedbackClass = useMemo(() => {
    if (feedbackState === "error") {
      return "text-rose-700";
    }
    if (feedbackState === "success") {
      return "text-emerald-700";
    }
    return "text-slate-600";
  }, [feedbackState]);

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="mb-4 border-b border-slate-200 pb-3">
        <h2 className="text-lg font-semibold text-slate-900">Notas internas</h2>
        <p className="mt-1 text-sm text-slate-600">
          Registro interno para seguimiento del proceso de seleccion.
        </p>
      </div>

      <form onSubmit={handleCreateNote} className="space-y-2">
        <label
          htmlFor="new-note"
          className="block text-xs font-semibold uppercase tracking-wide text-slate-500"
        >
          Nueva nota
        </label>
        <textarea
          id="new-note"
          value={newNote}
          onChange={(event) => setNewNote(event.target.value)}
          rows={3}
          placeholder="Escribe una observacion interna..."
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none ring-offset-2 placeholder:text-slate-400 focus:border-slate-500 focus:ring-2 focus:ring-slate-300"
          disabled={isCreating}
        />
        <div className="flex items-center justify-between gap-3">
          <button
            type="submit"
            disabled={isCreating}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {isCreating ? "Guardando nota..." : "Agregar nota"}
          </button>
          {deletingNoteId ? (
            <p className="text-xs text-slate-500">Eliminando nota...</p>
          ) : null}
        </div>
      </form>

      {feedback ? (
        <p className={`mt-3 text-sm font-medium ${feedbackClass}`}>{feedback}</p>
      ) : null}

      <div className="mt-5">
        {isLoadingNotes ? (
          <p className="text-sm text-slate-500">Cargando notas...</p>
        ) : null}

        {!isLoadingNotes && notesError ? (
          <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
            Error al cargar notas: {notesError}
          </div>
        ) : null}

        {!isLoadingNotes && !notesError && notes.length === 0 ? (
          <p className="text-sm text-slate-600">Todavia no hay notas internas.</p>
        ) : null}

        {!isLoadingNotes && !notesError && notes.length > 0 ? (
          <ul className="space-y-3">
            {notes.map((note) => (
              <li key={note.id} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <p className="whitespace-pre-wrap text-sm text-slate-800">{note.content}</p>
                <div className="mt-2 flex items-center justify-between gap-3">
                  <p className="text-xs text-slate-500">{formatDate(note.created_at)}</p>
                  <button
                    type="button"
                    onClick={() => handleDeleteNote(note.id)}
                    disabled={deletingNoteId === note.id}
                    className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-70"
                  >
                    {deletingNoteId === note.id ? "Eliminando..." : "Eliminar"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  );
}
