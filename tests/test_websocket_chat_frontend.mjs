import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addOptimisticUserMessage, applyChatEvent, chatErrorMessage, chatMessageText, chatProgressText, chatRetryDelay, chatUnitCoverageText, emptyChatState, parseChatServerEvent, reduceScopedChat,
} from "../uis/backoffice/lib/chatState.ts";
import { chatMemoryConversationId, currentChatMemoryProposal } from "../uis/backoffice/lib/chatMemory.ts";

const message = {
  message_id: "msg_user", role: "user", content: "hola", status: "completed",
  sequence: 1, created_at: "2026-11-24T20:00:00Z",
};

test("the workspace memory card addresses only the current chat's bound UUID", () => {
  const uuid = "74c58426-af5b-4992-907d-c4ee4057ce79";
  assert.equal(chatMemoryConversationId(uuid), uuid);
  assert.equal(chatMemoryConversationId(`chat_${uuid.replaceAll("-", "")}`), uuid);
  assert.equal(chatMemoryConversationId(uuid.toUpperCase()), uuid);
  assert.equal(chatMemoryConversationId(`chat_${uuid}`), null);
  assert.equal(chatMemoryConversationId(`${uuid}?client=other`), null);
});

test("expired or stale consent snapshots from another conversation or client stay hidden", () => {
  const conversation = "74c58426-af5b-4992-907d-c4ee4057ce79";
  const replacement = {
    id: "c32a58af-ab43-4208-aea2-a257e4399a03",
    conversation_id: conversation,
    scope: "client",
    client_id: "PureStep",
    category: "carrier_coverage",
    content: {},
    verification_status: "user_reported",
    expires_at: "2026-11-25T20:00:00Z",
  };
  const deadline = Date.parse(replacement.expires_at);
  assert.equal(currentChatMemoryProposal(replacement, conversation, "PureStep", deadline - 1), replacement);
  assert.equal(currentChatMemoryProposal(replacement, conversation, "PureStep", deadline), null);
  assert.equal(currentChatMemoryProposal(replacement, "new-chat", "PureStep", deadline - 1), null);
  assert.equal(currentChatMemoryProposal(replacement, conversation, "SoundWave", deadline - 1), null);
  assert.equal(currentChatMemoryProposal({ ...replacement, scope: "global" }, conversation, "PureStep", deadline - 1), null);
  assert.equal(currentChatMemoryProposal({ ...replacement, expires_at: "invalid" }, conversation, "PureStep", deadline - 1), null);
  assert.equal(currentChatMemoryProposal(null, conversation, "PureStep", deadline - 1), null);
});

test("switching sessions accepts a lower cursor and starts with that session's history", () => {
  const prior = { sessionId: "chat_a", chat: { ...emptyChatState, cursor: 9, messages: [message] } };
  const snapshot = parseChatServerEvent({
    event: "session_snapshot", data: { session_id: "chat_b", cursor: 0, messages: [] },
  });
  assert.ok(snapshot);
  const next = reduceScopedChat(prior, { sessionId: "chat_b", event: snapshot });
  assert.equal(next.chat.cursor, 0);
  assert.deepEqual(next.chat.messages, []);
  assert.equal(reduceScopedChat(next, { sessionId: "chat_a", text: "stale" }), next);
  const pending = reduceScopedChat(next, { sessionId: "chat_b", text: "nuevo turno" });
  assert.equal(pending.chat.generationActive, true);
  assert.equal(pending.chat.messages[0].content, "nuevo turno");
});

test("snapshot validates every message and restores interrupted history", () => {
  const valid = parseChatServerEvent({
    event: "session_snapshot",
    data: { session_id: "chat_1", cursor: 4, messages: [message, {
      ...message, message_id: "msg_agent", role: "assistant",
      content: "parcial", status: "interrupted", sequence: 2,
    }] },
  });
  assert.ok(valid);
  const restored = applyChatEvent(emptyChatState, valid);
  assert.equal(restored.cursor, 4);
  assert.equal(restored.messages[1].status, "interrupted");
  assert.equal(restored.generationActive, false);
  assert.equal(parseChatServerEvent({
    event: "session_snapshot",
    data: { session_id: "chat_1", cursor: 4, messages: [{ ...message, sequence: "1" }] },
  }), null);
});

