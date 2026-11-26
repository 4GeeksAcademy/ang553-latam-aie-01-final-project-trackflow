import type {
  SupportMemoryDecisionRequest,
  SupportMemoryDecisionResponse,
  SupportPendingMemoryResponse,
  SupportQueryOptions,
  SupportQueryResponse,
} from "@/types/support";
import {
  clearSupportAccessToken,
  getCurrentSupportAccessToken,
  getSupportAccessToken,
  SupportAuthorizationError,
  SupportAuthorizationRedirect,
} from "@/lib/supportOidc";
import { telemetryService } from "@/lib/telemetry";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export class SupportApiError extends Error {
  constructor(message: string, public readonly statusCode?: number) {
    super(message);
    this.name = "SupportApiError";
  }
}

function isMemoryProposal(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "string" && UUID_PATTERN.test(item.id) &&
    typeof item.conversation_id === "string" && UUID_PATTERN.test(item.conversation_id) &&
    ["client", "global"].includes(String(item.scope)) &&
    (item.client_id === null || typeof item.client_id === "string") &&
    ["carrier_coverage", "recurring_incident", "b2b_report_preference"].includes(String(item.category)) &&
    typeof item.content === "object" && item.content !== null && !Array.isArray(item.content) &&
    item.verification_status === "user_reported" &&
    typeof item.expires_at === "string"
  );
}

export function isSupportResponse(value: unknown): value is SupportQueryResponse {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.answer === "string" &&
    ["clarification", "knowledge", "ticket", "inventory", "mixed"].includes(
      String(item.route),
    ) &&
    typeof item.partial === "boolean" &&
    typeof item.needs_clarification === "boolean" &&
    (item.ticket === null || typeof item.ticket === "object") &&
    (item.inventory === null || typeof item.inventory === "object") &&
    (item.knowledge === null || typeof item.knowledge === "object") &&
    (item.conversation_id === undefined || item.conversation_id === null ||
      (typeof item.conversation_id === "string" && UUID_PATTERN.test(item.conversation_id))) &&
    (item.memory_proposal === undefined || item.memory_proposal === null ||
      isMemoryProposal(item.memory_proposal)) &&
    (item.memory_evaluation === undefined || item.memory_evaluation === null ||
      (typeof item.memory_evaluation === "object" &&
        typeof (item.memory_evaluation as Record<string, unknown>).status === "string" &&
        typeof (item.memory_evaluation as Record<string, unknown>).evaluator_called === "boolean")) &&
    (item.memory_read_status === undefined || item.memory_read_status === null ||
      typeof item.memory_read_status === "string") &&
    (item.memory_consent === undefined || item.memory_consent === null ||
      (typeof item.memory_consent === "object" &&
        ["approve", "reject", "edit", "ambiguous", "topic_change"].includes(
          String((item.memory_consent as Record<string, unknown>).action),
        ) &&
        ["approved", "rejected", "replacement_pending", "discarded", "expired"].includes(
          String((item.memory_consent as Record<string, unknown>).disposition),
        ) &&
        typeof (item.memory_consent as Record<string, unknown>).continue_support_question === "boolean"))
  );
}

function isMemoryDecisionResponse(
  value: unknown,
): value is SupportMemoryDecisionResponse {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.conversation_id === "string" && UUID_PATTERN.test(item.conversation_id) &&
    typeof item.disposition === "string" &&
    typeof item.memory_active === "boolean" &&
    typeof item.continue_support_question === "boolean" &&
    (item.proposal === null || isMemoryProposal(item.proposal))
  );
}

function isPendingMemoryResponse(
  value: unknown,
): value is SupportPendingMemoryResponse {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.conversation_id === "string" && UUID_PATTERN.test(item.conversation_id) &&
    (item.proposal === null || isMemoryProposal(item.proposal))
  );
}

function messageForStatus(status: number): string {
  const messages: Record<number, string> = {
    401: "La sesión OIDC de soporte expiró. Vuelve a autorizar el asistente.",
    403: "Tu rol no tiene acceso al asistente de soporte.",
    404: "La conversación o propuesta ya no está disponible.",
    409: "La propuesta ya fue resuelta o cambió. Actualiza la consulta.",
    422: "La memoria propuesta no cumple la política de datos.",
    503: "El asistente de soporte no está disponible temporalmente.",
  };
  return messages[status] ?? `La solicitud falló con estado ${status}.`;
}

