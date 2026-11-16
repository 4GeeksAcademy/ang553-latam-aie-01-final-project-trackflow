"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import {
  askSupport,
  decideSupportMemory,
  getPendingSupportMemory,
  SupportApiError,
} from "@/lib/supportApi";
import type {
  SupportMemoryAction,
  SupportMemoryProposal,
  SupportQueryResponse,
} from "@/types/support";
import {
  initializeSupportAuthorization,
  SupportAuthorizationError,
  SupportAuthorizationRedirect,
} from "@/lib/supportOidc";

const statusLabels = {
  ok: "Disponible",
  not_found: "No encontrado o no visible",
  timeout: "Tiempo de espera agotado",
  unavailable: "No disponible",
};

const PENDING_SUPPORT_QUESTION_KEY = "trackflow_support_pending_question";
const SUPPORT_CONVERSATION_KEY_PREFIX = "trackflow_support_conversation_id";
const SUPPORT_DECISION_KEY_PREFIX = "trackflow_support_decision";
const SUPPORT_DECISION_INTENT_KEY_PREFIX = "trackflow_support_decision_intent";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
let supportAuthorizationInitialization: Promise<boolean> | null = null;

interface StoredDecisionIntent {
  user_id: string;
  conversation_id: string;
  proposal_id: string;
  action: SupportMemoryAction;
  idempotency_key: string;
}

interface LegacyDecisionReference {
  proposal_id: string;
  action: "approve" | "reject";
}

function initializeSupportAuthorizationOnce(): Promise<boolean> {
  supportAuthorizationInitialization ??= initializeSupportAuthorization();
  return supportAuthorizationInitialization;
}

function clearPendingSupportQuestion(): void {
  window.sessionStorage.removeItem(PENDING_SUPPORT_QUESTION_KEY);
}

function consumePendingSupportQuestion(): string | null {
  const pending = window.sessionStorage.getItem(PENDING_SUPPORT_QUESTION_KEY);
  clearPendingSupportQuestion();
  const normalized = pending?.trim() ?? "";
  return normalized && normalized.length <= 2000 ? normalized : null;
}

function conversationStorageKey(userId: string): string {
  return `${SUPPORT_CONVERSATION_KEY_PREFIX}:${userId}`;
}

function storedConversationId(userId: string): string | null {
  const value = window.sessionStorage.getItem(conversationStorageKey(userId));
  return value && UUID_PATTERN.test(value) ? value : null;
}

function decisionIntentStorageKey(userId: string): string {
  return `${SUPPORT_DECISION_INTENT_KEY_PREFIX}:${userId}`;
}

function decisionStorageKey(
  userId: string,
  proposalId: string,
  action: SupportMemoryAction,
  fingerprint = "",
): string {
  return `${SUPPORT_DECISION_KEY_PREFIX}:${userId}:${proposalId}:${action}${fingerprint}`;
}

function legacyDecisionStorageKey(
  proposalId: string,
  action: SupportMemoryAction,
  fingerprint = "",
): string {
  return `${SUPPORT_DECISION_KEY_PREFIX}:${proposalId}:${action}${fingerprint}`;
}

function isDecisionAction(value: unknown): value is SupportMemoryAction {
  return ["approve", "reject", "edit", "ambiguous", "topic_change"].includes(
    String(value),
  );
}

function readDecisionIntent(userId: string): StoredDecisionIntent | null {
  const serialized = window.sessionStorage.getItem(decisionIntentStorageKey(userId));
  if (!serialized) return null;
  try {
    const parsed = JSON.parse(serialized) as Record<string, unknown>;
    if (
      parsed.user_id !== userId ||
      typeof parsed.conversation_id !== "string" ||
      !UUID_PATTERN.test(parsed.conversation_id) ||
      typeof parsed.proposal_id !== "string" ||
      !UUID_PATTERN.test(parsed.proposal_id) ||
      !isDecisionAction(parsed.action) ||
      typeof parsed.idempotency_key !== "string" ||
      parsed.idempotency_key.length < 1 ||
      parsed.idempotency_key.length > 64
    ) {
      throw new Error("invalid decision intent");
    }
    return parsed as unknown as StoredDecisionIntent;
  } catch {
    window.sessionStorage.removeItem(decisionIntentStorageKey(userId));
    return null;
  }
}

