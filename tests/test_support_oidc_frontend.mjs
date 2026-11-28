import assert from "node:assert/strict";
import { test } from "node:test";

process.env.NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT = "https://identity.example.test/authorize";
process.env.NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT = "https://identity.example.test/token";
process.env.NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID = "trackflow-support";

const values = new Map([
  ["trackflow_support_pkce_verifier", "verifier-for-delayed-exchange"],
  ["trackflow_support_pkce_state", "expected-state"],
]);
const sessionStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: (key) => values.delete(key),
};
const assigned = [];
let cleaned = false;
globalThis.window = {
  location: {
    origin: "https://backoffice.example.test",
    search: "?code=one-code&state=expected-state",
    assign: (url) => assigned.push(url),
  },
  history: {
    replaceState: () => {
      cleaned = true;
      globalThis.window.location.search = "";
    },
  },
  sessionStorage,
};

let releaseExchange;
let exchangeCount = 0;
globalThis.fetch = async () => {
  exchangeCount += 1;
  await new Promise((resolve) => { releaseExchange = resolve; });
  return {
    ok: true,
    json: async () => ({
      access_token: "support-token",
      token_type: "Bearer",
      expires_in: 120,
    }),
  };
};

const oidc = await import("../uis/backoffice/lib/supportOidc.ts");

test("delayed callback exchange is shared by page, chat and memory consumers", async () => {
  const page = oidc.initializeSupportAuthorization();
  const chat = oidc.getSupportAccessToken();
  const memory = oidc.getSupportAccessToken();
  assert.equal(cleaned, true);
  assert.equal(values.has("trackflow_support_pkce_verifier"), false);
  assert.equal(exchangeCount, 1);
  assert.equal(assigned.length, 0);

  releaseExchange();
  assert.equal(await page, true);
  assert.deepEqual(await Promise.all([chat, memory]), ["support-token", "support-token"]);
  assert.equal(exchangeCount, 1);
  assert.equal(assigned.length, 0);
});

test("a grant inside the non-reusable margin requires an explicit gesture", async () => {
  const originalNow = Date.now;
  Date.now = () => oidc.getSupportAccessTokenExpiresAt() - 10_000;
  try {
    await assert.rejects(
      oidc.getSupportAccessToken(),
      /autorización OIDC de soporte expiró/u,
    );
  } finally {
    Date.now = originalNow;
  }
  assert.equal(assigned.length, 0);
});

test("expired support grant stays stable until one explicit reauthorization", async () => {
  oidc.requireSupportReauthorization();
  await assert.rejects(
    oidc.getSupportAccessToken(),
    /autorización OIDC de soporte expiró/u,
  );
  assert.equal(assigned.length, 0);

  const first = oidc.reauthorizeSupport();
  const second = oidc.reauthorizeSupport();
  assert.equal(first, second);
  const results = await Promise.allSettled([first, second]);
  assert.ok(results.every((result) =>
    result.status === "rejected" &&
    result.reason instanceof oidc.SupportAuthorizationRedirect));
  assert.equal(assigned.length, 1);
  const destination = new URL(assigned[0]);
  assert.equal(destination.origin, "https://identity.example.test");
  assert.equal(destination.searchParams.get("code_challenge_method"), "S256");
  const repeated = await Promise.allSettled([oidc.reauthorizeSupport()]);
  assert.equal(repeated[0].status, "rejected");
  assert.equal(assigned.length, 1);
});

test("a late token consumer reuses the redirect latch after navigation starts", async () => {
  oidc.resetSupportAuthorization();
  const first = await Promise.allSettled([oidc.getSupportAccessToken()]);
  assert.equal(first[0].status, "rejected");
  assert.equal(assigned.length, 2);
  const late = await Promise.allSettled([oidc.getSupportAccessToken()]);
  assert.equal(late[0].status, "rejected");
  assert.equal(assigned.length, 2);
});
