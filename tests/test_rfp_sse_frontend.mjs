import assert from "node:assert/strict";
import { test } from "node:test";

import {
  mergeRfpAdminList, mergeRfpCreated, reconcileRfpCommercialSnapshot, reconcileRfpCommercialTicket,
} from "../uis/backoffice/lib/rfpFeedState.ts";
import {
  compareRfpCursor, InvalidRfpSseFrame, isRfpCursor, readRfpSse, rfpRetryDelay,
} from "../uis/backoffice/lib/rfpSse.ts";

test("SSE survives single-byte chunks, UTF-8, split CRLF and heartbeats", async () => {
  const bytes = new TextEncoder().encode(
    ": keep-alive\r\n\r\nid: 9007199254740993\r\nevent: rfp_ticket_created\r\n" +
      'data: {"client_name":"Compañía 🚚"}\r\n\r\nid: 2\ndata: incomplete',
  );
  const stream = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  assert.deepEqual(await Array.fromAsync(readRfpSse(stream)), [{
    id: "9007199254740993", event: "rfp_ticket_created", data: '{"client_name":"Compañía 🚚"}',
  }]);
  assert.equal(stream.locked, false);
});

test("SSE releases its reader if a caller stops consuming", async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode("id: 1\ndata: {}\n\n")); },
    cancel() { cancelled = true; },
  });
  for await (const frame of readRfpSse(stream)) {
    assert.equal(frame.id, "1");
    break;
  }
  assert.equal(cancelled, true);
  assert.equal(stream.locked, false);
});

test("SSE rejects oversized or invalid UTF-8 frames", async () => {
  for (const bytes of [new Uint8Array(20_000).fill(65), Uint8Array.of(0x80)]) {
    const stream = new ReadableStream({
      start(controller) { controller.enqueue(bytes); controller.close(); },
    });
    await assert.rejects(() => Array.fromAsync(readRfpSse(stream)), InvalidRfpSseFrame);
  }
});

test("cursor order is exact above JavaScript's safe integer and delays stay bounded", () => {
  assert.equal(isRfpCursor("0"), true);
  assert.equal(isRfpCursor("9223372036854775807"), true);
  assert.equal(isRfpCursor("9223372036854775808"), false);
  assert.equal(isRfpCursor("01"), false);
  assert.ok(compareRfpCursor("9007199254740993", "9007199254740992") > 0);
  assert.ok(compareRfpCursor("100", "99") > 0);
  assert.equal(rfpRetryDelay(0, 0), 375);
  assert.ok(rfpRetryDelay(1, 1) > rfpRetryDelay(0, 1));
  assert.equal(rfpRetryDelay(10, 1), 15_000);
});

test("live and replay merge by ID without regressing a terminal ticket", () => {
  const event = {
    ticket_id: "5d503335-98f7-49f0-8ac0-b0b0128dfc31",
    rfp_id: null, client_name: "Compañía", client_country: null,
    services_requested: ["warehouse"], status: "analyzing",
    created_at: "2026-11-24T21:02:10Z",
  };
  const first = mergeRfpCreated([], event);
  assert.equal(first[0].status, "analizando");
  assert.equal(mergeRfpCreated(first, event).length, 1);
  const terminal = { ...first[0], client_name: null, status: "analisis_completo", job_status: "succeeded" };
  assert.deepEqual(mergeRfpCreated([terminal], event), [{ ...terminal, client_name: "Compañía" }]);
  const staleAdmin = {
    ticket_id: event.ticket_id, rfp_id: null, client_name: null, currency: null,
    status: "analizando", job_status: "queued", departments_needed: [],
    created_at: event.created_at, updated_at: event.created_at,
  };
  const display = mergeRfpAdminList([staleAdmin], first);
  assert.equal(display.length, 1);
  assert.equal(display[0].client_name, "Compañía");
  assert.deepEqual(display[0].departments_needed, ["warehouse"]);
  const freshAdmin = { ...staleAdmin, status: "terminado", job_status: "succeeded" };
  assert.equal(mergeRfpAdminList([freshAdmin], first)[0].status, "terminado");
});

test("commercial detail reconciles its list row and delayed reads cannot undo progress", () => {
  const pending = {
    ticket_id: "qa-selected", rfp_id: null, client_name: "Cliente", client_country: "US",
    services_requested: ["warehouse"], status: "analizando", job_status: "running",
    created_at: "2026-11-24T21:02:10Z", updated_at: "2026-11-24T21:02:10Z",
  };
  const other = { ...pending, ticket_id: "qa-other" };
  const terminal = { ...pending, status: "analisis_completo", job_status: "succeeded" };
  const reconciled = reconcileRfpCommercialTicket([pending, other], terminal);
  assert.deepEqual(reconciled, [terminal, other]);
  assert.deepEqual(reconcileRfpCommercialTicket(reconciled, pending), reconciled);
  assert.deepEqual(reconcileRfpCommercialSnapshot(reconciled, [pending, other]), reconciled);
  const failed = { ...pending, job_status: "failed" };
  assert.deepEqual(reconcileRfpCommercialTicket([pending], failed), [failed]);
  assert.deepEqual(reconcileRfpCommercialTicket([failed], pending), [failed]);
  assert.deepEqual(reconcileRfpCommercialTicket([pending], other), [pending]);
});
