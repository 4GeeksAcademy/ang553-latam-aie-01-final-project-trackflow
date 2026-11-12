import type { SupportQueryResponse } from "@/types/support";
import {
  clearSupportAccessToken,
  getSupportAccessToken,
  SupportAuthorizationError,
  SupportAuthorizationRedirect,
} from "@/lib/supportOidc";
import { telemetryService } from "@/lib/telemetry";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

export class SupportApiError extends Error {
  constructor(message: string, public readonly statusCode?: number) {
    super(message);
    this.name = "SupportApiError";
  }
}

function isSupportResponse(value: unknown): value is SupportQueryResponse {
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
    (item.knowledge === null || typeof item.knowledge === "object")
  );
}

export async function askSupport(question: string): Promise<SupportQueryResponse> {
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
      body: JSON.stringify({ question }),
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
    const messages: Record<number, string> = {
      401: "La sesión OIDC de soporte expiró. Vuelve a autorizar el asistente.",
      403: "Tu rol no tiene acceso al asistente de soporte.",
      503: "El asistente de soporte no está disponible temporalmente.",
    };
    throw new SupportApiError(
      messages[response.status] ?? `La consulta falló con estado ${response.status}.`,
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
