import { NavLink } from "react-router-dom";
import {
  Bell,
  Calendar,
  Coins,
  CreditCard,
  FileText,
  Globe,
  LayoutDashboard,
  Share2,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { ADMIN_ROLES, MANAGER_ROLES, OWNER_ROLES, hasRole, useAuth } from "../../hooks/useAuth";
import type { Role } from "../../lib/api/types";
import { Logo } from "./Logo";

interface NavItem {
  to: string;
  label: string;
  icon: typeof LayoutDashboard;
  /** Omitted = every authenticated role. Mirrors App.tsx's route guards, so a
   *  link is never shown for a route that would redirect. */
  allow?: Role[];
}

/** Spec §4's audience split. "Owner + Member" pages carry no `allow`; the
 *  Owner-only and manager-only ones name their roles. */
const NAV: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/generate", label: "AI Studio", icon: Sparkles },
  { to: "/content", label: "Content", icon: FileText },
  { to: "/calendar", label: "Calendar", icon: Calendar },
  { to: "/social", label: "Social", icon: Share2 },
  { to: "/scraper", label: "Scraper", icon: Globe },
  { to: "/team", label: "Team", icon: Users, allow: MANAGER_ROLES },
  { to: "/credits", label: "Credits", icon: Coins, allow: OWNER_ROLES },
  { to: "/billing", label: "Billing", icon: CreditCard, allow: OWNER_ROLES },
  { to: "/notifications", label: "Notifications", icon: Bell },
];

const LINK_CLASSES =
  "group flex items-center gap-3 rounded-field px-3 py-2 text-[13px] font-medium transition-colors duration-150 ";

function linkClass({ isActive }: { isActive: boolean }): string {
  return (
    LINK_CLASSES +
    (isActive
      ? "bg-accent-600/15 text-accent-300"
      : "text-ink-soft hover:bg-surface-2 hover:text-ink")
  );
}

function iconClass(isActive: boolean): string {
  return isActive ? "text-accent-400" : "text-ink-faint group-hover:text-ink-soft";
}

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { role } = useAuth();
  const visible = NAV.filter((item) => !item.allow || hasRole(role, item.allow));
  const showAdmin = hasRole(role, ADMIN_ROLES);

  return (
    <aside className="flex h-full w-56 shrink-0 flex-col border-r border-edge bg-surface/50">
      <div className="flex h-14 items-center border-b border-edge px-4">
        <Logo />
      </div>
      <nav className="flex-1 space-y-0.5 overflow-y-auto p-3">
        {visible.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} onClick={onNavigate} className={linkClass}>
            {({ isActive }) => (
              <>
                <Icon size={16} className={iconClass(isActive)} />
                {label}
              </>
            )}
          </NavLink>
        ))}
        {showAdmin && (
          <>
            <div className="px-3 pb-1 pt-4 text-2xs font-semibold uppercase tracking-wider text-ink-faint">
              Platform
            </div>
            <NavLink to="/admin" onClick={onNavigate} className={linkClass}>
              {({ isActive }) => (
                <>
                  <ShieldCheck size={16} className={iconClass(isActive)} />
                  Admin
                </>
              )}
            </NavLink>
          </>
        )}
      </nav>
      <div className="border-t border-edge p-4">
        <p className="text-2xs leading-relaxed text-ink-faint">
          CreditFlow · AI content platform
        </p>
      </div>
    </aside>
  );
}
