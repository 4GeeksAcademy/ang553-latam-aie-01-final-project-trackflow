/**
 * Central, in-memory telemetry service for the backoffice.
 *
 * Captures events locally and delivers them in bounded batches with
 * best-effort retries and page-exit delivery through sendBeacon.
 */

export const TELEMETRY_SCHEMA_VERSION = "1.0" as const;
export const MAX_BATCH_SIZE = 20;
export const FLUSH_INTERVAL_MS = 10_000;
export const MAX_SEND_ATTEMPTS = 3;
export const RETRY_BASE_DELAY_MS = 1_000;

const TELEMETRY_ENDPOINT =
  process.env.NEXT_PUBLIC_TELEMETRY_ENDPOINT ??
  "http://localhost:8000/telemetry/events";

export type TelemetryEventType =
  | "inbound_order_created"
  | "outbound_order_created"
  | "stock_threshold_triggered"
  | "direct_stock_edit_rejected"
  | "inventory_discrepancy_detected"
  | "auth_login_succeeded"
  | "auth_login_failed"
  | "auth_password_reset_requested"
  | "auth_password_reset_completed"
  | "inventory_validation_failed"
  | "inventory_stock_insufficient"
  | "api_request_slow"
  | "api_request_failed"
  | "backoffice_section_entered"
  | "inventory_workflow_started"
  | "inventory_workflow_abandoned"
  | "inventory_product_created";

export interface TelemetryEventEnvelope {
  eventId: string;
  timestamp: string;
  sessionId: string | null;
  userId: string | null;
  event_type: TelemetryEventType;
  schemaVersion: typeof TELEMETRY_SCHEMA_VERSION;
  requestId: string | null;
  properties: Record<string, unknown>;
}

function createUuidV4(): string {
  // randomUUID is the most direct way to guarantee the UUID v4 version and
  // variant bits in browsers and in the supported server runtimes.
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  // Keep capture non-critical in older runtimes that do not expose
  // crypto.randomUUID. The fallback remains cryptographic.
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    throw new Error("No cryptographic UUID generator is available");
  }

  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hexadecimal = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");

  return `${hexadecimal.slice(0, 8)}-${hexadecimal.slice(
    8,
    12,
  )}-${hexadecimal.slice(12, 16)}-${hexadecimal.slice(
    16,
    20,
  )}-${hexadecimal.slice(20)}`;
}

export class TelemetryService {
  private static instance: TelemetryService | undefined;

  private readonly queue: TelemetryEventEnvelope[] = [];

  private flushTimer: ReturnType<typeof setTimeout> | null = null;

  private flushInProgress: Promise<void> | null = null;

  private activeBatch: TelemetryEventEnvelope[] | null = null;

  private activeBatchBeaconQueued = false;

  private lifecycleListenersRegistered = false;

  private sessionId: string | null = null;

  private userId: string | null = null;

  private currentRequestId: string | null = null;

  private constructor() {}

  static getInstance(): TelemetryService {
    TelemetryService.instance ??= new TelemetryService();
    return TelemetryService.instance;
  }

  /** Update the identity used by subsequently captured events. */
  setIdentity(userId: string | null, sessionId: string | null): void {
    this.userId = userId;
    this.sessionId = sessionId;
  }

  setUserId(userId: string | null): void {
    this.userId = userId;
  }

  setSessionId(sessionId: string | null): void {
    this.sessionId = sessionId;
  }

  /**
   * Capture request-scoped events in a synchronous callback only. Any track()
   * call that should use this request ID must run before the callback returns.
   */
  withRequestId(requestId: string | null, callback: () => void): void {
    const previousRequestId = this.currentRequestId;
    const normalizedRequestId =
      requestId !== null && requestId.trim() !== "" ? requestId : null;

    this.currentRequestId = normalizedRequestId;

    try {
      callback();
    } finally {
      this.currentRequestId = previousRequestId;
    }
  }

  /** Capture an event immediately and append it to the local queue. */
  track(
    eventType: TelemetryEventType,
    properties: Record<string, unknown>,
  ): void {
    try {
      // These values must be obtained at the point track() is called, rather
      // than when a future transport or flush operation runs.
      const timestamp = new Date().toISOString();
      const event: TelemetryEventEnvelope = {
        eventId: createUuidV4(),
        timestamp,
        sessionId: this.sessionId,
        userId: this.userId,
        event_type: eventType,
        schemaVersion: TELEMETRY_SCHEMA_VERSION,
        requestId: this.currentRequestId,
        properties: { ...properties },
      };

      this.queue.push(event);
      this.registerLifecycleListeners();
      this.scheduleFlush();

      if (this.queue.length >= MAX_BATCH_SIZE) {
        this.startFlush();
      }
    } catch {
      // Telemetry is non-critical. Do not let capture failures affect the
      // caller, and do not enqueue a partial event.
    }
  }

