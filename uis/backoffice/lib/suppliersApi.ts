import type {
  Supplier,
  SupplierCreate,
  SupplierFilters,
  SupplierRateUpdate,
  SupplierStatusUpdate,
} from "@/types/suppliers";

const BASE_URL: string = process.env.NEXT_PUBLIC_API_URL ?? "";

export class ApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

function buildSuppliersPath(filters?: SupplierFilters): string {
  const params = new URLSearchParams();

  if (filters?.country) {
    params.set("country", filters.country);
  }

  if (filters?.category) {
    params.set("category", filters.category);
  }

  const query = params.toString();
  return query ? `/api/suppliers?${query}` : "/api/suppliers";
}

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

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("Could not reach the suppliers API. Make sure backend is running.");
  }

  if (!response.ok) {
    const message = await getErrorMessage(response);
    throw new ApiError(message, response.status);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("Suppliers API returned an invalid JSON response.", response.status);
  }
}

export async function getSuppliers(filters?: SupplierFilters): Promise<Supplier[]> {
  return requestJson<Supplier[]>(buildSuppliersPath(filters), {
    method: "GET",
    cache: "no-store",
  });
}

export async function createSupplier(payload: SupplierCreate): Promise<Supplier> {
  return requestJson<Supplier>("/api/suppliers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateSupplierRate(id: number, payload: SupplierRateUpdate): Promise<Supplier> {
  return requestJson<Supplier>(`/api/suppliers/${id}/rate`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function updateSupplierStatus(id: number, payload: SupplierStatusUpdate): Promise<Supplier> {
  return requestJson<Supplier>(`/api/suppliers/${id}/status`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}