test("cursor and message id deduplicate token chunks", () => {
  const snapshot = parseChatServerEvent({
    event: "session_snapshot", data: { session_id: "chat_1", cursor: 0, messages: [message] },
  });
  const chunk = parseChatServerEvent({
    event: "token_chunk", data: {
      session_id: "chat_1", token: "Tu", sequence: 1, message_id: "msg_agent",
      generation_id: "gen_1", cursor: 1,
    },
  });
  assert.ok(snapshot && chunk);
  const once = applyChatEvent(applyChatEvent(emptyChatState, snapshot), chunk);
  const duplicate = applyChatEvent(once, chunk);
  assert.equal(duplicate.messages.find((item) => item.message_id === "msg_agent").content, "Tu");
  assert.equal(duplicate.cursor, 1);
});

test("QA units stay marked through abort and replay without accepting ordinary deltas into the same message", () => {
  const chunk = (cursor, token, publication_kind) => parseChatServerEvent({
    event: "token_chunk", data: {
      session_id: "chat_1", token, sequence: cursor, message_id: "msg_agent",
      generation_id: "gen_1", cursor, publication_kind,
    },
  });
  const first = chunk(1, "Primera unidad.", "authorized_unit");
  assert.ok(first);
  const partial = applyChatEvent(emptyChatState, first);
  assert.equal(partial.messages[0].publication_kind, "authorized_unit");
  assert.equal(applyChatEvent(partial, chunk(2, "Texto sin verificar.", undefined)), partial);
  const second = chunk(2, "\n\nSegunda unidad.", "authorized_unit");
  assert.ok(second);
  const confirmed = applyChatEvent(partial, second);
  const interrupted = applyChatEvent(confirmed, parseChatServerEvent({
    event: "generation_interrupted", data: {
      session_id: "chat_1", message_id: "msg_agent", generation_id: "gen_1",
      status: "interrupted", cursor: 3,
    },
  }));
  const snapshot = parseChatServerEvent({
    event: "session_snapshot", data: { session_id: "chat_1", messages: interrupted.messages, cursor: 3 },
  });
  assert.ok(snapshot);
  assert.equal(snapshot.data.messages[0].content, "Primera unidad.\n\nSegunda unidad.");
  assert.equal(snapshot.data.messages[0].publication_kind, "authorized_unit");
  assert.equal(snapshot.data.messages[0].status, "interrupted");
  assert.equal(chunk(3, "Inválido", "original_delta"), null);
});

const coverageIds = { session_id: "chat_1", message_id: "msg_agent", generation_id: "gen_1" };

test("QA coverage-only events can precede a verified unit without creating placeholder content", () => {
  const coverage = { status: "none", missing_topics: ["return_window"] };
  const initial = parseChatServerEvent({
    event: "coverage_updated", data: { ...coverageIds, cursor: 1, coverage },
  });
  assert.ok(initial);
  const pending = applyChatEvent(emptyChatState, initial);
  assert.equal(pending.generationActive, true);
  assert.equal(pending.messages[0].content, "");
  assert.equal(pending.messages[0].publication_kind, undefined);
  assert.deepEqual(pending.messages[0].unit_coverage, coverage);
  assert.equal(applyChatEvent(pending, initial), pending);

  const rawToken = { ...coverageIds, cursor: 2, sequence: 1, token: "Plazo autorizado." };
  const ordinary = parseChatServerEvent({ event: "token_chunk", data: rawToken });
  assert.ok(ordinary);
  assert.equal(applyChatEvent(pending, ordinary), pending);
  const verified = parseChatServerEvent({
    event: "token_chunk",
    data: { ...rawToken, publication_kind: "authorized_unit", unit_coverage: { status: "complete" } },
  });
  assert.ok(verified);
  const delivered = applyChatEvent(pending, verified);
  assert.equal(delivered.messages[0].content, "Plazo autorizado.");
  assert.equal(delivered.messages[0].status, "streaming");
  assert.deepEqual(delivered.messages[0].unit_coverage, { status: "complete" });
});

