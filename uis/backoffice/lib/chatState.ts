import type { KnowledgeProjection } from "../types/support";

export type ChatMessageStatus = "completed" | "streaming" | "interrupted" | "failed";
type PublicationKind = "authorized_unit";
type UnitCoverageTopic = "return_window" | "return_cost" | "return_inspection";

export interface UnitCoverage {
  status: "complete" | "partial" | "none" | "indeterminate";
  missing_topics?: UnitCoverageTopic[];
}

export interface ChatMessage {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  status: ChatMessageStatus;
  sequence: number;
  created_at: string;
  answer_kind?: "insufficient_context" | "clarification";
  knowledge?: KnowledgeProjection;
  publication_kind?: PublicationKind;
  unit_coverage?: UnitCoverage;
}

export type ChatProgressStage =
  | "processing"
  | "retrieving_context"
  | "preparing_response"
  | "validating_response"
  | "interrupting";

export interface ChatProgress {
  session_id: string;
  generation_id: string;
  stage: ChatProgressStage;
  cursor: number;
}

export interface ChatState {
  messages: ChatMessage[];
  cursor: number;
  generationActive: boolean;
  errorCode: string | null;
  redirectMessageId: string | null;
  progress: ChatProgress | null;
  activeGenerationId: string | null;
  terminalGenerationId: string | null;
  progressCursor: number;
}

export const emptyChatState: ChatState = {
  messages: [], cursor: 0, generationActive: false, errorCode: null,
  redirectMessageId: null, progress: null, activeGenerationId: null,
  terminalGenerationId: null, progressCursor: 0,
};

export interface ScopedChatState {
  sessionId: string | null;
  chat: ChatState;
}

export function reduceScopedChat(
  state: ScopedChatState,
  action: { sessionId: string; event: ChatServerEvent } | { sessionId: string; text: string; redirect?: boolean },
): ScopedChatState {
  if ("text" in action) {
    if (state.sessionId !== action.sessionId) return state;
    return { ...state, chat: addOptimisticUserMessage(state.chat, action.text, action.redirect) };
  }
  if (state.sessionId !== action.sessionId && action.event.event !== "session_snapshot") {
    return state;
  }
  return {
    sessionId: action.sessionId,
    chat: applyChatEvent(
      state.sessionId === action.sessionId ? state.chat : emptyChatState,
      action.event,
    ),
  };
}

type ChatServerEvent =
  | { event: "session_snapshot"; data: { session_id: string; messages: ChatMessage[]; cursor: number; progress?: ChatProgress | null } }
  | { event: "generation_progress"; data: ChatProgress }
  | { event: "token_chunk"; data: { session_id: string; token: string; sequence: number; message_id: string; generation_id: string; cursor: number; answer_kind?: "insufficient_context" | "clarification"; knowledge?: KnowledgeProjection; publication_kind?: PublicationKind; unit_coverage?: UnitCoverage } }
  | { event: "coverage_updated"; data: { session_id: string; message_id: string; generation_id: string; cursor: number; coverage: UnitCoverage } }
  | { event: "generation_interrupted"; data: { session_id: string; message_id: string; status: "interrupted"; generation_id: string; cursor: number } }
  | { event: "generation_completed"; data: { session_id: string; message_id: string; generation_id: string; cursor: number } }
  | { event: "error"; data: { session_id: string | null; code: string } };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const referenceId = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

const coverageTopics: Record<UnitCoverageTopic, string> = {
  return_window: "plazo de devolución",
  return_cost: "coste de devolución",
  return_inspection: "inspección de devoluciones",
};

function unitCoverage(value: unknown): value is UnitCoverage {
  if (!record(value) || typeof value.status !== "string" ||
      !["complete", "partial", "none", "indeterminate"].includes(value.status) ||
      Object.keys(value).some((key) => key !== "status" && key !== "missing_topics")) return false;
  const missing = value.missing_topics;
  return missing === undefined ||
    (Array.isArray(missing) && missing.length <= 3 && new Set(missing).size === missing.length &&
      (value.status !== "complete" || missing.length === 0) &&
      missing.every((topic) => typeof topic === "string" && Object.hasOwn(coverageTopics, topic)));
}

