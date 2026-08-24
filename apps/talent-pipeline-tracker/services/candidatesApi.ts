import { authFetch } from "@/lib/authFetch";
import type {
  Candidate,
  CandidateCreatePayload,
  CandidateNote,
  CandidateNoteCreatePayload,
  CandidatePatchPayload,
  CandidateUpdatePayload,
} from "@/types/candidate";

const API_URL = process.env.NEXT_PUBLIC_API_URL;

if (!API_URL) {
  throw new Error("NEXT_PUBLIC_API_URL is not configured.");
}

type CandidatesListResponse =
  | Candidate[]
  | {
      data: Candidate[];
    };

type CandidateNotesResponse = {
  data: CandidateNote[];
};

export class ApiError extends Error {
  statusCode?: number;

  constructor(message: string, statusCode?: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

async function authFetchWithError(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await authFetch(url, init);
  } catch {
    throw new ApiError(
      "We couldn't connect to the service. Please try again.",
    );
  }
}

async function handleResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") ?? "";
  let jsonData: unknown = null;

  if (contentType.includes("application/json")) {
    try {
      jsonData = await response.json();
    } catch {
      // Parsing failed — handled below
    }
  }

  if (!response.ok) {
    let safeMessage = "An unexpected error occurred.";

    if (response.status === 404) {
      safeMessage = "The requested resource was not found.";
    } else if (response.status === 409) {
      safeMessage = "The request conflicts with the current state.";
    } else if (response.status === 401 || response.status === 403) {
      safeMessage = "You are not authorized to perform this action.";
    } else if (response.status >= 500) {
      safeMessage = "The server encountered an internal error. Please try again later.";
    }

    throw new ApiError(safeMessage, response.status);
  }

  // Successful response — only accept valid JSON, reject everything else
  if (jsonData === null) {
    throw new ApiError(
      "The server returned an unexpected response. Please try again.",
      response.status,
    );
  }

  return jsonData as T;
}

function buildUrl(path: string): string {
  return `${API_URL}${path}`;
}

export async function getCandidates(): Promise<Candidate[]> {
  const response = await authFetchWithError(buildUrl("/records"), {
    method: "GET",
    cache: "no-store",
  });

  const payload = await handleResponse<CandidatesListResponse>(response);

  if (Array.isArray(payload)) {
    return payload;
  }

  if (payload && Array.isArray(payload.data)) {
    return payload.data;
  }

  throw new ApiError(
    "The server returned data in an unexpected format. Please try again.",
  );
}

export async function getCandidateById(id: string): Promise<Candidate> {
  const response = await authFetchWithError(buildUrl(`/records/${id}`), {
    method: "GET",
    cache: "no-store",
  });

  return handleResponse<Candidate>(response);
}

export async function createCandidate(
  payload: CandidateCreatePayload,
): Promise<Candidate> {
  const response = await authFetchWithError(buildUrl("/records"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return handleResponse<Candidate>(response);
}

export async function updateCandidate(
  id: string,
  payload: CandidateUpdatePayload,
): Promise<Candidate> {
  const response = await authFetchWithError(buildUrl(`/records/${id}`), {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return handleResponse<Candidate>(response);
}

export async function patchCandidate(
  id: string,
  payload: CandidatePatchPayload,
): Promise<Candidate> {
  const response = await authFetchWithError(buildUrl(`/records/${id}`), {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return handleResponse<Candidate>(response);
}

export async function getCandidateNotes(id: string): Promise<CandidateNote[]> {
  const response = await authFetchWithError(buildUrl(`/records/${id}/notes`), {
    method: "GET",
    cache: "no-store",
  });

  const payload = await handleResponse<CandidateNotesResponse>(response);
  return payload.data;
}

export async function createCandidateNote(
  id: string,
  payload: CandidateNoteCreatePayload,
): Promise<CandidateNote> {
  const response = await authFetchWithError(buildUrl(`/records/${id}/notes`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return handleResponse<CandidateNote>(response);
}

export async function deleteCandidateNote(
  id: string,
  noteId: string,
): Promise<void> {
  const response = await authFetchWithError(
    buildUrl(`/records/${id}/notes/${noteId}`),
    {
      method: "DELETE",
    },
  );

  if (!response.ok) {
    await handleResponse<never>(response);
  }
}
