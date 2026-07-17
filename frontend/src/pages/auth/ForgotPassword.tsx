import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Input";
import { useToast } from "../../hooks/useToast";
import { ApiError } from "../../lib/api/client";
import { authApi } from "../../lib/api/endpoints";
import { AuthLayout } from "./AuthLayout";

/** Two-step OTP flow: request a one-time code by email, then set a new password. */
export default function ForgotPassword() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function requestReset(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await authApi.requestPasswordReset(email);
      // Dev mode echoes the code (no mail delivery yet) — pre-fill it.
      if (res.dev_reset_token) setToken(res.dev_reset_token);
      toast("info", "Reset code sent", "If that email is registered, a code is on its way.");
      setStep("confirm");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmReset(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await authApi.confirmPasswordReset(token.trim(), password);
      toast("success", "Password updated", "Sign in with your new password.");
      navigate("/login");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Reset failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Reset your password"
      subtitle={
        step === "request"
          ? "Enter your email and we'll send a one-time reset code."
          : "Enter the code from your email and choose a new password."
      }
      footer={
        <Link to="/login" className="font-medium text-accent-300 hover:text-accent-400">
          Back to sign in
        </Link>
      }
    >
      {step === "request" ? (
        <form onSubmit={requestReset} className="space-y-4">
          <Field label="Email" error={error}>
            <Input
              type="email"
              required
              autoComplete="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Button type="submit" size="lg" loading={busy} className="w-full">
            Send reset code
          </Button>
        </form>
      ) : (
        <form onSubmit={confirmReset} className="space-y-4">
          <Field label="Reset code">
            <Input
              required
              placeholder="Paste the code from your email"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </Field>
          <Field label="New password" hint="At least 8 characters." error={error}>
            <Input
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Button type="submit" size="lg" loading={busy} className="w-full">
            Set new password
          </Button>
          <button
            type="button"
            onClick={() => setStep("request")}
            className="w-full text-center text-xs text-ink-faint transition-colors hover:text-accent-300"
          >
            Didn't get a code? Request again
          </button>
        </form>
      )}
    </AuthLayout>
  );
}
