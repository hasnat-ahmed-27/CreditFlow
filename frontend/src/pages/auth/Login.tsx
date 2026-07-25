import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Input";
import { useAuth } from "../../hooks/useAuth";
import { useToast } from "../../hooks/useToast";
import { ApiError } from "../../lib/api/client";
import { hasOnboarded } from "../../lib/onboarding";
import { AuthLayout } from "./AuthLayout";

export default function Login() {
  const { login } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const requested = (location.state as { from?: string } | null)?.from ?? null;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const claims = await login(email, password);
      toast("success", "Welcome back");
      // A user who was bounced here from a deep link goes back to it. Everyone
      // else lands on the dashboard — except on their first sign-in, where
      // spec §4's Create-or-Join screen comes first (shown once per user).
      if (requested) {
        navigate(requested, { replace: true });
      } else {
        navigate(hasOnboarded(claims?.sub) ? "/dashboard" : "/onboarding", { replace: true });
      }
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      if (apiError?.status === 403) {
        setError("Your email isn't verified yet.");
        navigate("/verify-email", { state: { email } });
      } else {
        setError(apiError?.message ?? "Login failed");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Welcome back — sign in to your workspace."
      footer={
        <>
          No account yet?{" "}
          <Link to="/signup" className="font-medium text-accent-300 hover:text-accent-400">
            Create one
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Email">
          <Input
            type="email"
            required
            autoComplete="email"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password" error={error}>
          <Input
            type="password"
            required
            autoComplete="current-password"
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <div className="flex items-center justify-between">
          <Link
            to="/forgot-password"
            className="text-xs text-ink-faint transition-colors hover:text-accent-300"
          >
            Forgot password?
          </Link>
        </div>
        <Button type="submit" size="lg" loading={busy} className="w-full">
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
