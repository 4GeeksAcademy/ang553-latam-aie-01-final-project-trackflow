"use client";

import { FormEvent, KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { BackofficeHeader } from "@/components/layout/BackofficeHeader";
import { useAuth } from "@/lib/AuthContext";
import { listOperationalClients } from "@/lib/incidentsApi";
import type { OperationalClient } from "@/types/incidents";
import { chatMessageText, chatProgressText, chatUnitCoverageText, type UnitCoverage } from "@/lib/chatState";
import { chatMemoryConversationId } from "@/lib/chatMemory";
import { useSupportChat } from "@/lib/useSupportChat";
import { ChatMemoryConsent, MemoryProposalCard } from "./ChatMemoryConsent";
import {
  askSupport,
  decideSupportMemory,
  getPendingSupportMemory,
  SupportApiError,
} from "@/lib/supportApi";
import type {
  SupportKnowledgeResult,
  SupportMemoryAction,
  SupportMemoryProposal,
  SupportQueryResponse,
} from "@/types/support";
import {
  initializeSupportAuthorization,
  SupportAuthorizationError,
  SupportAuthorizationRedirect,
} from "@/lib/supportOidc";

function KnowledgeEvidence({
  coverage,
  sources,
}: Pick<SupportKnowledgeResult, "coverage" | "sources">) {
  if (coverage !== "partial" && sources.length === 0) return null;
  return (
    <div className="mt-3 space-y-1 border-t border-white/10 pt-2 text-xs">
      {coverage === "partial" && <p className="font-medium text-amber-200">Cobertura parcial</p>}
      {sources.length > 0 && (
        <>
          <p className="text-slate-400">Fuentes consultadas</p>
          <ul className="space-y-1 break-words text-cyan-200" aria-label="Fuentes consultadas">
            {sources.map((source) => (
              <li key={`${source.source_document}:${source.section}`}>
                {source.source_document} · {source.section}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function UnitCoverageDetails({ coverage }: { coverage: UnitCoverage }) {
  const { label, missing } = chatUnitCoverageText(coverage);
  return (
    <div className="mt-2 space-y-1 text-xs text-slate-300">
      <p className="font-medium">{label}</p>
      {missing && <p>{missing}</p>}
    </div>
  );
}

const statusLabels = {
  ok: "Disponible",
  not_found: "No encontrado o no visible",
  timeout: "Tiempo de espera agotado",
  unavailable: "No disponible",
};

const memoryReadLabels: Record<string, string> = {
  used: "Se aplicó memoria activa y autorizada para esta consulta.",
  empty: "No había memoria activa relevante para esta consulta.",
  skipped: "La consulta no usó memoria persistente.",
  unavailable: "La memoria no estuvo disponible; la respuesta no la utilizó.",
};

const PENDING_SUPPORT_QUESTION_KEY = "trackflow_support_pending_question";
const SUPPORT_CONVERSATION_KEY_PREFIX = "trackflow_support_conversation_id";
const SUPPORT_DECISION_KEY_PREFIX = "trackflow_support_decision";
const SUPPORT_DECISION_INTENT_KEY_PREFIX = "trackflow_support_decision_intent";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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
  const [clients, setClients] = useState<OperationalClient[]>([]);
  const [chatDrafts, setChatDrafts] = useState<Record<string, string>>({});
  const conversationIdRef = useRef<string | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const chatComposerRef = useRef<HTMLTextAreaElement | null>(null);
  const chatPinnedRef = useRef(true);
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
    initializeSupportAuthorization()
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

  useEffect(() => {
    let active = true;
    if (!authorized || !userId) return;
    listOperationalClients()
      .then((items) => {
        if (!active) return;
        const available = items.filter((item) => item.is_active);
        setClients(available);
        if (available.length === 1) setClientId(available[0].client_id);
      })
      .catch(() => {
        if (active) setError("No se pudieron cargar los clientes autorizados.");
      });
    return () => { active = false; };
  }, [authorized, userId]);

  const chat = useSupportChat({
    userId,
    clientId,
    enabled: authorized && Boolean(clientId),
  });
  const chatDraftKey = `${userId ?? "anonymous"}:${clientId}:${chat.sessionId ?? "pending"}`;
  const chatDraft = chatDrafts[chatDraftKey] ?? "";

  useEffect(() => {
    chatPinnedRef.current = true;
  }, [chat.sessionId]);

  useEffect(() => {
    const viewport = chatScrollRef.current;
    if (!viewport || !chatPinnedRef.current) return;
    const frame = requestAnimationFrame(() => {
      viewport.scrollTop = viewport.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [chat.messages, chat.progress]);

  useEffect(() => {
    if (chat.connection !== "connected" || chat.generationActive || chat.creatingSession) return;
    chatComposerRef.current?.focus({ preventScroll: true });
  }, [chat.connection, chat.creatingSession, chat.generationActive, chat.sessionId]);

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

  const submitChat = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!chatDraft.trim()) return;
    const accepted = chat.generationActive
      ? chat.interrupt(chatDraft)
      : chat.send(chatDraft);
    if (accepted) {
      setChatDrafts((drafts) => ({ ...drafts, [chatDraftKey]: "" }));
      chatPinnedRef.current = true;
    }
  };

  const handleChatKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  const handleChatScroll = () => {
    const viewport = chatScrollRef.current;
    if (!viewport) return;
    chatPinnedRef.current = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 96;
  };

  const progressLabel = chatProgressText(chat.progress);
  const chatMemoryId = user?.role === "manager" && clients.length === 1 &&
    clients[0].client_id === clientId && chat.sessionId && chat.connection === "connected"
    ? chatMemoryConversationId(chat.sessionId)
    : null;

  return (
    <AuthGuard>
      <div className="min-h-screen">
        <BackofficeHeader />
        <main className="mx-auto max-w-5xl px-4 py-5 sm:px-6">
          {!authorized ? (
            <section role="alert" className="rounded-2xl border border-rose-400/30 bg-rose-950/30 p-5 text-sm text-rose-200"><h1 className="text-lg font-semibold">Asistente CX</h1><p className="mt-2">Tu rol no tiene acceso al asistente de soporte.</p></section>
          ) : (
            <>
            <section aria-labelledby="cx-title" className="flex h-[calc(100dvh-16rem)] min-h-[29rem] max-h-[54rem] flex-col rounded-2xl border border-cyan-400/20 bg-slate-900/70 p-4 sm:h-[calc(100dvh-11rem)] sm:min-h-[34rem] sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h1 id="cx-title" className="text-xl font-semibold text-white">Asistente CX</h1>
                <div className="flex flex-wrap items-center gap-2"><span role="status" className="rounded-full border border-white/10 px-3 py-1 text-xs text-slate-300">{chat.connection === "connected" ? "Conectado" : chat.connection === "reconnecting" ? "Reconectando…" : chat.connection === "connecting" ? "Conectando…" : "Sin conexión"}</span><button type="button" disabled={!clientId || chat.generationActive || chat.creatingSession} onClick={() => void chat.startNewSession()} className="rounded-lg border border-cyan-400/40 px-3 py-1.5 text-xs text-cyan-200 disabled:opacity-50">{chat.creatingSession ? "Creando…" : "Nueva conversación"}</button></div>
              </div>
              <div className="mt-3 flex flex-wrap items-end gap-3"><label className="min-w-56 flex-1 text-xs text-slate-300">Cliente autorizado
                <select value={clientId} onChange={(event) => setClientId(event.target.value)} disabled={chat.generationActive || chat.creatingSession} className="mt-1.5 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-sm text-white disabled:opacity-60">
                  <option value="">Selecciona un cliente</option>
                  {clients.map((client) => <option key={client.client_id} value={client.client_id}>{client.display_name}</option>)}
                </select></label><details className="text-xs text-slate-400"><summary className="cursor-pointer rounded-lg px-2 py-2 hover:text-slate-200">Detalles técnicos</summary><div className="mt-1 rounded-lg border border-white/10 bg-slate-950 p-3"><p>WebSocket autenticado</p><p className="mt-1 break-all">Sesión: {chat.sessionId ?? "pendiente"}</p><p className="mt-2">Una sesión nueva no elimina la anterior. El historial no se convierte en memoria automáticamente.</p></div></details></div>
              <div ref={chatScrollRef} onScroll={handleChatScroll} aria-live="polite" className="mt-3 min-h-0 flex-1 space-y-3 overflow-y-auto rounded-xl border border-white/10 bg-slate-950/60 p-3 sm:p-4">
                {chat.messages.length === 0 ? <div className="grid h-full place-items-center text-center"><p className="max-w-sm text-sm leading-6 text-slate-500">{clientId ? chat.connection === "connected" ? "Inicia la conversación con una consulta sobre políticas o una operación autorizada." : "Conectando con la conversación del cliente seleccionado…" : "Selecciona un cliente autorizado para iniciar una conversación."}</p></div> : chat.messages.map((message) => (
                  <article key={message.message_id} className={`rounded-xl p-3 ${message.role === "user" ? "ml-8 bg-cyan-950/40" : message.answer_kind === "clarification" ? "mr-8 border border-amber-400/30 bg-amber-950/20" : "mr-8 bg-slate-800"}`}>
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">{message.role === "user" ? "Tú" : message.answer_kind === "clarification" ? "TrackFlow CX · necesita aclaración" : "TrackFlow CX"}</p>
                    {(message.content || (!message.unit_coverage && message.status !== "interrupted")) && <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100">{chatMessageText(message) || "…"}</p>}
                    {message.publication_kind === "authorized_unit" && <p className="mt-1 text-xs text-slate-400">Unidades verificadas · QA</p>}
                    {message.unit_coverage && <UnitCoverageDetails coverage={message.unit_coverage} />}
                    {message.knowledge && <KnowledgeEvidence {...message.knowledge} />}
                    {message.status === "interrupted" && <p className="mt-2 text-xs font-medium text-amber-300">{message.content ? "Respuesta interrumpida · contenido parcial conservado" : "Respuesta interrumpida antes de publicar texto."}</p>}
                    {message.status === "failed" && <p className="mt-2 text-xs font-medium text-rose-300">Generación no disponible</p>}
                  </article>
                ))}
              </div>
              {chatMemoryId && userId && <ChatMemoryConsent
                key={JSON.stringify([userId, clientId, chat.sessionId])}
                clientId={clientId}
                conversationId={chatMemoryId}
                revision={`${chat.cursor}:${chat.errorCode ?? ""}`}
                settled={!chat.generationActive && !chat.creatingSession && !chat.interrupting &&
                  chat.errorCode !== "support_authorization_required" && chat.errorCode !== "session_unavailable"}
                decisionKey={(currentProposal, action, text) => decisionIdempotency(userId, currentProposal, action, text)}
              />}
              {chat.generationActive && <p role="status" aria-live="polite" className="mt-2 text-sm text-cyan-200">{chat.interrupting ? "Solicitando la interrupción…" : progressLabel ?? "Generando respuesta…"}</p>}
              {chat.error && <div role="alert" className="mt-3 text-sm text-rose-300"><p>{chat.error}</p>{chat.authorizationRequired ? <button type="button" onClick={() => void chat.reauthorize()} className="mt-2 rounded-lg border border-rose-400/40 px-3 py-1.5 text-xs">Volver a autorizar soporte</button> : chat.connection === "idle" && <button type="button" onClick={chat.retry} className="mt-2 rounded-lg border border-rose-400/40 px-3 py-1.5 text-xs">Reintentar conexión</button>}</div>}
              <form onSubmit={submitChat} className="mt-3 rounded-xl border border-white/10 bg-slate-950 p-3 transition focus-within:border-cyan-400/60 focus-within:ring-2 focus-within:ring-cyan-400/20">
                <label htmlFor="chat-message" className="sr-only">{chat.generationActive ? "Nueva consulta para redirigir" : "Mensaje"}</label>
                <textarea ref={chatComposerRef} id="chat-message" rows={2} maxLength={2000} value={chatDraft} onChange={(event) => setChatDrafts((drafts) => ({ ...drafts, [chatDraftKey]: event.target.value }))} onKeyDown={handleChatKeyDown} disabled={chat.connection !== "connected" || chat.interrupting} placeholder={chat.generationActive ? "Escribe otra consulta para redirigir…" : "Escribe una consulta…"} className="w-full resize-none bg-transparent px-1 py-1 text-sm text-white outline-none placeholder:text-slate-400 disabled:opacity-60" />
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-slate-400">Enter para enviar · Shift+Enter para nueva línea</span><div className="flex gap-2">{chat.generationActive && <button type="button" disabled={chat.interrupting} onClick={() => chat.interrupt()} className="rounded-lg border border-amber-400/50 px-3 py-2 text-sm text-amber-200 disabled:opacity-50">Detener</button>}<button type="submit" disabled={!chatDraft.trim() || chat.connection !== "connected" || chat.interrupting} className="rounded-lg bg-cyan-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50">{chat.generationActive ? "Redirigir" : "Enviar"}</button></div></div>
              </form>
            </section>
            <details className="mt-5 rounded-2xl border border-white/10 bg-slate-900/70"><summary className="cursor-pointer px-5 py-4 text-sm font-semibold text-slate-200">Herramientas avanzadas y memoria (HTTP secundario)</summary><form onSubmit={submit} className="border-t border-white/10 p-5">
              <p className="mb-4 text-sm leading-6 text-slate-400">Canal secundario para consultar componentes operativos y gestionar propuestas de memoria con consentimiento. No continúa el hilo WebSocket ni sustituye la conversación principal.</p>
              <div className="mb-4 grid gap-4 md:grid-cols-2">
                <label className="text-sm text-slate-200">Cliente autorizado<select value={clientId} onChange={(event) => setClientId(event.target.value)} disabled={loading || chat.generationActive || chat.creatingSession} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-white disabled:opacity-60"><option value="">Sin cliente explícito</option>{clients.map((client) => <option key={client.client_id} value={client.client_id}>{client.display_name}</option>)}</select></label>
                <label className="text-sm text-slate-200">Ámbito de memoria<select value={memoryScope} onChange={(event) => setMemoryScope(event.target.value as "client" | "global")} disabled={loading} className="mt-2 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2 text-white"><option value="client">Cliente</option>{user?.role === "admin" && <option value="global">Global (administrador)</option>}</select><span className="mt-2 block text-xs leading-5 text-slate-500">{memoryScope === "client" ? "Cliente: solo para el cliente seleccionado y usuarios autorizados." : "Global: solo administradores; aplica a conocimiento compartido permitido por la política."}</span></label>
              </div>
              <label htmlFor="support-question" className="block text-sm font-medium text-slate-200">Consulta</label>
              <textarea id="support-question" rows={5} maxLength={2000} disabled={loading} value={question} onChange={(event) => setQuestion(event.target.value)} className="mt-2 w-full resize-y rounded-xl border border-white/10 bg-slate-950 px-4 py-3 text-white outline-none focus:border-cyan-400 disabled:opacity-60" placeholder="Ej.: Check ticket TRF-000482 and product 17 stock." />
              <div className="mt-4 flex items-center justify-between gap-4"><span className="text-xs text-slate-500">{question.length}/2000</span><button type="submit" disabled={loading} className="rounded-lg bg-cyan-400 px-5 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-60">{loading ? "Consultando…" : "Consultar"}</button></div>
            </form></details>
            </>
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
            <MemoryProposalCard
              proposal={proposal}
              editing={editing}
              editText={editText}
              onChange={setEditText}
              onStartEdit={() => { setEditing(true); setEditText(JSON.stringify(proposal.content, null, 2)); }}
              onApprove={() => void decideMemory("approve")}
              onReject={() => void decideMemory("reject")}
              onEdit={() => void decideMemory("edit")}
              disabled={decisionLoading}
            />
          )}
          {result && (
            <section aria-live="polite" className="mt-6 rounded-2xl border border-white/10 bg-slate-900/70 p-6">
              {result.partial && <p className="mb-4 rounded-lg border border-amber-400/30 bg-amber-950/30 p-3 text-sm text-amber-200">Un componente de la consulta no estuvo disponible.</p>}
              {result.needs_clarification && <p className="mb-4 text-sm font-medium text-amber-200">Se necesita más información.</p>}
              <h2 className="text-lg font-semibold text-white">Respuesta</h2>
              <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-200">{result.answer}</p>
              {result.knowledge && <KnowledgeEvidence coverage={result.knowledge.coverage} sources={result.knowledge.sources} />}
              <p className="mt-4 text-xs uppercase tracking-wider text-slate-500">Ruta: {result.route}</p>
              {result.memory_read_status && <p className="mt-2 text-xs text-slate-400">{memoryReadLabels[result.memory_read_status] ?? "Estado de memoria controlado por el servicio."}</p>}
              <div className="mt-5 grid gap-4 md:grid-cols-2">
                {result.ticket && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Ticket</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.ticket.status]}</p>{result.ticket.incident_id && <p className="mt-1 text-sm text-slate-300">{result.ticket.incident_id} · {result.ticket.incident_status}</p>}</article>}
                {result.inventory && <article className="rounded-xl border border-white/10 p-4"><h3 className="font-semibold text-white">Inventario</h3><p className="mt-2 text-sm text-slate-300">{statusLabels[result.inventory.status]}</p>{result.inventory.product_id && <p className="mt-1 text-sm text-slate-300">{result.inventory.name} · SKU {result.inventory.sku} · {result.inventory.current_stock} unidades · {result.inventory.warehouse}</p>}</article>}
              </div>
            </section>
          )}
        </main>
      </div>
    </AuthGuard>
  );
}