function storeDecisionIntent(intent: StoredDecisionIntent): void {
  window.sessionStorage.setItem(
    decisionIntentStorageKey(intent.user_id),
    JSON.stringify(intent),
  );
}

function clearDecisionIntent(userId: string): void {
  window.sessionStorage.removeItem(decisionIntentStorageKey(userId));
}

function findLegacyDecisionReference(): LegacyDecisionReference | null {
  const prefix = `${SUPPORT_DECISION_KEY_PREFIX}:`;
  const matches: LegacyDecisionReference[] = [];
  for (let index = 0; index < window.sessionStorage.length; index += 1) {
    const storageKey = window.sessionStorage.key(index);
    if (!storageKey?.startsWith(prefix)) continue;
    const parts = storageKey.slice(prefix.length).split(":");
    if (
      parts.length !== 2 ||
      !UUID_PATTERN.test(parts[0]) ||
      !["approve", "reject"].includes(parts[1])
    ) continue;
    const idempotencyKey = window.sessionStorage.getItem(storageKey);
    if (!idempotencyKey || idempotencyKey.length > 64) continue;
    matches.push({
      proposal_id: parts[0],
      action: parts[1] as "approve" | "reject",
    });
  }
  return matches.length === 1 ? matches[0] : null;
}

function recoverSimpleDecisionIntent(
  userId: string,
  proposal: SupportMemoryProposal,
): StoredDecisionIntent | null {
  const existing = readDecisionIntent(userId);
  if (existing) return existing;
  const recovered = (["approve", "reject"] as const).flatMap((action) => {
    const currentKey = decisionStorageKey(userId, proposal.id, action);
    const legacyKey = legacyDecisionStorageKey(proposal.id, action);
    const idempotencyKey =
      window.sessionStorage.getItem(currentKey) ??
      window.sessionStorage.getItem(legacyKey);
    if (!idempotencyKey || idempotencyKey.length > 64) return [];
    return [{ action, idempotencyKey, currentKey, legacyKey }];
  });
  if (recovered.length !== 1) return null;
  const match = recovered[0];
  window.sessionStorage.setItem(match.currentKey, match.idempotencyKey);
  window.sessionStorage.removeItem(match.legacyKey);
  const intent: StoredDecisionIntent = {
    user_id: userId,
    conversation_id: proposal.conversation_id,
    proposal_id: proposal.id,
    action: match.action,
    idempotency_key: match.idempotencyKey,
  };
  storeDecisionIntent(intent);
  return intent;
}

async function decisionIdempotency(
  userId: string,
  proposal: SupportMemoryProposal,
  action: SupportMemoryAction,
  editText = "",
): Promise<{ key: string; storageKey: string }> {
  let fingerprint = "";
  if (action === "edit") {
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(editText),
    );
    fingerprint = `:${Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("")}`;
  }
  const storageKey = decisionStorageKey(userId, proposal.id, action, fingerprint);
  const legacyStorageKey = legacyDecisionStorageKey(
    proposal.id,
    action,
    fingerprint,
  );
  let key = window.sessionStorage.getItem(storageKey);
  if (!key) {
    key = window.sessionStorage.getItem(legacyStorageKey);
    if (key) {
      window.sessionStorage.setItem(storageKey, key);
      window.sessionStorage.removeItem(legacyStorageKey);
    }
  }
  if (!key) {
    key = globalThis.crypto.randomUUID();
    window.sessionStorage.setItem(storageKey, key);
  }
  return { key, storageKey };
}

const memoryCategoryLabels = {
  carrier_coverage: "Cobertura de transportista",
  recurring_incident: "Contexto de incidencia recurrente",
  b2b_report_preference: "Preferencia de reporte B2B",
};

