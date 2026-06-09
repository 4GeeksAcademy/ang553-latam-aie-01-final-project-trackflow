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

async function handleResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") ?? "";
  let jsonData: unknown = null;
  let textData = "";

  if (contentType.includes("application/json")) {
    try {
      jsonData = await response.json();
    } catch {
      jsonData = null;
    }
  } else {
    try {
      textData = await response.text();
    } catch {
      textData = "";
    }
  }

  if (!response.ok) {
    let errorMessage = response.statusText || "Request failed";

    if (jsonData && typeof jsonData === "object") {
      const errorObj = jsonData as Record<string, unknown>;
      if (typeof errorObj.detail === "string") {
        errorMessage = errorObj.detail;
      } else if (typeof errorObj.message === "string") {
        errorMessage = errorObj.message;
      } else if (typeof errorObj.error === "string") {
        errorMessage = errorObj.error;
      }
    } else if (textData.trim()) {
      errorMessage = textData.trim();
    }

    throw new Error(
      `API request failed (${response.status}): ${errorMessage}`,
    );
  }

  return jsonData as T;
}

function buildUrl(path: string): string {
  return `${API_URL}${path}`;
}

export async function getCandidates(): Promise<Candidate[]> {
  const response = await fetch(buildUrl("/records"), {
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

  throw new Error("Unexpected response format for GET /records");
}

export async function getCandidateById(id: string): Promise<Candidate> {
  const response = await fetch(buildUrl(`/records/${id}`), {
    method: "GET",
    cache: "no-store",
  });

  return handleResponse<Candidate>(response);
}

export async function createCandidate(
  payload: CandidateCreatePayload,
): Promise<Candidate> {
  const response = await fetch(buildUrl("/records"), {
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
  const response = await fetch(buildUrl(`/records/${id}`), {
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
  const response = await fetch(buildUrl(`/records/${id}`), {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return handleResponse<Candidate>(response);
}

export async function getCandidateNotes(id: string): Promise<CandidateNote[]> {
  const response = await fetch(buildUrl(`/records/${id}/notes`), {
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
  const response = await fetch(buildUrl(`/records/${id}/notes`), {
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
  const response = await fetch(buildUrl(`/records/${id}/notes/${noteId}`), {
    method: "DELETE",
  });

  if (!response.ok) {
    await handleResponse<never>(response);
  }
}