test("QA partial coverage and execution stay independent after interruption and reconnect", () => {
  const partial = { status: "partial", missing_topics: ["return_cost", "return_inspection"] };
  const confirmed = parseChatServerEvent({
    event: "token_chunk",
    data: { ...coverageIds, cursor: 1, sequence: 1, token: "Plazo autorizado.",
      publication_kind: "authorized_unit", unit_coverage: partial },
  });
  const interrupted = parseChatServerEvent({
    event: "generation_interrupted", data: { ...coverageIds, cursor: 2, status: "interrupted" },
  });
  const late = parseChatServerEvent({
    event: "coverage_updated", data: { ...coverageIds, cursor: 3, coverage: { status: "complete" } },
  });
  assert.ok(confirmed && interrupted && late);
  const stopped = applyChatEvent(applyChatEvent(emptyChatState, confirmed), interrupted);
  assert.equal(stopped.messages[0].status, "interrupted");
  assert.deepEqual(stopped.messages[0].unit_coverage, partial);
  assert.equal(applyChatEvent(stopped, late), stopped);
  const replay = (history) => parseChatServerEvent({
    event: "session_snapshot", data: { session_id: "chat_1", cursor: 2, messages: history },
  });
  const restored = applyChatEvent(emptyChatState, replay(stopped.messages));
  assert.equal(restored.messages[0].content, "Plazo autorizado.");
  assert.deepEqual(restored.messages[0].unit_coverage, partial);
  assert.equal(applyChatEvent(restored, late), restored);
  const redacted = { ...restored.messages[0], content: "", publication_kind: undefined, unit_coverage: undefined };
  assert.equal(applyChatEvent(restored, replay([redacted])).messages[0].unit_coverage, undefined);
  assert.deepEqual(chatUnitCoverageText(partial), {
    label: "Cobertura parcial", missing: "Temas sin resolver (coste de devolución e inspección de devoluciones).",
  });
});

test("the QA wire validator rejects unrestricted topic labels and metadata on ordinary deltas", () => {
  const update = (coverage) => parseChatServerEvent({
    event: "coverage_updated", data: { ...coverageIds, cursor: 1, coverage },
  });
  assert.ok(update({ status: "none" }));
  assert.ok(update({ status: "indeterminate", missing_topics: [] }));
  assert.equal(update({ status: "all" }), null);
  assert.equal(update({ status: ["complete"] }), null);
  assert.equal(update({ status: "complete", missing_topics: ["return_window"] }), null);
  assert.equal(update({ status: "partial", missing_topics: ["return_window", "return_window"] }), null);
  assert.equal(update({ status: "partial", missing_topics: ["private-catalog-subject"] }), null);
  assert.equal(update({ status: "partial", private_reason: "denied" }), null);
  const data = { ...coverageIds, cursor: 1, sequence: 1, token: "Texto", unit_coverage: { status: "partial" } };
  assert.equal(parseChatServerEvent({ event: "token_chunk", data }), null);
  assert.equal(parseChatServerEvent({ event: "session_snapshot", data: {
    session_id: "chat_1", cursor: 1, messages: [{ ...message, unit_coverage: { status: "none" } }],
  } }), null);
  assert.deepEqual(chatUnitCoverageText({ status: "indeterminate" }), {
    label: "Cobertura indeterminada", missing: null,
  });
});

