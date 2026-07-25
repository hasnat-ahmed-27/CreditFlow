import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Building2, Check, ChevronsUpDown, Loader2, Plus, User } from "lucide-react";
import { useAuth } from "../../hooks/useAuth";
import { useToast } from "../../hooks/useToast";
import { ApiError } from "../../lib/api/client";
import type { AccountSummary } from "../../lib/api/types";
import { shortId } from "../../lib/format";
import { Badge } from "../ui/Badge";
import { Skeleton } from "../ui/Skeleton";

/**
 * Spec §4: "Account Switcher (persistent component) — lets a user move
 * between every account they belong to; triggers a new account-scoped JWT."
 *
 * Switching is not a client-side filter: it calls POST /auth/switch-account,
 * which verifies membership server-side and mints a JWT scoped to the new
 * account (and revokes the old access session). Every account-scoped screen
 * then refetches, because AuthProvider bumps `accountEpoch` and AppShell keys
 * the routed subtree on it.
 */
export function AccountSwitcher() {
  const { accounts, accountsLoading, activeAccount, claims, switchAccount } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function choose(account: AccountSummary) {
    if (account.account_id === claims?.account_id) {
      setOpen(false);
      return;
    }
    setSwitchingTo(account.account_id);
    try {
      await switchAccount(account.account_id);
      toast("success", `Switched to ${accountLabel(account)}`);
      setOpen(false);
    } catch (err) {
      toast(
        "error",
        "Couldn't switch account",
        err instanceof ApiError ? err.message : undefined,
      );
    } finally {
      setSwitchingTo(null);
    }
  }

  if (accountsLoading && accounts.length === 0) {
    return <Skeleton className="h-8 w-40 rounded-field" />;
  }

  // Before the accounts list resolves (or if it failed) we still know the
  // scope from the token, so the switcher never renders as an empty hole.
  const label = activeAccount ? accountLabel(activeAccount) : shortId(claims?.account_id);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex max-w-[13rem] items-center gap-2 rounded-field border border-edge-strong bg-surface-2 py-1.5 pl-2 pr-2.5 text-left transition-colors hover:bg-surface-3"
      >
        <AccountIcon type={activeAccount?.type} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-ink">{label}</span>
          {activeAccount && (
            <span className="block truncate text-2xs capitalize text-ink-faint">
              {activeAccount.role} · {activeAccount.plan_tier}
            </span>
          )}
        </span>
        <ChevronsUpDown size={13} className="shrink-0 text-ink-faint" />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 top-full z-40 mt-2 w-72 rounded-card border border-edge-strong bg-surface-2 p-1.5 shadow-pop animate-scale-in"
        >
          <div className="px-2.5 pb-1.5 pt-1 text-2xs font-semibold uppercase tracking-wider text-ink-faint">
            Your accounts
          </div>

          <div className="max-h-72 overflow-y-auto">
            {accounts.map((account) => {
              const active = account.account_id === claims?.account_id;
              const busy = switchingTo === account.account_id;
              return (
                <button
                  key={account.account_id}
                  role="option"
                  aria-selected={active}
                  disabled={switchingTo !== null}
                  onClick={() => void choose(account)}
                  className={
                    "flex w-full items-center gap-2.5 rounded-field px-2.5 py-2 text-left transition-colors disabled:opacity-60 " +
                    (active ? "bg-accent-600/15" : "hover:bg-surface-3")
                  }
                >
                  <AccountIcon type={account.type} active={active} />
                  <span className="min-w-0 flex-1">
                    <span
                      className={
                        "block truncate text-[13px] font-medium " +
                        (active ? "text-accent-300" : "text-ink")
                      }
                    >
                      {accountLabel(account)}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-ink-faint">
                      <span className="capitalize">{account.type}</span>
                      <Badge tone={active ? "accent" : "neutral"} className="capitalize">
                        {account.role}
                      </Badge>
                    </span>
                  </span>
                  {busy ? (
                    <Loader2 size={14} className="shrink-0 animate-spin text-accent-400" />
                  ) : active ? (
                    <Check size={14} className="shrink-0 text-accent-400" />
                  ) : null}
                </button>
              );
            })}
          </div>

          <div className="mt-1 border-t border-edge pt-1">
            <button
              onClick={() => {
                setOpen(false);
                navigate("/onboarding");
              }}
              className="flex w-full items-center gap-2.5 rounded-field px-2.5 py-2 text-left text-[13px] text-ink-soft transition-colors hover:bg-surface-3 hover:text-ink"
            >
              <span className="flex h-6 w-6 items-center justify-center rounded-md border border-dashed border-edge-strong text-ink-faint">
                <Plus size={13} />
              </span>
              Create or join an account
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Individual accounts are auto-created on signup and carry no name, so fall
 *  back to something a human can recognise rather than a bare UUID. */
export function accountLabel(account: AccountSummary): string {
  if (account.name) return account.name;
  return account.type === "individual"
    ? "Personal workspace"
    : `Team ${shortId(account.account_id)}`;
}

function AccountIcon({ type, active }: { type?: string; active?: boolean }) {
  const Icon = type === "team" ? Building2 : User;
  return (
    <span
      className={
        "flex h-6 w-6 shrink-0 items-center justify-center rounded-md " +
        (active ? "bg-accent-600/25 text-accent-300" : "bg-surface-3 text-ink-faint")
      }
    >
      <Icon size={13} />
    </span>
  );
}
