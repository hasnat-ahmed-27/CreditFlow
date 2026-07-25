/**
 * The single gateway client: base URL, auth header injection, JSON parsing,
 * error normalization, and silent token refresh on 401.
 *
 * Every service behind the gateway raises FastAPI-style errors
 * ({"detail": string | object}), so ApiError carries both the raw detail and
 * a human-readable message.
 */

export const GATEWAY_URL: string = (
  import.meta.env.VITE_GATEWAY_URL ?? "http://localhost:8080"
).replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(ApiError.toMessage(status, detail));
    this.status = status;
    this.detail = detail;
  }

  static toMessage(status: number, detail: unknown): string {
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string") return message;
      return JSON.stringify(detail);
    }
    if (status === 0) return "Cannot reach the API gateway";
    return `Request failed (${status})`;
  }

  /** True when the backend itself was unreachable (gateway down / CORS). */
  get isNetwork(): boolean {
    return this.status === 0;
  }
}

// ---- token storage -------------------------------------------------------
/**
 * Spec §4: "store access token in memory, refresh token in an httpOnly
 * cookie; silent refresh on expiry".
 *
 * The access token is a module-scoped variable and nothing else — never
 * localStorage, never sessionStorage. Anything readable from storage is
 * readable by injected script at any later moment; a variable dies with the
 * page. The refresh token we never see at all: the browser holds it in the
 * httpOnly cookie Auth sets (services/auth/cookies.py) and attaches it to
 * /auth/* by itself.
 *
 * The cost is that a reload starts with NO access token, so the app must
 * bootstrap by asking /auth/refresh to mint one from the cookie — that is
 * what `bootstrapSession()` below is for, and it is the same code path as the
 * silent renewal on expiry.
 */
let accessToken: string | null = null;

export const session = {
  get access(): string | null {
    return accessToken;
  },
  set(token: string) {
    accessToken = token;
  },
  clear() {
    accessToken = null;
  },
};

/**
 * The readable half of the double-submit CSRF pair. Auth sets it alongside
 * the httpOnly refresh cookie; echoing it back in a header is what proves the
 * request came from our own origin rather than from a page that merely got
 * the browser to send cookies.
 *
 * Its presence also serves as a cheap "is there plausibly a session to
 * restore?" hint, so a first-time visitor doesn't pay for a doomed refresh
 * round-trip before the login screen renders.
 */
const CSRF_COOKIE = "cf_csrf";

function csrfToken(): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

export function hasSessionCookie(): boolean {
  return csrfToken() !== null;
}

/** Decoded (unverified) JWT payload — for display/routing only; the backend
 *  re-verifies the signature on every call. */
export interface TokenClaims {
  sub: string;
  account_id: string;
  role: string;
  jti: string;
  exp: number;
  type: string;
}

export function decodeClaims(token: string): TokenClaims | null {
  try {
    const payload = token.split(".")[1];
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as TokenClaims;
  } catch {
    return null;
  }
}

// ---- fetch wrapper -------------------------------------------------------

type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Query;
  /** Skip the Authorization header (auth endpoints themselves). */
  anonymous?: boolean;
  signal?: AbortSignal;
}

let onSessionExpired: (() => void) | null = null;

/** AuthProvider registers a callback so an unrecoverable 401 logs out cleanly. */
export function setSessionExpiredHandler(handler: (() => void) | null) {
  onSessionExpired = handler;
}

function buildUrl(path: string, query?: Query): string {
  const url = new URL(GATEWAY_URL + path);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

async function rawRequest(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!options.anonymous && accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }
  // Attached whenever we have one. Only /auth/refresh actually demands it, but
  // sending it everywhere costs a header and removes a whole class of "which
  // call needed the token again?" bug.
  const csrf = csrfToken();
  if (csrf) headers["X-CSRF-Token"] = csrf;

  try {
    return await fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers,
      // Required for the refresh cookie to travel at all: the frontend origin
      // and the gateway's differ in dev, and fetch omits cookies cross-origin
      // unless asked. The gateway sends back the matching
      // Access-Control-Allow-Credentials.
      credentials: "include",
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "Cannot reach the API gateway");
  }
}

// Single-flight refresh: concurrent 401s share one /auth/refresh call.
let refreshing: Promise<boolean> | null = null;

/**
 * Mint a new access token from the httpOnly refresh cookie. No body — the
 * browser supplies the credential, we supply the CSRF header (rawRequest
 * adds it), and Auth rotates both cookies in its response.
 */
async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      if (!hasSessionCookie()) return false;
      const res = await rawRequest("/auth/refresh", { method: "POST", anonymous: true });
      if (!res.ok) return false;
      const pair = (await res.json()) as { access_token: string };
      session.set(pair.access_token);
      return true;
    })().catch(() => false);
    refreshing.finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

/**
 * Restore a session on page load. With the access token held in memory, a
 * reload always starts signed-out until this resolves — AuthProvider awaits it
 * before deciding whether to show the app or redirect to /login.
 */
export function bootstrapSession(): Promise<boolean> {
  return tryRefresh();
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let res = await rawRequest(path, options);

  // Silent refresh, then one retry — only for authenticated calls. We can no
  // longer pre-check "do we hold a refresh token?" (it's httpOnly), so the
  // attempt itself is the check; tryRefresh short-circuits when the CSRF
  // cookie is absent, which means there is no session to restore.
  if (res.status === 401 && !options.anonymous) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      res = await rawRequest(path, options);
    } else {
      session.clear();
      onSessionExpired?.();
    }
  }

  const body = await parseBody(res);
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : body;
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) =>
    request<T>(path, { method: "POST", body, query }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
