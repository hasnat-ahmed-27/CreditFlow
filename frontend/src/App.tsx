import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { RequireRole, RequireSession } from "./components/auth/RequireRole";
import { AppShell } from "./components/layout/AppShell";
import { ADMIN_ROLES, AuthProvider, MANAGER_ROLES, OWNER_ROLES } from "./hooks/useAuth";
import { ToastProvider } from "./hooks/useToast";
import Admin from "./pages/Admin";
import Billing from "./pages/Billing";
import Content from "./pages/Content";
import Credits from "./pages/Credits";
import Dashboard from "./pages/Dashboard";
import Generate from "./pages/Generate";
import Landing from "./pages/Landing";
import NotFound from "./pages/NotFound";
import Notifications from "./pages/Notifications";
import Onboarding from "./pages/Onboarding";
import Scraper from "./pages/Scraper";
import Social from "./pages/Social";
import Team from "./pages/Team";
import ForgotPassword from "./pages/auth/ForgotPassword";
import Login from "./pages/auth/Login";
import Signup from "./pages/auth/Signup";
import VerifyEmail from "./pages/auth/VerifyEmail";

// FullCalendar is ~300 kB of the bundle and only one route needs it, so the
// calendar loads on demand rather than taxing every first paint.
const CalendarPage = lazy(() => import("./pages/CalendarPage"));

function RouteFallback() {
  return (
    <div className="flex items-center justify-center py-20">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-edge-strong border-t-accent-500" />
    </div>
  );
}

/**
 * Route table + role gating (spec §4: "role-gated routing … enforced both
 * client-side (hide/redirect) and server-side"). The Gateway is the real
 * boundary — these guards only keep users off screens that would fail for
 * them, and they mirror the Gateway's own policy so the two agree.
 */
export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <AuthProvider>
          <Routes>
            {/* public */}
            <Route path="/" element={<Landing />} />
            <Route path="/login" element={<Login />} />
            <Route path="/signup" element={<Signup />} />
            <Route path="/verify-email" element={<VerifyEmail />} />
            <Route path="/forgot-password" element={<ForgotPassword />} />

            {/* Onboarding is authenticated but deliberately outside the app
                shell: the sidebar's nav is meaningless until the user has
                picked the account it would be scoped to. */}
            <Route element={<RequireSession />}>
              <Route path="/onboarding" element={<Onboarding />} />
            </Route>

            {/* authenticated app shell */}
            <Route element={<AppShell />}>
              {/* Owner + Member (spec §4) */}
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/generate" element={<Generate />} />
              <Route path="/content" element={<Content />} />
              <Route
                path="/calendar"
                element={
                  <Suspense fallback={<RouteFallback />}>
                    <CalendarPage />
                  </Suspense>
                }
              />
              <Route path="/social" element={<Social />} />
              <Route path="/scraper" element={<Scraper />} />
              <Route path="/notifications" element={<Notifications />} />

              {/* Team management — owner/admin, matching the User service's
                  require_manager. */}
              <Route element={<RequireRole allow={MANAGER_ROLES} />}>
                <Route path="/team" element={<Team />} />
              </Route>

              {/* Money: owner literally. The Gateway gates /billing/* and the
                  marketplace writes on role == "owner", so an admin — and a
                  SuperAdmin — is refused there too. */}
              <Route element={<RequireRole allow={OWNER_ROLES} />}>
                <Route path="/billing" element={<Billing />} />
                <Route path="/credits" element={<Credits />} />
              </Route>

              {/* Platform console. The Admin service scopes a non-superadmin
                  to their own account_id (spec §8 Service 13), which is why
                  owner/admin are admitted alongside superadmin here. */}
              <Route element={<RequireRole allow={ADMIN_ROLES} />}>
                <Route path="/admin" element={<Admin />} />
              </Route>

              <Route path="/app" element={<Navigate to="/dashboard" replace />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