test("validated knowledge metadata stays attached to its message after reconnect", () => {
  const session_id = "chat_1";
  const source = {
    source_document: "returns-policy", section: "standard-return-process", retrieval_kind: "primary",
  };
  const knowledge = { sources: [source] };
  const token = "Solicita la devolución en el portal de la marca. Si el motivo cumple sus reglas, TrackFlow genera la etiqueta.";
  const chunk = parseChatServerEvent({
    event: "token_chunk",
    data: { session_id, token, sequence: 1, message_id: "msg_agent", generation_id: "gen_1", cursor: 1, knowledge },
  });
  const completed = parseChatServerEvent({
    event: "generation_completed",
    data: { session_id, message_id: "msg_agent", generation_id: "gen_1", cursor: 2 },
  });
  assert.ok(chunk && completed);
  const live = applyChatEvent(applyChatEvent(emptyChatState, chunk), completed);
  assert.deepEqual(live.messages[0].knowledge, knowledge);
  assert.equal(chatMessageText(live.messages[0]), token);

  const snapshot = parseChatServerEvent({
    event: "session_snapshot",
    data: { session_id, cursor: 2, messages: [live.messages[0]] },
  });
  assert.ok(snapshot);
  const restored = applyChatEvent(emptyChatState, snapshot);
  assert.equal(restored.messages[0].message_id, "msg_agent");
  assert.deepEqual(restored.messages[0].knowledge, knowledge);
  assert.deepEqual(applyChatEvent(restored, chunk), restored);
  const { knowledge: previousKnowledge, ...redactedMessage } = restored.messages[0];
  assert.deepEqual(previousKnowledge, knowledge);
  const redacted = parseChatServerEvent({
    event: "session_snapshot",
    data: { session_id, cursor: 2, messages: [redactedMessage] },
  });
  assert.ok(redacted);
  assert.equal(applyChatEvent(restored, redacted).messages[0].knowledge, undefined);
});

test("knowledge metadata rejects invalid coverage, sources and user message attribution", () => {
  const source = {
    source_document: "returns-policy", section: "standard-return-process", retrieval_kind: "primary",
  };
  const chunk = (knowledge) => parseChatServerEvent({
    event: "token_chunk",
    data: {
      session_id: "chat_1", token: "Respuesta", sequence: 1, message_id: "msg_agent",
      generation_id: "gen_1", cursor: 1, knowledge,
    },
  });
  assert.ok(chunk({ coverage: "complete", sources: [source] }));
  assert.ok(chunk({ coverage: "partial", sources: [source] }));
  assert.ok(chunk({ coverage: "insufficient", sources: [] }));
  assert.ok(chunk({ sources: [source] }));
  assert.equal(chunk({ sources: [] }), null);
  assert.equal(chunk({ sources: [{ ...source, retrieval_kind: "complementary" }] }), null);
  assert.equal(chunk({ coverage: "invented", sources: [source] }), null);
  assert.equal(chunk({ coverage: "partial", sources: [] }), null);
  assert.equal(chunk({ coverage: "partial", sources: [{ ...source, retrieval_kind: "complementary" }] }), null);
  assert.equal(chunk({ coverage: "insufficient", sources: [source] }), null);
  assert.equal(chunk({ coverage: "partial", sources: [{ ...source, source_document: "https://example.org" }] }), null);
  assert.equal(parseChatServerEvent({
    event: "session_snapshot",
    data: { session_id: "chat_1", cursor: 1, messages: [{ ...message, knowledge: { coverage: "complete", sources: [source] } }] },
  }), null);
  const state = { ...emptyChatState, messages: [{ ...message, message_id: "msg_agent" }] };
  assert.equal(applyChatEvent(state, chunk({ coverage: "complete", sources: [source] })), state);
});

