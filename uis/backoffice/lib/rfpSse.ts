interface SseFrame {
  id: string | undefined;
  event: string | undefined;
  data: string;
}

export class InvalidRfpSseFrame extends Error {}

const MAX_FRAME_UNITS = 16_384;
const MAX_CURSOR = "9223372036854775807";

export function compareRfpCursor(first: string, second: string): number {
  return first.length - second.length || (first < second ? -1 : first > second ? 1 : 0);
}

export function isRfpCursor(value: unknown): value is string {
  return typeof value === "string" && /^(0|[1-9][0-9]{0,18})$/.test(value)
    && compareRfpCursor(value, MAX_CURSOR) <= 0;
}

export function rfpRetryDelay(attempt: number, random = Math.random()): number {
  const ceiling = Math.min(15_000, 750 * (2 ** Math.min(attempt, 5)));
  return Math.round(ceiling * (0.5 + random * 0.5));
}

/** Read complete frames without relying on the network's chunk boundaries. */
export async function* readRfpSse(
  stream: ReadableStream<Uint8Array>, onChunk?: () => void,
): AsyncGenerator<SseFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let line = "";
  let previousCR = false;
  let size = 0;
  let id: string | undefined;
  let event: string | undefined;
  let data: string[] = [];
  try {
    while (true) {
      const result = await reader.read();
      if (result.value?.length) onChunk?.();
      let chunk: string;
      try {
        chunk = decoder.decode(result.value, { stream: !result.done });
      } catch {
        throw new InvalidRfpSseFrame("El stream contiene texto inválido.");
      }
      for (const char of chunk) {
        if (previousCR && char === "\n") {
          previousCR = false;
          continue;
        }
        previousCR = char === "\r";
        size += char.length;
        if (size > MAX_FRAME_UNITS) throw new InvalidRfpSseFrame("Evento SSE demasiado grande.");
        if (char !== "\n" && char !== "\r") {
          line += char;
          continue;
        }
        if (line === "") {
          if (data.length) yield { id, event, data: data.join("\n") };
          id = undefined;
          event = undefined;
          data = [];
          size = 0;
          continue;
        }
        if (!line.startsWith(":")) {
          const split = line.indexOf(":");
          const field = split === -1 ? line : line.slice(0, split);
          let value = split === -1 ? "" : line.slice(split + 1);
          if (value.startsWith(" ")) value = value.slice(1);
          if (field === "data") data.push(value);
          if (field === "event") event = value;
          if (field === "id" && !value.includes("\0")) id = value;
        }
        line = "";
      }
      if (result.done) return;
    }
  } finally {
    void reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
