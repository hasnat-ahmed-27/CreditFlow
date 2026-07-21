import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  bootstrapSession,
  decodeClaims,
  hasSessionCookie,
  session,
  setSessionExpiredHandler,
  type TokenClaims,
} from "../lib/api/client";
import { authApi, userApi } from "../lib/api/endpoints";
import type { AccountSummary, Role } from "../lib/api/types";

export interface AuthState {
  /** Claims decoded from the in-memory access token; null = signed out. */
  claims: TokenClaims | null;
  /** The caller's role IN THE CURRENTLY SCOPED ACCOUNT (or "superadmin"). */
  role: Role | null;
  /** False until the bootstrap refresh has settled — routes must wait for it,
   *  or a reload would bounce a signed-in user to /login. */
  ready: boolean;

  /** Every account the user belongs to (spec §4 account switcher). */
  accounts: AccountSummary[];
  accountsLoading: boolean;
  activeAccount: AccountSummary | null;
  /**
   * Increments on every scope change. AppShell uses it as a React `key` for
   * the routed subtree, which remounts every screen and so refetches all
   * account-scoped data — no page needs to know the switcher exists.
   */
  accountEpoch: number;

  /** Resolves with the new session's claims, so the caller can route on them
   *  without waiting for the context state to settle. */
  login: (email: string, password: string) => Promise<TokenClaims | null>;
  logout: () => Promise<void>;
  switchAccount: (accountId: string) => Promise<AccountSummary | null>;
  reloadAccounts: () => Promise<AccountSummary[]>;
  /** Re-read the token after an out-of-band update (e.g. refresh rotation). */
  sync: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [claims, setClaims] = useState<TokenClaims | null>(null);
  const [ready, setReady] = useState(false);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [accountEpoch, setAccountEpoch] = useState(0);

  const sync = useCallback(() => {
    setClaims(session.access ? decodeClaims(session.access) : null);
  }, []);

  // Bootstrap. The access token lives in memory only, so a reload starts with
  // nothing and the httpOnly refresh cookie is the sole way back into the
  // session. Skipped entirely when no session cookie is present, so a
  // first-time visitor reaches the login screen without a wasted round-trip.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (hasSessionCookie()) {
        await bootstrapSession();
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

  const reloadAccounts = useCallback(async (): Promise<AccountSummary[]> => {
    if (!session.access) {
      setAccounts([]);
      return [];
    }
    setAccountsLoading(true);
    try {
      const res = await userApi.myAccounts();
      setAccounts(res.accounts);
      return res.accounts;
    } catch {
      // Non-fatal: the switcher degrades to showing just the active account.
      return [];
    } finally {
      setAccountsLoading(false);
    }
  }, []);

  // Keyed on the USER, not the account: switching scope doesn't change which
  // accounts someone belongs to, and refetching on every switch would make the
  // switcher flicker its own list away mid-interaction.
  const userId = claims?.sub ?? null;
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!userId) {
      loadedFor.current = null;
      setAccounts([]);
      return;
    }
    if (loadedFor.current === userId) return;
    loadedFor.current = userId;
    void reloadAccounts();
  }, [userId, reloadAccounts]);

  const login = useCallback(
    async (email: string, password: string) => {
      const pair = await authApi.login(email, password);
      session.set(pair.access_token); // refresh token stayed in the httpOnly cookie
      sync();
      setAccountEpoch((n) => n + 1);
      return decodeClaims(pair.access_token);
    },
    [sync],
  );

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Best effort — clear locally regardless.
    }
    session.clear();
    setAccounts([]);
    loadedFor.current = null;
    sync();
  }, [sync]);

  const switchAccount = useCallback(
    async (accountId: string): Promise<AccountSummary | null> => {
      const pair = await authApi.switchAccount(accountId);
      session.set(pair.access_token);
      sync();
      // Bump AFTER the new token is installed, so remounted screens fetch with
      // the new scope rather than racing the old one.
      setAccountEpoch((n) => n + 1);
      const next = await reloadAccounts();
      return next.find((a) => a.account_id === accountId) ?? null;
    },
    [sync, reloadAccounts],
  );

  const value = useMemo<AuthState>(() => {
    const activeAccount =
      accounts.find((a) => a.account_id === claims?.account_id) ?? null;
    return {
      claims,
      role: (claims?.role as Role | undefined) ?? null,
      ready,
      accounts,
      accountsLoading,
      activeAccount,
      accountEpoch,
      login,
      logout,
      switchAccount,
      reloadAccounts,
      sync,
    };
  }, [
    claims,
    ready,
    accounts,
    accountsLoading,
    accountEpoch,
    login,
    logout,
    switchAccount,
    reloadAccounts,
    sync,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

// ---- role policy ---------------------------------------------------------
/**
 * Who may see which screen (spec §4's audience split, §10's "Owner, Member and
 * SuperAdmin each see their correct, distinct set of frontend pages").
 *
 * These lists MIRROR the gateway's ROLE_RULES (services/gateway/security.py)
 * rather than inventing a second policy. The gateway is the security boundary
 * — it re-checks the role on every call and a hand-edited bundle changes
 * nothing — so this layer exists purely so a member is never shown a door
 * that would slam in their face.
 */
export const ADMIN_ROLES: Role[] = ["owner", "admin", "superadmin"];

/** Owner literally — the gateway gates billing and marketplace writes on
 *  `role == "owner"`, so a team Admin is refused there too. */
export const OWNER_ROLES: Role[] = ["owner"];

/** The account-scoped managers (User service `require_manager`). */
export const MANAGER_ROLES: Role[] = ["owner", "admin"];

export function hasRole(role: Role | null, allowed: Role[]): boolean {
  return role !== null && allowed.includes(role);
}
