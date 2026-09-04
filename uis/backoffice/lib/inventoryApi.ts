/**
 * Inventory API client for TrackFlow Backoffice.
 *
 * Provides typed functions to interact with the TrackFlow inventory
 * backend (``/inventory/products``, ``/inventory/orders/*``).
 *
 * Uses ``NEXT_PUBLIC_INVENTORY_API_URL`` when provided; falls back to
 * an empty string (same-origin) if the variable is not set.
 *
 * Every function uses ``authFetch`` so the JWT is automatically attached
 * as a ``Bearer`` token and ``401`` responses trigger a redirect to
 * ``/login``.
 */

import { authFetch } from "@/lib/authFetch";
import type {
  SKUResponse,
  StockEntryCreate,
  StockEntryResponse,
  StockExitCreate,
  StockExitResponse,
  InventoryOrderResponse,
} from "@/types/inventory";

const BASE_URL: string = process.env.NEXT_PUBLIC_INVENTORY_API_URL ?? "";

/**
 * Error thrown by inventory API functions.
 * Carries a human-readable message and an optional HTTP status code.
 */
export class ApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

/* ── Error extraction ────────────────────────────────────────────────────── */

/**
 * Extract a human-readable error message from a failed HTTP response.
 *
 * Handles the most common FastAPI error shapes:
 * - ``{ "detail": "string" }`` — simple string detail.
 * - ``{ "detail": [{ "msg": "..." }] }`` — Pydantic validation array.
 * - ``{ "message": "..." }`` — generic message field.
 *
 * Falls back to ``"Request failed with status <code>"`` when the body
 * cannot be parsed or does not contain a recognised field.
 */
async function getErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      detail?: string | Array<{ msg?: string }>;
      message?: string;
    };
    if (typeof body.detail === "string" && body.detail.trim().length > 0) {
      return body.detail;
    }

    if (Array.isArray(body.detail)) {
      const firstWithMessage = body.detail.find(
        (item) => typeof item?.msg === "string" && item.msg.trim().length > 0,
      );
      if (firstWithMessage?.msg) {
        return firstWithMessage.msg;
      }
    }

    if (typeof body.message === "string" && body.message.trim().length > 0) {
      return body.message;
    }
  } catch {
    // Ignore parse errors and fallback to a default message.
  }

  return `Request failed with status ${response.status}`;
}

/* ── Internal request helper ─────────────────────────────────────────────── */

/**
 * Perform an authenticated JSON request against the inventory API.
 *
 * - Builds the full URL from ``BASE_URL`` and ``path``.
 * - Attaches the JWT via ``authFetch``.
 * - Sets ``Content-Type: application/json`` by default.
 * - Serialises ``body`` when present.
 * - Parses the response and throws a readable ``ApiError`` on failure.
 */
async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await authFetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(
      "Could not reach the inventory API. Make sure the backend is running.",
    );
  }

  if (!response.ok) {
    const message = await getErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(
      "Inventory API returned an invalid JSON response.",
      response.status,
    );
  }
}

/* ── Products ────────────────────────────────────────────────────────────── */

/**
 * Fetch the full list of SKUs from the inventory backend.
 *
 * Calls ``GET /inventory/products``.
 *
 * @returns A flat array of ``SKUResponse`` objects.
 * @throws {@link ApiError} on failure.
 */
export async function getInventoryProducts(): Promise<SKUResponse[]> {
  return requestJson<SKUResponse[]>("/inventory/products", {
    method: "GET",
    cache: "no-store",
  });
}

/**
 * Fetch a single SKU by its ID.
 *
 * Calls ``GET /inventory/products/{id}``.
 *
 * @param id - The numeric ID of the SKU.
 * @returns The matching ``SKUResponse``.
 * @throws {@link ApiError} with ``statusCode`` ``404`` when the SKU
 *         is not found.
 */
export async function getInventoryProduct(id: number): Promise<SKUResponse> {
  return requestJson<SKUResponse>(`/inventory/products/${id}`, {
    method: "GET",
  });
}

/* ── Stock entries (inbound) ─────────────────────────────────────────────── */

/**
 * Record a stock-in (inbound) movement.
 *
 * Calls ``POST /inventory/orders/inbound``.
 *
 * @param data - Inbound order payload (sku_id, quantity, reference,
 *               warehouse).
 * @returns The created ``StockEntryResponse``.
 * @throws {@link ApiError} on validation or server errors.
 */
export async function createStockEntry(
  data: StockEntryCreate,
): Promise<StockEntryResponse> {
  return requestJson<StockEntryResponse>("/inventory/orders/inbound", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/* ── Stock exits (outbound) ──────────────────────────────────────────────── */

/**
 * Record a stock-out (outbound) movement.
 *
 * Calls ``POST /inventory/orders/outbound``.
 *
 * @param data - Outbound order payload (sku_id, quantity, exit_type,
 *               tracking_number, warehouse).
 * @returns The created ``StockExitResponse``.
 * @throws {@link ApiError} on validation errors, insufficient stock
 *         (400), or server errors.
 */
export async function createStockExit(
  data: StockExitCreate,
): Promise<StockExitResponse> {
  return requestJson<StockExitResponse>("/inventory/orders/outbound", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/* ── Orders (history) ───────────────────────────────────────────────────── */

/**
 * Fetch the full list of inventory movements (orders).
 *
 * Calls ``GET /inventory/orders``.
 *
 * Returns a flat array of combined inbound and outbound movements,
 * each annotated with ``movement_type`` and a ``sku`` summary.
 *
 * @returns A flat array of ``InventoryOrderResponse`` objects.
 * @throws {@link ApiError} on failure.
 */
export async function getInventoryOrders(): Promise<InventoryOrderResponse[]> {
  return requestJson<InventoryOrderResponse[]>("/inventory/orders", {
    method: "GET",
    cache: "no-store",
  });
}