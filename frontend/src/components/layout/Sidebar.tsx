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
} from "lucide-react";
import { ADMIN_ROLES, useAuth } from "../../hooks/useAuth";
import { Logo } from "./Logo";

const NAV = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/generate", label: "AI Studio", icon: Sparkles },
  { to: "/content", label: "Content", icon: FileText },
  { to: "/calendar", label: "Calendar", icon: Calendar },
  { to: "/social", label: "Social", icon: Share2 },
  { to: "/scraper", label: "Scraper", icon: Globe },
  { to: "/credits", label: "Credits", icon: Coins },
  { to: "/billing", label: "Billing", icon: CreditCard },
  { to: "/notifications", label: "Notifications", icon: Bell },
];

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { role } = useAuth();
  const showAdmin = role !== null && ADMIN_ROLES.includes(role);

  return (
    <aside className="flex h-full w-56 shrink-0 flex-col border-r border-edge bg-surface/50">
      <div className="flex h-14 items-center border-b border-edge px-4">
        <Logo />
      </div>
      <nav className="flex-1 space-y-0.5 overflow-y-auto p-3">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            className={({ isActive }) =>
              "group flex items-center gap-3 rounded-field px-3 py-2 text-[13px] font-medium transition-colors duration-150 " +
              (isActive
                ? "bg-accent-600/15 text-accent-300"
                : "text-ink-soft hover:bg-surface-2 hover:text-ink")
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  size={16}
                  className={
                    isActive ? "text-accent-400" : "text-ink-faint group-hover:text-ink-soft"
                  }
                />
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
            <NavLink
              to="/admin"
              onClick={onNavigate}
              className={({ isActive }) =>
                "group flex items-center gap-3 rounded-field px-3 py-2 text-[13px] font-medium transition-colors duration-150 " +
                (isActive
                  ? "bg-accent-600/15 text-accent-300"
                  : "text-ink-soft hover:bg-surface-2 hover:text-ink")
              }
            >
              {({ isActive }) => (
                <>
                  <ShieldCheck
                    size={16}
                    className={
                      isActive ? "text-accent-400" : "text-ink-faint group-hover:text-ink-soft"
                    }
                  />
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
