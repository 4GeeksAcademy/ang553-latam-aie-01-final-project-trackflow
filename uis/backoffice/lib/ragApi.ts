import { authFetch } from "@/lib/authFetch";
import type { KnowledgeQueryResponse } from "@/types/rag";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

export class KnowledgeApiError extends Error {
  constructor(message: string, public readonly statusCode?: number) {
    super(message);
    this.name = "KnowledgeApiError";
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) {
      return body.detail;
    }
  } catch {
    // Fall back to a safe status-based message below.
  }
  return `Knowledge request failed with status ${response.status}`;
}

function isKnowledgeQueryResponse(value: unknown): value is KnowledgeQueryResponse {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.answer !== "string" ||
    typeof candidate.insufficient_context !== "boolean" ||
    !Array.isArray(candidate.sources)
  ) {
    return false;
  }
  return candidate.sources.every(
    (source) =>
      typeof source === "object" &&
      source !== null &&
      typeof (source as Record<string, unknown>).source_document === "string" &&
      typeof (source as Record<string, unknown>).section === "string" &&
      ((source as Record<string, unknown>).retrieval_kind === "primary" ||
        (source as Record<string, unknown>).retrieval_kind === "complementary"),
  );
}

export async function askKnowledgeBase(
  question: string,
): Promise<KnowledgeQueryResponse> {
  let response: Response;
  try {
    response = await authFetch(
      `${BASE_URL}/knowledge/query`,
      {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ question }),
      },
      { pathTemplate: "/knowledge/query" },
    );
  } catch {
    throw new KnowledgeApiError(
      "No se pudo conectar con la base de conocimiento.",
    );
  }

  if (!response.ok) {
    if (response.status === 503) {
      throw new KnowledgeApiError(
        "El proveedor de conocimiento no está disponible temporalmente. Inténtalo de nuevo más tarde.",
        response.status,
      );
    }
    throw new KnowledgeApiError(await errorDetail(response), response.status);
  }

  try {
    const body: unknown = await response.json();
    if (!isKnowledgeQueryResponse(body)) {
      throw new Error("invalid response contract");
    }
    return body;
  } catch {
    throw new KnowledgeApiError(
      "La base de conocimiento devolvió una respuesta inválida.",
    );
  }
}
