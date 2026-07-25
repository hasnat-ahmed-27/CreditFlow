import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronDown, Coins, LogOut, Menu, User } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { useAuth } from "../../hooks/useAuth";
import { creditsApi } from "../../lib/api/endpoints";
import { formatNumber, shortId } from "../../lib/format";
import { Badge } from "../ui/Badge";
import { Skeleton } from "../ui/Skeleton";
import { AccountSwitcher } from "./AccountSwitcher";

export function Topbar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { claims, role, logout } = useAuth();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Re-keyed on the account so a switch refetches the balance for the new
  // scope — the credit ledger is account-level (spec §6).
  const balance = useApi(() => creditsApi.balance(), [claims?.account_id]);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [menuOpen]);

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-edge bg-surface/50 px-4 backdrop-blur">
      <div className="flex items-center gap-3">
        <button
          onClick={onToggleSidebar}
          aria-label="Toggle navigation"
          className="rounded-md p-1.5 text-ink-faint transition-colors hover:bg-surface-2 hover:text-ink lg:hidden"
        >
          <Menu size={18} />
        </button>
        <AccountSwitcher />
      </div>

      <div className="flex items-center gap-2.5">
        <button
          onClick={() => navigate("/credits")}
          title="Credit balance"
          className="flex items-center gap-2 rounded-full border border-edge-strong bg-surface-2 py-1.5 pl-2.5 pr-3.5 transition-colors hover:border-accent-600/50 hover:bg-surface-3"
        >
          <Coins size={14} className="text-warning" />
          {balance.loading ? (
            <Skeleton className="h-3.5 w-10" />
          ) : (
            <span className="text-xs font-semibold tnum text-ink">
              {balance.data ? formatNumber(balance.data.balance) : "—"}
            </span>
          )}
          <span className="hidden text-2xs text-ink-faint sm:inline">credits</span>
        </button>

        <div ref={menuRef} className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="flex items-center gap-2 rounded-full border border-edge-strong bg-surface-2 p-1 pr-2 transition-colors hover:bg-surface-3"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent-600/25 text-accent-300">
              <User size={13} />
            </span>
            <ChevronDown size={13} className="text-ink-faint" />
          </button>

          {menuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-full z-40 mt-2 w-60 rounded-card border border-edge-strong bg-surface-2 p-1.5 shadow-pop animate-scale-in"
            >
              <div className="border-b border-edge px-3 py-2.5">
                <p className="text-xs font-medium text-ink">
                  User {shortId(claims?.sub)}
                </p>
                <p className="mt-0.5 flex items-center gap-1.5 text-2xs text-ink-faint">
                  Account {shortId(claims?.account_id)}
                  {role && (
                    <Badge tone="accent" className="capitalize">
                      {role}
                    </Badge>
                  )}
                </p>
              </div>
              <button
                role="menuitem"
                onClick={async () => {
                  setMenuOpen(false);
                  await logout();
                  navigate("/login");
                }}
                className="mt-1 flex w-full items-center gap-2.5 rounded-field px-3 py-2 text-left text-[13px] text-ink-soft transition-colors hover:bg-surface-3 hover:text-ink"
              >
                <LogOut size={14} />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