export async function askSupport(
  question: string,
  options: SupportQueryOptions = {},
): Promise<SupportQueryResponse> {
  let response: Response;
  const startedAt =
    typeof performance === "undefined" ? Date.now() : performance.now();
  try {
    const token = await getSupportAccessToken();
    const requestId = globalThis.crypto?.randomUUID?.();
    const headers = new Headers({
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    });
    if (requestId) headers.set("X-Request-ID", requestId);
    response = await fetch(`${BASE_URL}/support/query`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        question,
        ...(options.conversationId
          ? { conversation_id: options.conversationId }
          : {}),
        ...(options.clientId ? { client_id: options.clientId } : {}),
        ...(options.memoryScope ? { memory_scope: options.memoryScope } : {}),
      }),
    });
  } catch (error) {
    if (error instanceof SupportAuthorizationRedirect) throw error;
    if (error instanceof SupportAuthorizationError) {
      throw new SupportApiError(error.message);
    }
    throw new SupportApiError("No se pudo conectar con el asistente de soporte.");
  }

  if (response.status === 401) clearSupportAccessToken();

  if (response.status >= 500 && response.status <= 599) {
    const finishedAt =
      typeof performance === "undefined" ? Date.now() : performance.now();
    telemetryService.withRequestId(response.headers.get("X-Request-ID"), () => {
      telemetryService.track("api_request_failed", {
        method: "POST",
        path_template: "/support/query",
        status_code: response.status,
        duration_ms: Math.max(0, finishedAt - startedAt),
      });
    });
  }

  if (!response.ok) {
    throw new SupportApiError(
      messageForStatus(response.status),
      response.status,
    );
  }

  try {
    const body: unknown = await response.json();
    if (!isSupportResponse(body)) throw new Error("invalid response");
    return body;
  } catch {
    throw new SupportApiError("El asistente devolvió una respuesta inválida.");
  }
}

interface MemoryRequestOptions {
  signal?: AbortSignal;
  currentAuthorizationOnly?: boolean;
}

async function memoryAccessToken(options: MemoryRequestOptions): Promise<string> {
  options.signal?.throwIfAborted();
  const token = options.currentAuthorizationOnly
    ? getCurrentSupportAccessToken()
    : await getSupportAccessToken();
  options.signal?.throwIfAborted();
  if (!token) throw new SupportApiError(messageForStatus(401), 401);
  return token;
}

export async function decideSupportMemory(
  decision: SupportMemoryDecisionRequest,
  options: MemoryRequestOptions = {},
): Promise<SupportMemoryDecisionResponse> {
  let response: Response;
  let requestToken: string;
  try {
    requestToken = await memoryAccessToken(options);
    options.signal?.throwIfAborted();
    response = await fetch(`${BASE_URL}/support/memory/decision`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        Authorization: `Bearer ${requestToken}`,
      },
      body: JSON.stringify(decision),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof SupportAuthorizationRedirect || error instanceof SupportApiError) throw error;
    if (error instanceof SupportAuthorizationError) {
      throw new SupportApiError(error.message);
    }
    throw new SupportApiError("No se pudo registrar la decisión de memoria.");
  }
  options.signal?.throwIfAborted();
  if (response.status === 401) clearSupportAccessToken(requestToken);
  if (!response.ok) {
    throw new SupportApiError(messageForStatus(response.status), response.status);
  }
  try {
    const body: unknown = await response.json();
    if (!isMemoryDecisionResponse(body)) throw new Error("invalid response");
    return body;
  } catch {
    throw new SupportApiError("El asistente devolvió una decisión inválida.");
  }
}

export async function getPendingSupportMemory(
  identifier: { conversationId: string } | { proposalId: string },
  options: MemoryRequestOptions = {},
): Promise<SupportPendingMemoryResponse> {
  let response: Response;
  let requestToken: string;
  try {
    requestToken = await memoryAccessToken(options);
    options.signal?.throwIfAborted();
    const query = new URLSearchParams(
      "conversationId" in identifier
        ? { conversation_id: identifier.conversationId }
        : { proposal_id: identifier.proposalId },
    );
    response = await fetch(`${BASE_URL}/support/memory/pending?${query.toString()}`, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${requestToken}`,
      },
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof SupportAuthorizationRedirect || error instanceof SupportApiError) throw error;
    if (error instanceof SupportAuthorizationError) {
      throw new SupportApiError(error.message);
    }
    throw new SupportApiError("No se pudo recuperar la propuesta pendiente.");
  }
  options.signal?.throwIfAborted();
  if (response.status === 401) clearSupportAccessToken(requestToken);
  if (!response.ok) {
    throw new SupportApiError(messageForStatus(response.status), response.status);
  }
  try {
    const body: unknown = await response.json();
    if (!isPendingMemoryResponse(body)) throw new Error("invalid response");
    if (body.proposal !== null && body.proposal.conversation_id !== body.conversation_id) {
      throw new Error("mismatched recovery response");
    }
    if (
      "conversationId" in identifier &&
      body.conversation_id !== identifier.conversationId
    ) throw new Error("mismatched recovery response");
    if (
      "proposalId" in identifier &&
      body.proposal !== null &&
      body.proposal.id !== identifier.proposalId
    ) throw new Error("mismatched recovery response");
    return body;
  } catch {
    throw new SupportApiError("El asistente devolvió una propuesta inválida.");
  }
}
