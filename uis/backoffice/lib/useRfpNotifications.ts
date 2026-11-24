import { useCallback, useEffect, useRef, useState } from "react";
import {
  getRfpCommercialSnapshot, openRfpNotificationStream, parseRfpTicketCreated, RfpApiError,
} from "@/lib/rfpApi";
import {
  mergeRfpCreated, reconcileRfpCommercialSnapshot, reconcileRfpCommercialTicket, rememberRfpTicket,
} from "@/lib/rfpFeedState";
import { compareRfpCursor, InvalidRfpSseFrame, isRfpCursor, readRfpSse, rfpRetryDelay } from "@/lib/rfpSse";
import type { RfpCommercialTicket, RfpTicketCreated } from "@/types/rfp";

export type RfpConnectionState = "connecting" | "connected" | "reconnecting" | "recovering" | "forbidden";

const IDLE_TIMEOUT_MS = 30_000;

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export function useRfpNotifications(userId: string | undefined) {
  const [connection, setConnection] = useState<RfpConnectionState>("connecting");
  const [authorized, setAuthorized] = useState(false);
  const [tickets, setTickets] = useState<RfpCommercialTicket[]>([]);
  const [notice, setNotice] = useState<RfpTicketCreated | null>(null);
  const [newTickets, setNewTickets] = useState<Set<string>>(() => new Set());
  const ownUpload = useRef(false);
  const ownTickets = useRef(new Set<string>());
  const deferred = useRef<RfpTicketCreated[]>([]);
  const announced = useRef(new Set<string>());

  const reconcileTicket = useCallback((ticket: RfpCommercialTicket) => {
    setTickets((prior) => reconcileRfpCommercialTicket(prior, ticket));
  }, []);

  const announce = useCallback((event: RfpTicketCreated) => {
    if (ownTickets.current.has(event.ticket_id) || announced.current.has(event.ticket_id)) return;
    rememberRfpTicket(announced.current, event.ticket_id);
    setNotice(event);
    setNewTickets(new Set(announced.current));
  }, []);

  const beginOwnUpload = useCallback(() => {
    if (ownUpload.current) return false;
    ownUpload.current = true;
    return true;
  }, []);

  const finishOwnUpload = useCallback((ticketId?: string) => {
    if (ticketId) rememberRfpTicket(ownTickets.current, ticketId);
    ownUpload.current = false;
    const held = deferred.current;
    deferred.current = [];
    for (const event of held) announce(event);
  }, [announce]);

  useEffect(() => {
    if (!userId) return;
    const session = new AbortController();
    const signal = session.signal;

    async function connect() {
      let cursor: string | undefined;
      let failures = 0;
      while (!signal.aborted) {
        const attempt = new AbortController();
        const attemptSignal = AbortSignal.any([signal, attempt.signal]);
        let timer: ReturnType<typeof setTimeout> | undefined;
        const resetTimeout = () => {
          clearTimeout(timer);
          timer = setTimeout(() => attempt.abort(), IDLE_TIMEOUT_MS);
        };
        resetTimeout();
        try {
          if (cursor === undefined) {
            setConnection(failures ? "recovering" : "connecting");
            const snapshot = await getRfpCommercialSnapshot(attemptSignal);
            if (attemptSignal.aborted) throw new Error("Snapshot interrupted.");
            setAuthorized(true);
            setTickets((prior) => reconcileRfpCommercialSnapshot(prior, snapshot.items));
            cursor = snapshot.cursor;
            resetTimeout();
          } else {
            setConnection("recovering");
          }
          const stream = await openRfpNotificationStream(cursor, attemptSignal);
          if (attemptSignal.aborted) throw new Error("SSE connection interrupted.");
          setConnection("connected");
          const connectedAt = performance.now();
          for await (const frame of readRfpSse(stream, resetTimeout)) {
            if (attemptSignal.aborted) break;
            if (frame.event !== "rfp_ticket_created" || !isRfpCursor(frame.id) || frame.id === "0") {
              throw new InvalidRfpSseFrame("Evento inesperado en el stream RFP.");
            }
            if (compareRfpCursor(frame.id, cursor) <= 0) continue;
            const event = parseRfpTicketCreated(frame.data);
            setTickets((prior) => mergeRfpCreated(prior, event));
            if (ownUpload.current) {
              // A worker can publish before the upload POST has returned its ID.
              deferred.current = [...deferred.current.slice(-99), event];
            } else {
              announce(event);
            }
            cursor = frame.id;
            failures = 0;
          }
          if (performance.now() - connectedAt >= 10_000) failures = 0;
        } catch (error) {
          if (signal.aborted) return;
          if (error instanceof RfpApiError && (error.statusCode === 401 || error.statusCode === 403)) {
            setAuthorized(false);
            setTickets([]);
            setNotice(null);
            setNewTickets(new Set());
            deferred.current = [];
            setConnection("forbidden");
            return;
          }
          if (error instanceof InvalidRfpSseFrame
            || (error instanceof RfpApiError && error.statusCode === 409)) cursor = undefined;
        } finally {
          clearTimeout(timer);
          attempt.abort();
        }
        if (signal.aborted) return;
        setConnection("reconnecting");
        await abortableDelay(rfpRetryDelay(failures), signal);
        failures = Math.min(failures + 1, 10);
      }
    }

    void connect();
    return () => session.abort();
  }, [announce, userId]);

  return { authorized, connection, tickets, notice, newTickets, beginOwnUpload, finishOwnUpload, reconcileTicket };
}