test("interrupted and completed are terminal, stale events cannot regress them", () => {
  const chunk = parseChatServerEvent({
    event: "token_chunk", data: {
      session_id: "chat_1", token: "parcial", sequence: 1, message_id: "msg_agent",
      generation_id: "gen_1", cursor: 1,
    },
  });
  const interrupted = parseChatServerEvent({
    event: "generation_interrupted", data: {
      session_id: "chat_1", message_id: "msg_agent", status: "interrupted",
      generation_id: "gen_1", cursor: 2,
    },
  });
  assert.ok(chunk && interrupted);
  const terminal = applyChatEvent(applyChatEvent(emptyChatState, chunk), interrupted);
  assert.equal(terminal.messages[0].status, "interrupted");
  assert.equal(terminal.generationActive, false);
  assert.deepEqual(applyChatEvent(terminal, chunk), terminal);
});

test("controlled provider error releases the active UI and messages stay safe", () => {
  const active = { ...emptyChatState, generationActive: true };
  const error = parseChatServerEvent({
    event: "error", data: { session_id: "chat_1", code: "generation_unavailable" },
  });
  assert.ok(error);
  const failed = applyChatEvent(active, error);
  assert.equal(failed.generationActive, false);
  assert.equal(chatErrorMessage(failed.errorCode), "La generación de soporte no está disponible temporalmente.");
  assert.equal(parseChatServerEvent({ event: "error", data: { code: "private-detail" } }), null);
});

test("abort before the first token surfaces immediately without inserting draft text", () => {
  const interrupted = parseChatServerEvent({
    event: "generation_interrupted",
    data: { session_id: "chat_1", message_id: "msg_absent", status: "interrupted",
      generation_id: "gen_abort", cursor: 1 },
  });
  const nextChunk = parseChatServerEvent({
    event: "token_chunk",
    data: { session_id: "chat_1", token: "safe", sequence: 1,
      message_id: "msg_next", generation_id: "gen_next", cursor: 2 },
  });
  assert.ok(interrupted && nextChunk);
  const stopped = applyChatEvent({ ...emptyChatState, generationActive: true }, interrupted);
  assert.equal(stopped.generationActive, false);
  assert.equal(stopped.messages[0].message_id, "msg_absent");
  assert.equal(stopped.messages[0].status, "interrupted");
  assert.equal(stopped.messages[0].content, "");
  const redirected = applyChatEvent(stopped, nextChunk);
  assert.deepEqual(redirected.messages.map((item) => item.content), ["", "safe"]);
});

test("WS-07 redirection renders the empty aborted turn before Zaragoza and reconciles once", () => {
  const session_id = "chat_1";
  const event = (name, data) => parseChatServerEvent({ event: name, data: { session_id, ...data } });
  const prior = [
    message,
    { ...message, message_id: "msg_prior", role: "assistant", sequence: 2, content: "Treinta días." },
  ];
  const initial = applyChatEvent(emptyChatState, event("session_snapshot", { cursor: 2, messages: prior }));
  const returns = "Explica detalladamente el proceso estándar de devoluciones y el tratamiento de devoluciones internacionales";
  const zaragoza = "¿Qué transportistas figuran en la política de cobertura para Zaragoza?";
  const pending = addOptimisticUserMessage(addOptimisticUserMessage(initial, returns), zaragoza, true);
  const interruption = event("generation_interrupted", {
    cursor: 3, message_id: "msg_aborted", generation_id: "gen_returns", status: "interrupted",
  });
  const interrupted = applyChatEvent(pending, interruption);
  assert.equal(interrupted.generationActive, false);
  assert.deepEqual(interrupted.messages.slice(2).map(({ content, status }) => [content, status]), [
    [returns, "completed"], ["", "interrupted"], [zaragoza, "completed"],
  ]);
  const marker = "There is not enough information in the TrackFlow knowledge base to answer this question.";
  const chunk = event("token_chunk", {
    cursor: 4, message_id: "msg_zaragoza", generation_id: "gen_zaragoza", sequence: 1,
    token: marker, answer_kind: "insufficient_context",
  });
  const completed = applyChatEvent(applyChatEvent(interrupted, chunk), event("generation_completed", {
    cursor: 5, message_id: "msg_zaragoza", generation_id: "gen_zaragoza",
  }));
  assert.equal(completed.generationActive, false);
  assert.equal(completed.messages[3].message_id, "msg_aborted");
  const answer = completed.messages[5];
  assert.equal(answer.status, "completed");
  assert.equal(answer.content, marker);
  assert.match(chatMessageText({ ...answer, content: "opaque fallback" }), /información suficiente/u);
  assert.equal(chatMessageText({ ...answer, knowledge: { coverage: "insufficient", sources: [] } }), marker);
  assert.equal(chatMessageText({ ...answer, answer_kind: undefined }), marker);
  assert.equal(event("token_chunk", { ...chunk.data, answer_kind: "arbitrary" }), null);

  const canonical = completed.messages.map((item) => item.role === "user" && item.sequence > 2
    ? { ...item, message_id: "msg_user_" + item.sequence } : item);
  const snapshot = event("session_snapshot", { cursor: 5, messages: canonical });
  const reconnected = applyChatEvent(completed, snapshot);
  assert.deepEqual(reconnected.messages, canonical);
  assert.equal(reconnected.messages.filter((item) => item.message_id === "msg_aborted").length, 1);
  assert.equal(applyChatEvent(reconnected, interruption), reconnected);
  assert.match(chatMessageText(reconnected.messages[5]), /información suficiente/u);
});

