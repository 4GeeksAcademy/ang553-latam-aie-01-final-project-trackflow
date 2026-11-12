"use client";

const PKCE_VERIFIER_KEY = "trackflow_support_pkce_verifier";
const PKCE_STATE_KEY = "trackflow_support_pkce_state";
const SUPPORT_SCOPES = "openid incidents:read inventory:read";

let accessToken: string | null = null;
let accessTokenExpiresAt = 0;

export class SupportAuthorizationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SupportAuthorizationError";
  }
}

export class SupportAuthorizationRedirect extends Error {
  constructor() {
    super("Redirecting to the support identity provider.");
    this.name = "SupportAuthorizationRedirect";
  }
}

function configuration(): {
  authorizationEndpoint: string;
  tokenEndpoint: string;
  clientId: string;
} {
  const authorizationEndpoint =
    process.env.NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT;
  const tokenEndpoint = process.env.NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT;
  const clientId = process.env.NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID;
  if (!authorizationEndpoint || !tokenEndpoint || !clientId) {
    throw new SupportAuthorizationError(
      "La autenticación OIDC de soporte no está configurada.",
    );
  }
  for (const value of [authorizationEndpoint, tokenEndpoint]) {
    const url = new URL(value);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (
      (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    ) {
      throw new SupportAuthorizationError(
        "La configuración OIDC de soporte no es segura.",
      );
    }
  }
  return { authorizationEndpoint, tokenEndpoint, clientId };
}

function randomBase64Url(byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  globalThis.crypto.getRandomValues(bytes);
  let binary = "";
  bytes.forEach((value) => {
    binary += String.fromCharCode(value);
  });
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

async function challenge(verifier: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  let binary = "";
  new Uint8Array(digest).forEach((value) => {
    binary += String.fromCharCode(value);
  });
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function callbackUrl(): string {
  return `${window.location.origin}/backoffice/support`;
}

function clearCallbackParameters(): void {
  window.history.replaceState({}, "", "/backoffice/support");
}

function clearPkce(): void {
  window.sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  window.sessionStorage.removeItem(PKCE_STATE_KEY);
}

export function clearSupportAccessToken(): void {
  accessToken = null;
  accessTokenExpiresAt = 0;
}

export async function initializeSupportAuthorization(): Promise<boolean> {
  const parameters = new URLSearchParams(window.location.search);
  const code = parameters.get("code");
  const returnedState = parameters.get("state");
  const providerError = parameters.get("error");
  if (!code && !providerError) return false;

  const expectedState = window.sessionStorage.getItem(PKCE_STATE_KEY);
  const verifier = window.sessionStorage.getItem(PKCE_VERIFIER_KEY);
  clearCallbackParameters();
  clearPkce();
  if (providerError || !code || !returnedState || returnedState !== expectedState || !verifier) {
    throw new SupportAuthorizationError(
      "No se pudo validar la respuesta de autenticación de soporte.",
    );
  }

  const { tokenEndpoint, clientId } = configuration();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: clientId,
    code,
    code_verifier: verifier,
    redirect_uri: callbackUrl(),
  });
  const response = await fetch(tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    throw new SupportAuthorizationError(
      "No se pudo completar la autenticación de soporte.",
    );
  }
  const payload = (await response.json()) as Record<string, unknown>;
  if (
    typeof payload.access_token !== "string" ||
    payload.access_token.length === 0 ||
    String(payload.token_type).toLowerCase() !== "bearer" ||
    typeof payload.expires_in !== "number" ||
    !Number.isFinite(payload.expires_in) ||
    payload.expires_in <= 0
  ) {
    throw new SupportAuthorizationError(
      "El proveedor devolvió una credencial de soporte inválida.",
    );
  }
  accessToken = payload.access_token;
  accessTokenExpiresAt = Date.now() + payload.expires_in * 1000;
  return true;
}

async function redirectForAuthorization(): Promise<never> {
  const { authorizationEndpoint, clientId } = configuration();
  const verifier = randomBase64Url(64);
  const state = randomBase64Url(32);
  window.sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  window.sessionStorage.setItem(PKCE_STATE_KEY, state);
  const url = new URL(authorizationEndpoint);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", clientId);
  url.searchParams.set("redirect_uri", callbackUrl());
  url.searchParams.set("scope", SUPPORT_SCOPES);
  url.searchParams.set("state", state);
  url.searchParams.set("code_challenge", await challenge(verifier));
  url.searchParams.set("code_challenge_method", "S256");
  window.location.assign(url.toString());
  throw new SupportAuthorizationRedirect();
}

export async function getSupportAccessToken(): Promise<string> {
  if (accessToken && Date.now() < accessTokenExpiresAt - 15_000) {
    return accessToken;
  }
  clearSupportAccessToken();
  if (await initializeSupportAuthorization()) {
    if (accessToken) return accessToken;
  }
  return redirectForAuthorization();
}
