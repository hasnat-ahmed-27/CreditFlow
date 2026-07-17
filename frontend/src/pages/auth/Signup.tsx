import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Input";
import { useToast } from "../../hooks/useToast";
import { ApiError } from "../../lib/api/client";
import { authApi } from "../../lib/api/endpoints";
import { AuthLayout } from "./AuthLayout";

export default function Signup() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await authApi.signup(email, password);
      toast("success", "Account created", "Check your email for a verification link.");
      // While the backend runs with AUTH_EXPOSE_DEV_TOKENS=1 (no mail
      // delivery yet) the token is echoed back — hand it to the verify
      // screen so the flow works end-to-end in a demo.
      navigate("/verify-email", {
        state: { email, devToken: res.dev_verification_token ?? null },
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Signup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Start generating AI content with free credits."
      footer={
        <>
          Already registered?{" "}
          <Link to="/login" className="font-medium text-accent-300 hover:text-accent-400">
            Sign in
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
        <Field label="Password" hint="At least 8 characters." error={error}>
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
          Create account
        </Button>
      </form>
    </AuthLayout>
  );
}