  private scheduleFlush(): void {
    if (
      this.flushInProgress !== null ||
      this.flushTimer !== null ||
      this.queue.length === 0
    ) {
      return;
    }

    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      this.startFlush();
    }, FLUSH_INTERVAL_MS);
  }

  private startFlush(): void {
    if (this.flushInProgress !== null || this.queue.length === 0) {
      return;
    }

    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }

    const batch = this.queue.splice(0, MAX_BATCH_SIZE);
    this.activeBatch = batch;
    this.activeBatchBeaconQueued = false;
    this.flushInProgress = this.flushBatch(batch).then(() => {
      this.flushInProgress = null;
      this.activeBatch = null;
      this.activeBatchBeaconQueued = false;

      if (this.queue.length >= MAX_BATCH_SIZE) {
        this.startFlush();
      } else if (this.queue.length > 0) {
        this.scheduleFlush();
      }
    });
  }

  private async flushBatch(
    batch: TelemetryEventEnvelope[],
  ): Promise<void> {
    for (let attempt = 1; attempt <= MAX_SEND_ATTEMPTS; attempt += 1) {
      if (this.activeBatchBeaconQueued) {
        return;
      }

      try {
        const response = await fetch(TELEMETRY_ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ events: batch }),
        });

        if (response.status >= 200 && response.status < 300) {
          return;
        }
      } catch {
        // Transport failures consume an attempt and are retried below.
      }

      if (this.activeBatchBeaconQueued) {
        return;
      }

      if (attempt < MAX_SEND_ATTEMPTS) {
        await this.delay(RETRY_BASE_DELAY_MS * 2 ** (attempt - 1));

        if (this.activeBatchBeaconQueued) {
          return;
        }
      }
    }
  }

  private delay(milliseconds: number): Promise<void> {
    return new Promise((resolve) => {
      setTimeout(resolve, milliseconds);
    });
  }

  private registerLifecycleListeners(): void {
    if (
      this.lifecycleListenersRegistered ||
      typeof window === "undefined" ||
      typeof document === "undefined"
    ) {
      return;
    }

    document.addEventListener("visibilitychange", this.handleVisibilityChange);
    window.addEventListener("pagehide", this.handlePageHide);
    this.lifecycleListenersRegistered = true;
  }

  private readonly handleVisibilityChange = (): void => {
    try {
      if (document.visibilityState === "hidden") {
        this.flushWithBeacon();
      }
    } catch {
      // Page-exit telemetry is best-effort and must not affect the app.
    }
  };

  private readonly handlePageHide = (): void => {
    try {
      this.flushWithBeacon();
    } catch {
      // Page-exit telemetry is best-effort and must not affect the app.
    }
  };

  private flushWithBeacon(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }

    if (
      typeof navigator === "undefined" ||
      typeof navigator.sendBeacon !== "function" ||
      typeof Blob === "undefined"
    ) {
      if (this.flushInProgress === null && this.queue.length > 0) {
        this.scheduleFlush();
      }
      return;
    }

    if (this.activeBatch !== null && !this.activeBatchBeaconQueued) {
      if (this.sendBeacon(this.activeBatch)) {
        this.activeBatchBeaconQueued = true;
      }
    }

    while (this.queue.length > 0) {
      const batch = this.queue.slice(0, MAX_BATCH_SIZE);

      if (!this.sendBeacon(batch)) {
        break;
      }

      this.queue.splice(0, batch.length);
    }

    if (this.flushInProgress === null && this.queue.length > 0) {
      this.scheduleFlush();
    }
  }

  private sendBeacon(batch: TelemetryEventEnvelope[]): boolean {
    try {
      const blob = new Blob([JSON.stringify({ events: batch })], {
        type: "application/json",
      });

      return navigator.sendBeacon(TELEMETRY_ENDPOINT, blob);
    } catch {
      return false;
    }
  }
}

/** The one shared service instance used by the backoffice. */
export const telemetryService = TelemetryService.getInstance();