export default function SupportPage() {
  const { user } = useAuth();
  const userId = user?.id;
  const authorized = user?.role === "admin" || user?.role === "manager";
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<SupportQueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [clientId, setClientId] = useState("");
  const [memoryScope, setMemoryScope] = useState<"client" | "global">("client");
  const [proposal, setProposal] = useState<SupportMemoryProposal | null>(null);
  const [interruptedIntent, setInterruptedIntent] =
    useState<StoredDecisionIntent | null>(null);
  const [decisionLoading, setDecisionLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const conversationIdRef = useRef<string | null>(null);
  const executeQuestionRef = useRef<
    (normalized: string, retainAcrossRedirect: boolean) => Promise<void>
  >(async () => undefined);

  const executeQuestion = useCallback(
    async (normalized: string, retainAcrossRedirect: boolean) => {
      if (retainAcrossRedirect) {
        window.sessionStorage.setItem(PENDING_SUPPORT_QUESTION_KEY, normalized);
      }
      setLoading(true);
      setError(null);
      try {
        const activeConversationId = conversationIdRef.current;
        const next = await askSupport(normalized, {
          conversationId: activeConversationId,
          clientId: memoryScope === "global" ? null : clientId.trim() || null,
          memoryScope,
        });
        setResult(next);
        const nextProposal = next.memory_proposal ?? null;
        setProposal(nextProposal);
        if (userId) {
          const intent = readDecisionIntent(userId);
          if (
            !nextProposal ||
            (intent &&
              (intent.conversation_id !== nextProposal.conversation_id ||
                intent.proposal_id !== nextProposal.id))
          ) {
            clearDecisionIntent(userId);
            setInterruptedIntent(null);
          }
        }
        if (next.conversation_id && UUID_PATTERN.test(next.conversation_id) && userId) {
          setConversationId(next.conversation_id);
          conversationIdRef.current = next.conversation_id;
          window.sessionStorage.setItem(
            conversationStorageKey(userId),
            next.conversation_id,
          );
        }
        clearPendingSupportQuestion();
      } catch (caught: unknown) {
        if (caught instanceof SupportAuthorizationRedirect) {
          if (!retainAcrossRedirect) clearPendingSupportQuestion();
          return;
        }
        if (caught instanceof SupportApiError && caught.statusCode === 404 && userId) {
          conversationIdRef.current = null;
          setConversationId(null);
          setProposal(null);
          window.sessionStorage.removeItem(conversationStorageKey(userId));
        }
        clearPendingSupportQuestion();
        setError(
          caught instanceof SupportApiError
            ? caught.message
            : "No se pudo completar la consulta.",
        );
      } finally {
        setLoading(false);
      }
    },
    [clientId, memoryScope, userId],
  );

  useEffect(() => {
    executeQuestionRef.current = executeQuestion;
  }, [executeQuestion]);

  useEffect(() => {
    let active = true;
    if (!userId) {
      conversationIdRef.current = null;
      queueMicrotask(() => {
        if (!active) return;
        setConversationId(null);
        setProposal(null);
        setInterruptedIntent(null);
        setResult(null);
        setClientId("");
        setMemoryScope("client");
      });
      return () => {
        active = false;
      };
    }
    const stored = storedConversationId(userId);
    const storedIntent = readDecisionIntent(userId);
    const legacyReference = stored ? null : findLegacyDecisionReference();
    const recoveryIdentifier = stored
      ? { conversationId: stored }
      : storedIntent
        ? { proposalId: storedIntent.proposal_id }
        : legacyReference
          ? { proposalId: legacyReference.proposal_id }
          : null;
    conversationIdRef.current = stored;
    queueMicrotask(() => {
      if (!active) return;
      setConversationId(stored);
      setProposal(null);
      setInterruptedIntent(null);
      setResult(null);
      setClientId("");
      setMemoryScope("client");
    });
    initializeSupportAuthorizationOnce()
      .then(async (completed) => {
        if (!active) return;
        if (completed) {
          const pending = consumePendingSupportQuestion();
          if (pending) {
            setQuestion(pending);
            void executeQuestionRef.current(pending, false);
            return;
          }
        } else {
          clearPendingSupportQuestion();
        }
        if (!recoveryIdentifier) {
          clearDecisionIntent(userId);
          return;
        }
        try {
          const recovered = await getPendingSupportMemory(recoveryIdentifier);
          if (!active) return;
          const recoveredProposal = recovered.proposal;
          setProposal(recoveredProposal);
          if (!recoveredProposal) {
            clearDecisionIntent(userId);
            return;
          }
          conversationIdRef.current = recovered.conversation_id;
          setConversationId(recovered.conversation_id);
          window.sessionStorage.setItem(
            conversationStorageKey(userId),
            recovered.conversation_id,
          );
          setMemoryScope(recoveredProposal.scope);
          setClientId(recoveredProposal.client_id ?? "");
          const intent = recoverSimpleDecisionIntent(userId, recoveredProposal);
          if (
            intent &&
            intent.conversation_id === recoveredProposal.conversation_id &&
            intent.proposal_id === recoveredProposal.id
          ) {
            setInterruptedIntent(intent);
          } else if (intent) {
            clearDecisionIntent(userId);
          }
        } catch (caught: unknown) {
          if (caught instanceof SupportAuthorizationRedirect) return;
          if (caught instanceof SupportApiError && caught.statusCode === 404) {
            conversationIdRef.current = null;
            setConversationId(null);
            window.sessionStorage.removeItem(conversationStorageKey(userId));
            clearDecisionIntent(userId);
            return;
          }
          setError(
            caught instanceof SupportApiError
              ? caught.message
              : "No se pudo recuperar la propuesta pendiente.",
          );
        }
      })
      .catch((caught: unknown) => {
        if (!active) return;
        clearPendingSupportQuestion();
        setError(
          caught instanceof SupportAuthorizationError
            ? caught.message
            : "No se pudo completar la autenticación de soporte.",
        );
      });
    return () => {
      active = false;
    };
  }, [userId]);

  const submitDecision = async (
    intent: StoredDecisionIntent,
    storageKey: string,
    editedContent?: Record<string, unknown>,
  ) => {
    if (!proposal || decisionLoading || intent.user_id !== userId) return;
    if (
      proposal.id !== intent.proposal_id ||
      proposal.conversation_id !== intent.conversation_id
    ) {
      clearDecisionIntent(intent.user_id);
      setInterruptedIntent(null);
      setError("La propuesta recuperada no coincide con la decisión pendiente.");
      return;
    }
    setDecisionLoading(true);
    setError(null);
    try {
      const decision = await decideSupportMemory({
        conversation_id: intent.conversation_id,
        proposal_id: intent.proposal_id,
        idempotency_key: intent.idempotency_key,
        action: intent.action,
        ...(editedContent ? { edited_content: editedContent } : {}),
      });
      window.sessionStorage.removeItem(storageKey);
      clearDecisionIntent(intent.user_id);
      setInterruptedIntent(null);
      setProposal(decision.proposal);
      if (decision.proposal) {
        setMemoryScope(decision.proposal.scope);
        setClientId(decision.proposal.client_id ?? "");
      }
      setEditing(false);
      setEditText("");
    } catch (caught: unknown) {
      if (caught instanceof SupportAuthorizationRedirect) return;
      if (
        caught instanceof SupportApiError &&
        (caught.statusCode === 404 || caught.statusCode === 409)
      ) {
        clearDecisionIntent(intent.user_id);
        setInterruptedIntent(null);
        setProposal(null);
      }
      setError(
        caught instanceof SupportApiError
          ? caught.message
          : "No se pudo registrar la decisión de memoria.",
      );
    } finally {
      setDecisionLoading(false);
    }
  };

  const decideMemory = async (action: SupportMemoryAction) => {
    if (!proposal || !conversationId || !userId || decisionLoading) return;
    let editedContent: Record<string, unknown> | undefined;
    if (action === "edit") {
      try {
        const parsed: unknown = JSON.parse(editText);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("invalid object");
        }
        editedContent = parsed as Record<string, unknown>;
      } catch {
        setError("La edición debe ser un objeto JSON válido.");
        return;
      }
    }
    const idempotency = await decisionIdempotency(
      userId,
      proposal,
      action,
      editText,
    );
    const intent: StoredDecisionIntent = {
      user_id: userId,
      conversation_id: conversationId,
      proposal_id: proposal.id,
      action,
      idempotency_key: idempotency.key,
    };
    storeDecisionIntent(intent);
    setInterruptedIntent(intent);
    await submitDecision(intent, idempotency.storageKey, editedContent);
  };

  const retryInterruptedDecision = async () => {
    if (!interruptedIntent || !userId || decisionLoading) return;
    if (!["approve", "reject"].includes(interruptedIntent.action)) {
      setError("Revisa de nuevo la edición antes de crear otra propuesta.");
      return;
    }
    await submitDecision(
      interruptedIntent,
      decisionStorageKey(
        userId,
        interruptedIntent.proposal_id,
        interruptedIntent.action,
      ),
    );
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = question.trim();
    if (!normalized) {
      setError("Escribe una consulta de soporte.");
      return;
    }
    await executeQuestion(normalized, true);
  };

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-4xl px-6 py-10">
          <p className="text-xs font-semibold uppercase tracking-[0.3em] text-cyan-300">TrackFlow Internal</p>
          <h1 className="mt-2 text-3xl font-bold text-white">Asistente de soporte</h1>
          <p className="mt-3 text-sm text-slate-400">Consulta políticas, tickets e inventario autorizado. Incluye un identificador cuando solicites un registro operativo.</p>

          {!authorized ? (
            <section role="alert" className="mt-8 rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200">Tu rol no tiene acceso al asistente de soporte.</section>
          ) : (
            <form onSubmit={submit} className="mt-8 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
              <div className="mb-4 grid gap-4 md:grid-cols-2">
                <label className="text-sm text-slate-200">Cliente (UUID opcional)<input value={clientId} onChange={(event) => setClientId(event.target.value)} disabled={loading} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-white" placeholder="UUID de cliente" /></label>
                <label className="text-sm text-slate-200">Ámbito de memoria<select value={memoryScope} onChange={(event) => setMemoryScope(event.target.value as "client" | "global")} disabled={loading} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-white"><option value="client">Cliente</option>{user?.role === "admin" && <option value="global">Global (administrador)</option>}</select></label>
              </div>
              <label htmlFor="support-question" className="block text-sm font-medium text-slate-200">Consulta</label>
              <textarea id="support-question" rows={5} maxLength={2000} disabled={loading} value={question} onChange={(event) => setQuestion(event.target.value)} className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950 px-4 py-3 text-white outline-none focus:border-cyan-400 disabled:opacity-60" placeholder="Ej.: Check ticket TRF-000482 and product 17 stock." />
              <div className="mt-4 flex items-center justify-between gap-4"><span className="text-xs text-slate-500">{question.length}/2000</span><button type="submit" disabled={loading} className="rounded-lg bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-60">{loading ? "Consultando…" : "Consultar"}</button></div>
            </form>
          )}

          {error && <section role="alert" className="mt-6 rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200">{error}</section>}
          {proposal && interruptedIntent && ["approve", "reject"].includes(interruptedIntent.action) && (
            <section role="status" className="mt-6 rounded-2xl border border-amber-400/30 bg-amber-950/30 p-5 text-sm text-amber-100">
              <p>Hay una decisión interrumpida. La propuesta sigue pendiente y no se ha reenviado automáticamente.</p>
              <button type="button" disabled={decisionLoading} onClick={() => void retryInterruptedDecision()} className="mt-3 rounded-lg bg-amber-300 px-4 py-2 font-semibold text-slate-950 disabled:opacity-60">
                {interruptedIntent.action === "approve" ? "Reintentar aprobación" : "Reintentar rechazo"}
              </button>
            </section>
          )}
          {proposal && (
            <section aria-live="polite" className="mt-6 rounded-2xl border border-cyan-400/30 bg-cyan-950/20 p-6">
              <p className="text-xs font-semibold uppercase tracking-wider text-cyan-300">Propuesta de memoria · dato reportado por usuario</p>
              <h2 className="mt-2 text-lg font-semibold text-white">{memoryCategoryLabels[proposal.category]}</h2>
              <pre className="mt-3 overflow-x-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-sm text-slate-200">{JSON.stringify(proposal.content, null, 2)}</pre>
              <p className="mt-3 text-xs text-slate-400">No se activará hasta que la confirmes. Expira: {new Date(proposal.expires_at).toLocaleString()}.</p>
              {editing && <label className="mt-4 block text-sm text-slate-200">Contenido estructurado (JSON)<textarea rows={7} value={editText} onChange={(event) => setEditText(event.target.value)} disabled={decisionLoading} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 p-3 font-mono text-sm text-white" /></label>}
              <div className="mt-4 flex flex-wrap gap-3">
                <button type="button" disabled={decisionLoading} onClick={() => void decideMemory("approve")} className="rounded-lg bg-emerald-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60">Aceptar</button>
                <button type="button" disabled={decisionLoading} onClick={() => void decideMemory("reject")} className="rounded-lg border border-rose-400/50 px-4 py-2 text-sm text-rose-200 disabled:opacity-60">Rechazar</button>
                {!editing ? <button type="button" disabled={decisionLoading} onClick={() => { setEditing(true); setEditText(JSON.stringify(proposal.content, null, 2)); }} className="rounded-lg border border-cyan-400/50 px-4 py-2 text-sm text-cyan-200 disabled:opacity-60">Editar</button> : <button type="button" disabled={decisionLoading} onClick={() => void decideMemory("edit")} className="rounded-lg bg-cyan-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60">Crear propuesta corregida</button>}
              </div>
              {editing && <p className="mt-3 text-xs text-amber-200">La corrección crea otra propuesta y requerirá una nueva confirmación.</p>}
            </section>
          )}
          {result && (
            <section aria-live="polite" className="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
              {result.partial && <p className="mb-4 rounded-lg border border-amber-400/30 bg-amber-950/30 p-3 text-sm text-amber-200">Respuesta parcial: una fuente no estuvo disponible.</p>}
              {result.needs_clarification && <p className="mb-4 text-sm font-medium text-amber-200">Se necesita más información.</p>}
              <h2 className="text-lg font-semibold text-white">Respuesta</h2>
              <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-200">{result.answer}</p>
              <p className="mt-4 text-xs uppercase tracking-wider text-slate-500">Ruta: {result.route}</p>
              <div className="mt-5 grid gap-4 md:grid-cols-2">
                {result.ticket && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Ticket</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.ticket.status]}</p>{result.ticket.incident_id && <p className="mt-1 text-sm text-slate-300">{result.ticket.incident_id} · {result.ticket.incident_status}</p>}</article>}
                {result.inventory && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Inventario</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.inventory.status]}</p>{result.inventory.product_id && <p className="mt-1 text-sm text-slate-300">{result.inventory.name} · SKU {result.inventory.sku} · {result.inventory.current_stock} unidades · {result.inventory.warehouse}</p>}</article>}
              </div>
              {result.knowledge && result.knowledge.sources.length > 0 && <div className="mt-5 border-t border-white/10 pt-4"><h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Fuentes</h3><ul className="mt-2 space-y-1 text-sm text-cyan-200">{result.knowledge.sources.map((source) => <li key={`${source.source_document}:${source.section}`}>{source.source_document} · {source.section}</li>)}</ul></div>}
            </section>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}
