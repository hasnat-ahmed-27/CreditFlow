import { Navigate, Outlet, useLocation } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import { hasRole, useAuth } from "../../hooks/useAuth";
import { Card } from "../ui/Card";
import { EmptyState } from "../ui/EmptyState";
import type { Role } from "../../lib/api/types";

/**
 * "Signed in?" without the app chrome — for authenticated screens that render
 * outside AppShell (onboarding). Blocks until the bootstrap refresh settles,
 * otherwise a reload would redirect a valid session to /login before the
 * httpOnly cookie had a chance to restore it.
 */
export function RequireSession() {
  const { claims, ready } = useAuth();
  const location = useLocation();

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-edge-strong border-t-accent-500" />
      </div>
    );
  }
  if (!claims) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  return <Outlet />;
}

/**
 * Route-level role gate (spec §4: "Route guarding by role (Owner / Member /
 * SuperAdmin) with graceful redirect, not just a hidden nav link").
 *
 * This is a UX layer, not a security boundary — the Gateway re-checks the role
 * on every call, so a user who edits their way past this gets 403s and nothing
 * more. What it buys is that a member never lands on a screen that can only
 * fail.
 *
 * Two outcomes, deliberately different:
 *   - navigating to a page your role can't use REDIRECTS to the dashboard, so
 *     a stale bookmark or an old link lands somewhere useful instead of on a
 *     dead end;
 *   - a role that is momentarily unknown (mid-switch) renders an explanation
 *     rather than bouncing, because bouncing there would look like a bug.
 */
export function RequireRole({
  allow,
  redirectTo = "/dashboard",
}: {
  allow: Role[];
  redirectTo?: string;
}) {
  const { role, ready } = useAuth();
  const location = useLocation();

  // AppShell already blocks on `ready`, but a guard that assumed it would be
  // a landmine the first time one is used outside the shell.
  if (!ready) return null;

  if (hasRole(role, allow)) return <Outlet />;

  // No role at all means the token is gone; AppShell's own redirect to /login
  // is the right destination, so don't fight it by sending them to a page
  // that will bounce them again.
  if (role === null) return null;

  if (redirectTo) {
    return <Navigate to={redirectTo} state={{ deniedFrom: location.pathname }} replace />;
  }

  return (
    <Card>
      <EmptyState
        icon={<ShieldAlert size={18} />}
        title="You don't have access to this page"
        body={`This area is limited to ${allow.join(" or ")} roles. You're signed in as ${role}.`}
      />
    </Card>
  );
}