test("redirect is an independent optimistic user turn without erasing partial content", () => {
  const partial = {
    ...emptyChatState, cursor: 3, generationActive: true,
    messages: [{ ...message, role: "assistant", message_id: "msg_partial", content: "parcial", status: "streaming" }],
  };
  const redirected = addOptimisticUserMessage(partial, "ahora una devolución", true);
  assert.equal(redirected.messages.length, 2);
  assert.equal(redirected.messages[0].content, "parcial");
  assert.equal(redirected.messages[1].role, "user");
  assert.equal(redirected.messages[1].content, "ahora una devolución");
  assert.equal(redirected.cursor, 3);
  const interrupted = parseChatServerEvent({
    event: "generation_interrupted", data: {
      session_id: "chat_1", message_id: "msg_partial", status: "interrupted",
      generation_id: "gen_partial", cursor: 4,
    },
  });
  assert.ok(interrupted);
  const stopped = applyChatEvent(redirected, interrupted);
  assert.equal(stopped.messages[0].content, "parcial");
  assert.equal(stopped.messages[0].status, "interrupted");
  assert.equal(stopped.messages.length, 2);
});

test("completion winning the redirect race removes the unconfirmed user turn", () => {
  const initial = addOptimisticUserMessage(emptyChatState, "primera");
  const redirect = addOptimisticUserMessage(initial, "redirección", true);
  const completed = parseChatServerEvent({
    event: "generation_completed",
    data: { session_id: "chat_1", message_id: "msg_previous", generation_id: "gen_previous", cursor: 2 },
  });
  assert.ok(completed);
  const state = applyChatEvent(redirect, completed);
  assert.deepEqual(state.messages.map(({ content }) => content), ["primera"]);
  assert.equal(state.generationActive, false);
  assert.equal(state.errorCode, "interrupt_unavailable");
});

test("reconnection backoff is progressive and bounded", () => {
  assert.equal(chatRetryDelay(0, 0), 375);
  assert.ok(chatRetryDelay(2, 0.5) > chatRetryDelay(1, 0.5));
  assert.equal(chatRetryDelay(20, 1), 18_750);
});

