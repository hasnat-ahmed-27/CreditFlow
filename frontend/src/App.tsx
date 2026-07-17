import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/layout/AppShell";
import { AuthProvider } from "./hooks/useAuth";
import { ToastProvider } from "./hooks/useToast";
import Admin from "./pages/Admin";
import Billing from "./pages/Billing";
import CalendarPage from "./pages/CalendarPage";
import Content from "./pages/Content";
import Credits from "./pages/Credits";
import Dashboard from "./pages/Dashboard";
import Generate from "./pages/Generate";
import Landing from "./pages/Landing";
import NotFound from "./pages/NotFound";
import Notifications from "./pages/Notifications";
import Scraper from "./pages/Scraper";
import Social from "./pages/Social";
import ForgotPassword from "./pages/auth/ForgotPassword";
import Login from "./pages/auth/Login";
import Signup from "./pages/auth/Signup";
import VerifyEmail from "./pages/auth/VerifyEmail";

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

            {/* authenticated app shell */}
            <Route element={<AppShell />}>
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/generate" element={<Generate />} />
              <Route path="/content" element={<Content />} />
              <Route path="/calendar" element={<CalendarPage />} />
              <Route path="/social" element={<Social />} />
              <Route path="/scraper" element={<Scraper />} />
              <Route path="/credits" element={<Credits />} />
              <Route path="/billing" element={<Billing />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/admin" element={<Admin />} />
              <Route path="/app" element={<Navigate to="/dashboard" replace />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
