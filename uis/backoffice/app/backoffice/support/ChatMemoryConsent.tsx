"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { currentChatMemoryProposal } from "@/lib/chatMemory";
import {
  decideSupportMemory,
  getPendingSupportMemory,
  SupportApiError,
} from "@/lib/supportApi";
import type { SupportMemoryAction, SupportMemoryProposal } from "@/types/support";

const memoryCategoryLabels = {
  carrier_coverage: "Cobertura de transportista",
  recurring_incident: "Contexto de incidencia recurrente",
  b2b_report_preference: "Preferencia de reporte B2B",
};

export function MemoryProposalCard({
  proposal,
  editing,
  editText,
  onChange,
  onStartEdit,
  onApprove,
  onReject,
  onEdit,
  onCancelEdit,
  disabled,
  compact = false,
}: {
  proposal: SupportMemoryProposal;
  editing: boolean;
  editText: string;
  onChange: (text: string) => void;
  onStartEdit: () => void;
  onApprove: () => void;
  onReject: () => void;
  onEdit: () => void;
  onCancelEdit?: () => void;
  disabled: boolean;
  compact?: boolean;
}) {
  const editingLocksDecision = editing && onCancelEdit !== undefined;

  return (
    <section aria-live="polite" className={`border border-cyan-400/30 bg-cyan-950/20 ${compact ? "mt-3 max-h-64 shrink-0 overflow-y-auto rounded-lg p-3" : "mt-6 rounded-2xl p-6"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs font-semibold uppercase tracking-wider text-cyan-300">Propuesta de memoria · dato reportado por usuario</p><span className="rounded-full border border-cyan-400/30 px-2.5 py-1 text-xs text-cyan-200">{proposal.scope === "client" ? "Ámbito cliente" : "Ámbito global"}</span></div>
      <h2 className="mt-2 text-lg font-semibold text-white">{memoryCategoryLabels[proposal.category]}</h2>
      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-sm text-slate-200">{JSON.stringify(proposal.content, null, 2)}</pre>
      <p className="mt-3 text-xs leading-5 text-slate-400">{proposal.scope === "client" ? "Si la aceptas, podrá reutilizarse solo dentro del cliente indicado y por usuarios autorizados." : "Si la aceptas, podrá reutilizarse globalmente únicamente bajo la política administrativa permitida."} No se activará hasta que la confirmes. Expira: {new Date(proposal.expires_at).toLocaleString()}.</p>
      {editing && <label className="mt-4 block text-sm text-slate-200">Contenido estructurado (JSON)<textarea rows={7} value={editText} onChange={(event) => onChange(event.target.value)} disabled={disabled} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 p-3 font-mono text-sm text-white" /></label>}
      <div className="mt-4 flex flex-wrap gap-3">
        <button type="button" disabled={disabled || editingLocksDecision} onClick={onApprove} className="rounded-lg bg-emerald-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60">Aceptar</button>
        <button type="button" disabled={disabled || editingLocksDecision} onClick={onReject} className="rounded-lg border border-rose-400/50 px-4 py-2 text-sm font-semibold text-rose-200 disabled:opacity-60">Rechazar</button>
        {!editing ? <button type="button" disabled={disabled} onClick={onStartEdit} className="rounded-lg border border-cyan-400/50 px-4 py-2 text-sm font-semibold text-cyan-200 disabled:opacity-60">Editar</button> : <button type="button" disabled={disabled} onClick={onEdit} className="rounded-lg bg-cyan-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60">Crear propuesta corregida</button>}
        {editing && onCancelEdit && <button type="button" disabled={disabled} onClick={onCancelEdit} className="rounded-lg border border-white/20 px-4 py-2 text-sm text-slate-200 disabled:opacity-60">Cancelar edición</button>}
      </div>
      {editing && <p className="mt-3 text-xs text-amber-200">La corrección crea otra propuesta y requerirá una nueva confirmación.</p>}
    </section>
  );
}

type DecisionAction = "approve" | "reject" | "edit";

interface ProposalView {
  revision: string;
  proposal: SupportMemoryProposal | null;
}

export function ChatMemoryConsent({
  clientId,
  conversationId,
  revision,
  settled,
  decisionKey,
}: {
  clientId: string;
  conversationId: string;
  revision: string;
  settled: boolean;
  decisionKey: (
    proposal: SupportMemoryProposal,
    action: SupportMemoryAction,
    editedContent: string,
  ) => Promise<{ key: string; storageKey: string }>;
}) {
  const [view, setView] = useState<ProposalView | null>(null);
  const [edit, setEdit] = useState<{ proposalId: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const activeRef = useRef(false);
  const settledRef = useRef<string | null>(null);
  const readRef = useRef<AbortController | null>(null);
  const decisionRef = useRef<AbortController | null>(null);

  useLayoutEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      decisionRef.current?.abort();
    };
  }, []);

  useLayoutEffect(() => {
    settledRef.current = settled ? revision : null;
    return () => {
      settledRef.current = null;
      readRef.current?.abort();
    };
  }, [revision, settled]);

  const reload = useCallback(async () => {
    const currentRevision = settledRef.current;
    if (currentRevision === null || !activeRef.current) return;
    readRef.current?.abort();
    const request = new AbortController();
    readRef.current = request;
    try {
      const pending = await getPendingSupportMemory(
        { conversationId },
        { signal: request.signal, currentAuthorizationOnly: true },
      );
      if (request.signal.aborted) return;
      if (pending.conversation_id !== conversationId) throw new Error("mismatched pending conversation");
      const proposal = currentChatMemoryProposal(pending.proposal, conversationId, clientId);
      setView({ revision: currentRevision, proposal });
      setEdit((previous) => previous?.proposalId === proposal?.id ? previous : null);
      setReadError(null);
    } catch (error) {
      if (request.signal.aborted) return;
      setView({ revision: currentRevision, proposal: null });
      setEdit(null);
      setReadError(error instanceof SupportApiError && error.statusCode === 404
        ? null
        : error instanceof SupportApiError ? error.message : "No se pudo consultar la memoria de esta conversación.");
    }
  }, [clientId, conversationId]);

  useEffect(() => {
    if (!settled) return;
    void reload();
    const onFocus = () => {
      if (!decisionRef.current) void reload();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reload, revision, settled]);

  const proposal = settled && view?.revision === revision
    ? currentChatMemoryProposal(view.proposal, conversationId, clientId)
    : null;
  const expiresAt = proposal ? Date.parse(proposal.expires_at) : null;
  useEffect(() => {
    if (expiresAt === null) return;
    const timer = window.setTimeout(() => {
      setView(null);
      setEdit(null);
      if (!decisionRef.current) setMessage({ text: "La propuesta de memoria expiró." });
    }, Math.min(Math.max(0, expiresAt - Date.now()), 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [expiresAt]);

  const decide = async (action: DecisionAction) => {
    if (!proposal || decisionRef.current || settledRef.current !== revision ||
        !currentChatMemoryProposal(proposal, conversationId, clientId)) return;
    if (action !== "edit" && edit?.proposalId === proposal.id) return;
    let editedContent: Record<string, unknown> | undefined;
    const editText = edit?.proposalId === proposal.id ? edit.text : "";
    if (action === "edit") {
      try {
        const value: unknown = JSON.parse(editText);
        if (typeof value !== "object" || value === null || Array.isArray(value)) {
          throw new Error("invalid object");
        }
        editedContent = value as Record<string, unknown>;
      } catch {
        setMessage({ error: true, text: "La edición debe ser un objeto JSON válido." });
        return;
      }
    }
    const request = new AbortController();
    decisionRef.current = request;
    readRef.current?.abort();
    setBusy(true);
    setMessage(null);
    try {
      const idempotency = await decisionKey(proposal, action, editText);
      if (request.signal.aborted || settledRef.current !== revision ||
          !currentChatMemoryProposal(proposal, conversationId, clientId)) return;
      const result = await decideSupportMemory({
        conversation_id: conversationId,
        proposal_id: proposal.id,
        idempotency_key: idempotency.key,
        action,
        ...(editedContent ? { edited_content: editedContent } : {}),
      }, { signal: request.signal, currentAuthorizationOnly: true });
      if (request.signal.aborted) return;
      if (result.conversation_id !== conversationId ||
          (result.disposition === "replacement_pending" && result.memory_active)) {
        throw new Error("invalid decision response");
      }
      window.sessionStorage.removeItem(idempotency.storageKey);
      setEdit(null);
      const notices: Record<string, string> = {
        approved: result.memory_active ? "Memoria confirmada." : "La propuesta fue aceptada.",
        rejected: "Propuesta rechazada.",
        replacement_pending: "La propuesta corregida requiere una nueva confirmación.",
        expired: "La propuesta de memoria expiró.",
      };
      setMessage({ text: notices[result.disposition] ?? "La propuesta ya no está disponible." });
    } catch (error) {
      if (!request.signal.aborted) {
        setMessage({ error: true, text: error instanceof SupportApiError ? error.message : "No se pudo confirmar el estado de la decisión." });
      }
    } finally {
      if (!request.signal.aborted) {
        setView(null);
        await reload();
        if (!request.signal.aborted) setBusy(false);
      }
      if (decisionRef.current === request) decisionRef.current = null;
    }
  };

  return (
    <>
      {readError && settled && <p role="alert" className="mt-2 text-xs text-rose-300">{readError}</p>}
      {message && settled && <p role={message.error ? "alert" : "status"} className={`mt-2 text-xs ${message.error ? "text-rose-300" : "text-amber-200"}`}>{message.text}</p>}
      {proposal && <MemoryProposalCard
        proposal={proposal}
        editing={edit?.proposalId === proposal.id}
        editText={edit?.proposalId === proposal.id ? edit.text : ""}
        onChange={(text) => setEdit({ proposalId: proposal.id, text })}
        onStartEdit={() => setEdit({ proposalId: proposal.id, text: JSON.stringify(proposal.content, null, 2) })}
        onApprove={() => void decide("approve")}
        onReject={() => void decide("reject")}
        onEdit={() => void decide("edit")}
        onCancelEdit={() => setEdit(null)}
        disabled={busy}
        compact
      />}
    </>
  );
}
