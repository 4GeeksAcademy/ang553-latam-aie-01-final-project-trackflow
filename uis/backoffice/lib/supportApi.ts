import { authFetch } from "@/lib/authFetch";
import type { SupportQueryResponse } from "@/types/support";

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
  try {
    response = await authFetch(
      `${BASE_URL}/support/query`,
      {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      },
      { pathTemplate: "/support/query" },
    );
  } catch {
    throw new SupportApiError("No se pudo conectar con el asistente de soporte.");
  }

  if (!response.ok) {
    const messages: Record<number, string> = {
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
