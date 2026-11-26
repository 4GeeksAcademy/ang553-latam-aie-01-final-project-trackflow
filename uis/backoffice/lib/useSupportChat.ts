"use client";

import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { authFetch } from "@/lib/authFetch";
import { getToken } from "@/lib/auth";
import { clearSupportAccessToken, getSupportAccessToken, SupportAuthorizationRedirect } from "@/lib/supportOidc";
import {
  chatErrorMessage,
  chatRetryDelay,
  emptyChatState,
  parseChatServerEvent,
  reduceScopedChat,
} from "@/lib/chatState";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "";
const SESSION_PREFIX = "trackflow_chat_session_id";
const CHAT_SESSION_ID = /^(?:chat_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/iu;

function storageKey(userId: string, clientId: string): string {
  return `${SESSION_PREFIX}:${userId}:${clientId}`;
}

function socketUrl(): string {
  const configured = process.env.NEXT_PUBLIC_CHAT_WS_URL;
  if (configured) return configured;
  if (BASE_URL.startsWith("http://") || BASE_URL.startsWith("https://")) {
    const url = new URL("/chat/ws", BASE_URL);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }
  if (["localhost", "127.0.0.1"].includes(window.location.hostname)) {
    return `ws://${window.location.hostname}:8000/chat/ws`;
  }
  const url = new URL("/chat/ws", window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

async function createSession(clientId: string): Promise<string> {
  const response = await authFetch(`${BASE_URL}/chat/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ client_id: clientId }),
  });
  if (!response.ok) throw new Error(response.status === 403 ? "forbidden" : "unavailable");
  const body = await response.json() as Record<string, unknown>;
  if (
    typeof body.session_id !== "string" ||
    !CHAT_SESSION_ID.test(body.session_id)
  ) {
    throw new Error("invalid-session");
  }
  return body.session_id;
}

export function useSupportChat(options: {
  userId: string | undefined;
  clientId: string;
  enabled: boolean;
}) {
  const { userId, clientId, enabled } = options;
  const scope = enabled && userId && clientId ? storageKey(userId, clientId) : null;
  const [scopedState, dispatch] = useReducer(reduceScopedChat, { sessionId: null, chat: emptyChatState });
  const [connection, setConnection] = useState<"idle" | "connecting" | "connected" | "reconnecting">("idle");
  const [transportError, setTransportError] = useState<string | null>(null);
  const [creatingSession, setCreatingSession] = useState(false);
  const [interrupting, setInterrupting] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const [selected, setSelected] = useState<{ scope: string; sessionId: string } | null>(null);
  const sessionId = scope === selected?.scope ? selected?.sessionId ?? null : null;
  const state = sessionId && scopedState.sessionId === sessionId ? scopedState.chat : emptyChatState;
  const socketRef = useRef<WebSocket | null>(null);
  const sessionRef = useRef<string | null>(null);
  const scopeRef = useRef(scope);
  const generationLockedRef = useRef(false);
  const interruptingRef = useRef(false);
  const creatingSessionRef = useRef(false);

  useLayoutEffect(() => {
    scopeRef.current = scope;
  }, [scope]);

  useEffect(() => {
    if (!enabled || !userId || !clientId) {
      socketRef.current?.close(1000, "chat disabled");
      socketRef.current = null;
      sessionRef.current = null;
      queueMicrotask(() => {
        setSelected(null);
        setConnection("idle");
      });
      return;
    }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = async () => {
      setConnection(attempt === 0 ? "connecting" : "reconnecting");
      setTransportError(null);
      try {
        const key = storageKey(userId, clientId);
        let sessionId = window.sessionStorage.getItem(key);
        if (!sessionId || !CHAT_SESSION_ID.test(sessionId)) {
          sessionId = await createSession(clientId);
          if (stopped) return;
          window.sessionStorage.setItem(key, sessionId);
        }
        sessionRef.current = sessionId;
        setSelected({ scope: key, sessionId });
        const accessToken = getToken();
        if (!accessToken) throw new Error("authentication");
        const supportToken = await getSupportAccessToken();
        if (stopped) return;
        const socket = new WebSocket(socketUrl());
        socketRef.current = socket;
        socket.onopen = () => {
          if (stopped || socketRef.current !== socket) return;
          socket.send(JSON.stringify({
            event: "authenticate",
            data: {
              session_id: sessionId,
              access_token: accessToken,
              support_token: supportToken,
              progress_events: true,
            },
          }));
        };
        socket.onmessage = (message) => {
          if (stopped || socketRef.current !== socket) return;
          try {
            const event = parseChatServerEvent(JSON.parse(String(message.data)) as unknown);
            if (!event || event.data.session_id !== sessionId) return;
            dispatch({ sessionId, event });
            if (event.event === "session_snapshot") {
              const snapshotActive = event.data.messages.some(
                (item) => item.status === "streaming",
              );
              generationLockedRef.current = snapshotActive;
              if (!snapshotActive) {
                interruptingRef.current = false;
                setInterrupting(false);
              }
              attempt = 0;
              setConnection("connected");
            } else if (event.event === "generation_progress" || event.event === "token_chunk" || event.event === "coverage_updated") {
              generationLockedRef.current = true;
            } else if (event.event === "generation_completed" || event.event === "generation_interrupted") {
              generationLockedRef.current = false;
              interruptingRef.current = false;
              setInterrupting(false);
            } else if (event.event === "error") {
              interruptingRef.current = false;
              setInterrupting(false);
              if (["generation_unavailable", "support_authorization_required", "session_unavailable"].includes(event.data.code)) {
                generationLockedRef.current = false;
              }
            }
          } catch {
            setTransportError("El chat recibió un evento inválido.");
          }
        };
        socket.onerror = () => {
          if (!stopped && socketRef.current === socket) {
            setTransportError("No se pudo mantener la conexión de chat.");
          }
        };
        socket.onclose = (event) => {
          if (socketRef.current === socket) socketRef.current = null;
          if (stopped || event.code === 1000) return;
          if (event.code === 4404) {
            window.sessionStorage.removeItem(key);
            sessionRef.current = null;
            setSelected(null);
            timer = setTimeout(() => void connect(), 250);
            return;
          }
          if (event.code === 4401 || event.code === 4403) {
            generationLockedRef.current = false;
            interruptingRef.current = false;
            setInterrupting(false);
            clearSupportAccessToken();
            setTransportError("La autenticación del chat expiró. Vuelve a autorizar soporte.");
            setConnection("idle");
            return;
          }
          attempt += 1;
          setConnection("reconnecting");
          timer = setTimeout(() => void connect(), chatRetryDelay(attempt));
        };
      } catch (error) {
        if (error instanceof SupportAuthorizationRedirect || stopped) return;
        const message = error instanceof Error && error.message === "forbidden"
          ? "No tienes acceso a ese cliente."
          : "No se pudo iniciar el chat en tiempo real.";
        setTransportError(message);
        setConnection("idle");
      }
    };
    void connect();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close(1000, "component disposed");
    };
  }, [clientId, enabled, retryNonce, userId]);

  const send = useCallback((text: string): boolean => {
    const normalized = text.trim();
    const socket = socketRef.current;
    if (!normalized || !sessionId || sessionRef.current !== sessionId || connection !== "connected" ||
        socket?.readyState !== WebSocket.OPEN || state.generationActive || generationLockedRef.current) return false;
    generationLockedRef.current = true;
    try {
      socket.send(JSON.stringify({ event: "user_message", data: { session_id: sessionId, text: normalized } }));
      dispatch({ sessionId, text: normalized });
      return true;
    } catch {
      generationLockedRef.current = false;
      setTransportError("No se pudo enviar el mensaje por el canal en tiempo real.");
      return false;
    }
  }, [connection, sessionId, state.generationActive]);

  const interrupt = useCallback((newInput?: string): boolean => {
    const socket = socketRef.current;
    if (!sessionId || sessionRef.current !== sessionId || connection !== "connected" ||
        socket?.readyState !== WebSocket.OPEN || !state.generationActive || interruptingRef.current) return false;
    const redirected = newInput?.trim();
    interruptingRef.current = true;
    setInterrupting(true);
    try {
      socket.send(JSON.stringify({
        event: "interrupt_requested",
        data: { session_id: sessionId, ...(redirected ? { new_input: redirected } : {}) },
      }));
      if (redirected) dispatch({ sessionId, text: redirected, redirect: true });
      return true;
    } catch {
      interruptingRef.current = false;
      setInterrupting(false);
      setTransportError("No se pudo solicitar la interrupción.");
      return false;
    }
  }, [connection, sessionId, state.generationActive]);

  const startNewSession = useCallback(async (): Promise<boolean> => {
    if (!scope || !clientId || creatingSessionRef.current || state.generationActive || generationLockedRef.current) return false;
    const requestedScope = scope;
    creatingSessionRef.current = true;
    setCreatingSession(true);
    setTransportError(null);
    try {
      const nextSessionId = await createSession(clientId);
      if (scopeRef.current !== requestedScope) return false;
      window.sessionStorage.setItem(requestedScope, nextSessionId);
      socketRef.current?.close(1000, "new chat session");
      socketRef.current = null;
      sessionRef.current = null;
      setSelected(null);
      setRetryNonce((value) => value + 1);
      return true;
    } catch (error) {
      setTransportError(
        error instanceof Error && error.message === "forbidden"
          ? "No tienes acceso a ese cliente."
          : "No se pudo crear una conversación nueva.",
      );
      return false;
    } finally {
      creatingSessionRef.current = false;
      setCreatingSession(false);
    }
  }, [clientId, scope, state.generationActive]);

  return {
    ...state,
    connection: sessionId ? connection : scope && connection !== "idle" ? "connecting" : "idle",
    error: transportError ?? chatErrorMessage(state.errorCode),
    sessionId,
    send,
    interrupt,
    startNewSession,
    creatingSession,
    interrupting,
    retry: () => setRetryNonce((value) => value + 1),
  };
}