function knowledgeProjection(value: unknown): value is KnowledgeProjection {
  if (!record(value) || !Array.isArray(value.sources)) return false;
  if (value.coverage === "insufficient") return value.sources.length === 0;
  if (value.coverage !== undefined && value.coverage !== "complete" && value.coverage !== "partial") return false;
  return value.sources.some((source) => record(source) && source.retrieval_kind === "primary") &&
    value.sources.every((source) =>
    record(source) &&
    typeof source.source_document === "string" && referenceId.test(source.source_document) &&
    typeof source.section === "string" && referenceId.test(source.section) &&
    (source.retrieval_kind === "primary" || source.retrieval_kind === "complementary"));
}

const progressStages: ChatProgressStage[] = [
  "processing", "retrieving_context", "preparing_response",
  "validating_response", "interrupting",
];

function chatProgress(value: unknown): value is ChatProgress {
  return record(value) &&
    typeof value.session_id === "string" &&
    typeof value.generation_id === "string" &&
    progressStages.includes(value.stage as ChatProgressStage) &&
    Number.isSafeInteger(value.cursor) && Number(value.cursor) >= 0;
}

function chatMessage(value: unknown): value is ChatMessage {
  if (!record(value)) return false;
  return typeof value.message_id === "string" &&
    ["user", "assistant"].includes(String(value.role)) &&
    typeof value.content === "string" && value.content.length <= 16_000 &&
    (value.answer_kind === undefined ||
      (value.role === "assistant" && ["insufficient_context", "clarification"].includes(String(value.answer_kind)))) &&
    (value.knowledge === undefined || (value.role === "assistant" && knowledgeProjection(value.knowledge))) &&
    (value.publication_kind === undefined ||
      (value.role === "assistant" && value.publication_kind === "authorized_unit")) &&
    (value.unit_coverage === undefined ||
      (value.role === "assistant" && unitCoverage(value.unit_coverage) &&
        value.knowledge === undefined && value.answer_kind === undefined &&
        (value.content === "" || value.publication_kind === "authorized_unit"))) &&
    ["completed", "streaming", "interrupted", "failed"].includes(String(value.status)) &&
    Number.isSafeInteger(value.sequence) && Number(value.sequence) > 0 &&
    typeof value.created_at === "string" && !Number.isNaN(Date.parse(value.created_at));
}

export function parseChatServerEvent(value: unknown): ChatServerEvent | null {
  if (!record(value) || typeof value.event !== "string" || !record(value.data)) return null;
  const data = value.data;
  if (value.event === "error") {
    const codes = ["invalid_event", "generation_active", "generation_unavailable", "support_authorization_required", "session_unavailable"];
    return codes.includes(String(data.code)) &&
      (data.session_id === null || typeof data.session_id === "string")
      ? value as unknown as ChatServerEvent : null;
  }
  if (typeof data.session_id !== "string" || !Number.isSafeInteger(data.cursor) || Number(data.cursor) < 0) return null;
  if (value.event === "session_snapshot") {
    if (!Array.isArray(data.messages) || !data.messages.every(chatMessage) ||
        (data.progress !== undefined && data.progress !== null &&
          (!chatProgress(data.progress) || data.progress.session_id !== data.session_id))) return null;
    return value as unknown as ChatServerEvent;
  }
  if (value.event === "generation_progress") {
    return chatProgress(data) ? value as unknown as ChatServerEvent : null;
  }
  if (typeof data.message_id !== "string" || typeof data.generation_id !== "string") return null;
  if (value.event === "coverage_updated") {
    return Number(data.cursor) > 0 && data.message_id && data.generation_id && unitCoverage(data.coverage)
      ? value as unknown as ChatServerEvent : null;
  }
  if (value.event === "token_chunk") {
    if (typeof data.token !== "string" || !Number.isSafeInteger(data.sequence) || Number(data.sequence) < 1 ||
        (data.answer_kind !== undefined && !["insufficient_context", "clarification"].includes(String(data.answer_kind))) ||
        (data.knowledge !== undefined && !knowledgeProjection(data.knowledge)) ||
        (data.publication_kind !== undefined && data.publication_kind !== "authorized_unit") ||
        (data.unit_coverage !== undefined && (data.publication_kind !== "authorized_unit" ||
          data.knowledge !== undefined || data.answer_kind !== undefined || !unitCoverage(data.unit_coverage)))) return null;
    return value as unknown as ChatServerEvent;
  }
  if (value.event === "generation_interrupted" && data.status === "interrupted") return value as unknown as ChatServerEvent;
  if (value.event === "generation_completed") return value as unknown as ChatServerEvent;
  return null;
}

