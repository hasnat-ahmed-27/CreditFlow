import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  decodeClaims,
  setSessionExpiredHandler,
  tokenStore,
  type TokenClaims,
} from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";
import type { Role } from "../lib/api/types";

export interface AuthState {
  /** Claims decoded from the current access token; null = signed out. */
  claims: TokenClaims | null;
  role: Role | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-read tokens after an out-of-band update (e.g. refresh rotation). */
  sync: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [claims, setClaims] = useState<TokenClaims | null>(() =>
    tokenStore.access ? decodeClaims(tokenStore.access) : null,
  );
  const [ready, setReady] = useState(false);

  const sync = useCallback(() => {
    setClaims(tokenStore.access ? decodeClaims(tokenStore.access) : null);
  }, []);

  // If a stored session exists, validate it once against /me (also proves the
  // jti hasn't been revoked). Network failure keeps the local session — the
  // app degrades to empty states instead of a redirect loop.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (tokenStore.access) {
        try {
          await authApi.me();
        } catch {
          // request() already cleared tokens on an unrecoverable 401.
        }
      }
      if (!cancelled) {
        sync();
        setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sync]);

  useEffect(() => {
    setSessionExpiredHandler(sync);
    return () => setSessionExpiredHandler(null);
  }, [sync]);

  const login = useCallback(
    async (email: string, password: string) => {
      const pair = await authApi.login(email, password);
      tokenStore.set(pair.access_token, pair.refresh_token);
      sync();
    },
    [sync],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout(tokenStore.refresh);
    } catch {
      // Best effort — clear locally regardless.
    }
    tokenStore.clear();
    sync();
  }, [sync]);

  const value = useMemo<AuthState>(
    () => ({
      claims,
      role: (claims?.role as Role | undefined) ?? null,
      ready,
      login,
      logout,
      sync,
    }),
    [claims, ready, login, logout, sync],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** Roles allowed to see the Admin console (matches services/admin ADMIN_ROLES). */
export const ADMIN_ROLES: Role[] = ["owner", "admin", "superadmin"];
