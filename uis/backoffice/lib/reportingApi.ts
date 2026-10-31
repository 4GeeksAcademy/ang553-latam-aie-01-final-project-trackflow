import { authFetch } from "@/lib/authFetch";
import type { WeeklyPerformanceReport } from "@/types/reporting";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

export class ReportingApiError extends Error {
  constructor(message: string, public readonly statusCode?: number) {
    super(message);
    this.name = "ReportingApiError";
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // The status-specific UI remains useful when the backend body is unavailable.
  }
  return `Reporting request failed with status ${response.status}`;
}

export async function getWeeklyPerformanceReport(): Promise<WeeklyPerformanceReport> {
  let response: Response;
  try {
    response = await authFetch(
      `${BASE_URL}/reporting/weekly-warehouse-client-performance`,
      { method: "GET", headers: { Accept: "application/json" }, cache: "no-store" },
      { pathTemplate: "/reporting/weekly-warehouse-client-performance" },
    );
  } catch {
    throw new ReportingApiError("No se pudo conectar con el servicio de reporting.");
  }
  if (!response.ok) {
    throw new ReportingApiError(await errorDetail(response), response.status);
  }
  try {
    return (await response.json()) as WeeklyPerformanceReport;
  } catch {
    throw new ReportingApiError("El servicio de reporting devolvió una respuesta inválida.");
  }
}