function ordered(messages: ChatMessage[]): ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of messages) byId.set(message.message_id, message);
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence);
}

export function applyChatEvent(state: ChatState, event: ChatServerEvent): ChatState {
  if (event.event === "error") return {
    ...state,
    errorCode: event.data.code,
    redirectMessageId: null,
    generationActive: ["generation_unavailable", "support_authorization_required", "session_unavailable"].includes(event.data.code)
      ? false : state.generationActive,
    progress: null,
    activeGenerationId: ["generation_unavailable", "support_authorization_required", "session_unavailable"].includes(event.data.code)
      ? null : state.activeGenerationId,
    terminalGenerationId: state.activeGenerationId ?? state.terminalGenerationId,
  };
  if (event.event === "generation_progress") {
    if (event.data.generation_id === state.terminalGenerationId ||
        event.data.cursor <= state.progressCursor) return state;
    return {
      ...state,
      generationActive: true,
      errorCode: null,
      progress: event.data,
      activeGenerationId: event.data.generation_id,
      progressCursor: event.data.cursor,
    };
  }
  if (event.data.cursor <= state.cursor && event.event !== "session_snapshot") return state;
  if (event.event === "session_snapshot") {
    if (event.data.cursor < state.cursor) return state;
    const generationActive = event.data.messages.some((message) => message.status === "streaming");
    const progress = generationActive && event.data.progress ? event.data.progress : null;
    return {
      messages: ordered(event.data.messages),
      cursor: event.data.cursor,
      generationActive,
      errorCode: null,
      redirectMessageId: null,
      progress,
      activeGenerationId: progress?.generation_id ?? null,
      terminalGenerationId: generationActive
        ? null
        : event.data.progress?.generation_id ?? state.terminalGenerationId,
      progressCursor: progress?.cursor ?? 0,
    };
  }
  if (event.event === "token_chunk" || event.event === "coverage_updated") {
    const existing = state.messages.find((message) => message.message_id === event.data.message_id);
    if ((existing && (existing.role === "user" || existing.status !== "streaming")) ||
        (!state.generationActive && state.terminalGenerationId === event.data.generation_id)) return state;
    if (event.event === "coverage_updated") {
      if (existing?.knowledge || existing?.answer_kind ||
          (existing?.content && existing.publication_kind !== "authorized_unit")) return state;
    } else if (existing) {
      // A coverage-only assessment reserves a message before its first verified unit.
      const expectedPublication = existing.unit_coverage ? "authorized_unit" : existing.publication_kind;
      if (expectedPublication !== event.data.publication_kind) return state;
    }
    const token = event.event === "token_chunk" ? event.data.token : "";
    const metadata = event.event === "coverage_updated" ? { unit_coverage: event.data.coverage } : {
      ...(event.data.answer_kind ? { answer_kind: event.data.answer_kind } : {}),
      ...(event.data.knowledge ? { knowledge: event.data.knowledge } : {}),
      ...(event.data.publication_kind ? { publication_kind: event.data.publication_kind } : {}),
      ...(event.data.unit_coverage ? { unit_coverage: event.data.unit_coverage } : {}),
    };
    const next = existing
      ? state.messages.map((message) => message.message_id === event.data.message_id
          ? { ...message, ...metadata, content: message.content + token, status: "streaming" as const }
          : message)
      : [...state.messages, {
          message_id: event.data.message_id,
          role: "assistant" as const,
          ...metadata,
          content: token,
          status: "streaming" as const,
          sequence: Math.max(0, ...state.messages.map((message) => message.sequence)) + 1,
          created_at: new Date().toISOString(),
        }];
    return {
      ...state, messages: ordered(next), cursor: event.data.cursor,
      generationActive: true, errorCode: null,
      progress: null,
      activeGenerationId: event.data.generation_id,
      terminalGenerationId: event.data.generation_id,
    };
  }
  const interrupted = event.event === "generation_interrupted";
  const status = interrupted ? "interrupted" : "completed";
  const missedRedirect = !interrupted && state.redirectMessageId !== null;
  let messages = state.messages
    .filter((message) => !missedRedirect || message.message_id !== state.redirectMessageId)
    .map((message): ChatMessage =>
    message.message_id === event.data.message_id ? { ...message, status } : message);
  if (interrupted && !messages.some((message) => message.message_id === event.data.message_id)) {
    const redirected = messages.find((message) => message.message_id === state.redirectMessageId);
    const sequence = redirected?.sequence ?? Math.max(0, ...messages.map((message) => message.sequence)) + 1;
    if (redirected) {
      messages = messages.map((message) =>
        message.message_id === redirected.message_id ? { ...message, sequence: sequence + 1 } : message);
    }
    messages = ordered([...messages, {
      message_id: event.data.message_id,
      role: "assistant",
      content: "",
      status: "interrupted",
      sequence,
      created_at: new Date().toISOString(),
    }]);
  }
  return {
    ...state, messages, cursor: event.data.cursor, errorCode: missedRedirect ? "interrupt_unavailable" : null,
    generationActive: false,
    redirectMessageId: null,
    progress: null,
    activeGenerationId: null,
    terminalGenerationId: event.data.generation_id,
  };
}

