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
// Access + refresh tokens are kept in localStorage so a reload keeps the
// session (pragmatic demo trade-off vs. the httpOnly-cookie ideal, which
// needs backend cookie support the auth service doesn't expose yet).

const ACCESS_KEY = "creditflow.access_token";
const REFRESH_KEY = "creditflow.refresh_token";

export const tokenStore = {
  get access(): string | null {
    return localStorage.getItem(ACCESS_KEY);
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY);
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

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
  if (!options.anonymous && tokenStore.access) {
    headers["Authorization"] = `Bearer ${tokenStore.access}`;
  }
  try {
    return await fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers,
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

async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      const refresh = tokenStore.refresh;
      if (!refresh) return false;
      const res = await rawRequest("/auth/refresh", {
        method: "POST",
        body: { refresh_token: refresh },
        anonymous: true,
      });
      if (!res.ok) return false;
      const pair = (await res.json()) as { access_token: string; refresh_token: string };
      tokenStore.set(pair.access_token, pair.refresh_token);
      return true;
    })().catch(() => false);
    refreshing.finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
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

  // Silent refresh, then one retry — only for authenticated calls.
  if (res.status === 401 && !options.anonymous && tokenStore.refresh) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      res = await rawRequest(path, options);
    } else {
      tokenStore.clear();
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