test("ephemeral progress is deduplicated without advancing the durable cursor", () => {
  const progress = parseChatServerEvent({
    event: "generation_progress", data: {
      session_id: "chat_1", generation_id: "gen_1",
      stage: "retrieving_context", cursor: 1,
    },
  });
  assert.ok(progress);
  const initial = { ...emptyChatState, cursor: 7, generationActive: true };
  const pending = applyChatEvent(initial, progress);
  assert.equal(pending.cursor, 7);
  assert.equal(pending.progress.stage, "retrieving_context");
  assert.equal(chatProgressText(pending.progress), "Consultando el contexto autorizado…");
  assert.deepEqual(applyChatEvent(pending, progress), pending);

  const terminal = parseChatServerEvent({
    event: "generation_interrupted", data: {
      session_id: "chat_1", generation_id: "gen_1", message_id: "msg_1",
      status: "interrupted", cursor: 8,
    },
  });
  assert.ok(terminal);
  const stopped = applyChatEvent(pending, terminal);
  assert.equal(stopped.progress, null);
  assert.equal(stopped.generationActive, false);
  assert.deepEqual(applyChatEvent(stopped, {
    ...progress, data: { ...progress.data, stage: "validating_response", cursor: 2 },
  }), stopped);
  const newProgress = { ...progress, data: { ...progress.data, generation_id: "gen_next", cursor: 3 } };
  const redirected = applyChatEvent(stopped, newProgress);
  assert.equal(redirected.progress.generation_id, "gen_next");
  assert.equal(redirected.cursor, 8);
  assert.equal(applyChatEvent(redirected, progress), redirected);
});

test("validated token and errors suppress late progress for the same turn", () => {
  const progress = parseChatServerEvent({ event: "generation_progress", data: {
    session_id: "chat_1", generation_id: "gen_1", stage: "processing", cursor: 1,
  } });
  const token = parseChatServerEvent({ event: "token_chunk", data: {
    session_id: "chat_1", generation_id: "gen_1", message_id: "msg_1",
    token: "validado", sequence: 1, cursor: 1,
  } });
  assert.ok(progress && token);
  const afterToken = applyChatEvent(applyChatEvent(emptyChatState, progress), token);
  assert.equal(afterToken.progress, null);
  assert.deepEqual(applyChatEvent(afterToken, {
    ...progress, data: { ...progress.data, stage: "validating_response", cursor: 2 },
  }), afterToken);

  const error = parseChatServerEvent({
    event: "error", data: { session_id: "chat_1", code: "generation_unavailable" },
  });
  assert.ok(error);
  const afterError = applyChatEvent(applyChatEvent(emptyChatState, progress), error);
  assert.equal(afterError.progress, null);
  assert.deepEqual(applyChatEvent(afterError, progress), afterError);
});

test("snapshot restores progress only for a non-terminal turn", () => {
  const progress = {
    session_id: "chat_1", generation_id: "gen_1", stage: "processing", cursor: 3,
  };
  const terminalSnapshot = parseChatServerEvent({
    event: "session_snapshot", data: {
      session_id: "chat_1", cursor: 5, messages: [message], progress,
    },
  });
  const activeSnapshot = parseChatServerEvent({
    event: "session_snapshot", data: {
      session_id: "chat_1", cursor: 5,
      messages: [{ ...message, role: "assistant", status: "streaming" }], progress,
    },
  });
  assert.ok(terminalSnapshot && activeSnapshot);
  assert.equal(applyChatEvent(emptyChatState, terminalSnapshot).progress, null);
  assert.equal(applyChatEvent(emptyChatState, activeSnapshot).progress.stage, "processing");
  assert.equal(parseChatServerEvent({
    event: "generation_progress", data: { ...progress, stage: "private_stage" },
  }), null);
});

test("clarification answer kind is validated and remains visible as server text", () => {
  const clarification = parseChatServerEvent({
    event: "token_chunk", data: {
      session_id: "chat_1", generation_id: "gen_1", message_id: "msg_1",
      token: "¿A qué política te refieres?", sequence: 1, cursor: 1,
      answer_kind: "clarification",
    },
  });
  assert.ok(clarification);
  const state = applyChatEvent(emptyChatState, clarification);
  assert.equal(state.messages[0].answer_kind, "clarification");
  assert.equal(chatMessageText(state.messages[0]), "¿A qué política te refieres?");
});