export function addOptimisticUserMessage(state: ChatState, text: string, redirect = false): ChatState {
  const sequence = Math.max(0, ...state.messages.map((message) => message.sequence)) + 1;
  const messageId = `local_${crypto.randomUUID()}`;
  return {
    ...state,
    generationActive: true,
    errorCode: null,
    redirectMessageId: redirect ? messageId : null,
    progress: null,
    activeGenerationId: null,
    messages: [...state.messages, {
      message_id: messageId,
      role: "user",
      content: text,
      status: "completed",
      sequence,
      created_at: new Date().toISOString(),
    }],
  };
}

export function chatMessageText(message: ChatMessage): string {
  return message.knowledge === undefined && message.answer_kind === "insufficient_context"
    ? "La base de conocimiento de TrackFlow no contiene información suficiente para responder esta pregunta."
    : message.content;
}

export function chatUnitCoverageText(coverage: UnitCoverage): { label: string; missing: string | null } {
  const label = ({
    complete: "Cobertura completa",
    partial: "Cobertura parcial",
    none: "Sin cobertura",
    indeterminate: "Cobertura indeterminada",
  } satisfies Record<UnitCoverage["status"], string>)[coverage.status];
  const topics = coverage.missing_topics?.map((topic) => coverageTopics[topic]) ?? [];
  return {
    label,
    missing: topics.length
      ? `Temas sin resolver (${new Intl.ListFormat("es").format(topics)}).`
      : null,
  };
}

export function chatProgressText(progress: ChatProgress | null): string | null {
  if (!progress) return null;
  return ({
    processing: "Procesando la consulta…",
    retrieving_context: "Consultando el contexto autorizado…",
    preparing_response: "Preparando una respuesta…",
    validating_response: "Validando la respuesta antes de mostrarla…",
    interrupting: "Interrumpiendo la respuesta…",
  } satisfies Record<ChatProgressStage, string>)[progress.stage];
}

export function chatRetryDelay(attempt: number, random = Math.random()): number {
  const base = Math.min(15_000, 500 * 2 ** Math.max(0, Math.min(attempt, 5)));
  return Math.round(base * (0.75 + Math.max(0, Math.min(random, 1)) * 0.5));
}

export function chatErrorMessage(code: string | null): string | null {
  if (!code) return null;
  return ({
    invalid_event: "El servidor rechazó un evento de chat.",
    generation_active: "Ya hay una respuesta en curso.",
    interrupt_unavailable: "La respuesta ya había terminado. Puedes enviar la nueva consulta.",
    generation_unavailable: "La generación de soporte no está disponible temporalmente.",
    support_authorization_required: "La autorización OIDC de soporte expiró.",
    session_unavailable: "La sesión de chat no está disponible.",
  } as Record<string, string>)[code] ?? "El chat devolvió un error controlado.";
}
