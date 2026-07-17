import { useEffect, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { MailCheck } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Field, Input } from "../../components/ui/Input";
import { useToast } from "../../hooks/useToast";
import { ApiError } from "../../lib/api/client";
import { authApi } from "../../lib/api/endpoints";
import { AuthLayout } from "./AuthLayout";

/**
 * Email-verification landing: consumes ?token=… from the emailed link, or
 * lets the user paste the token manually (dev mode echoes it at signup).
 */
export default function VerifyEmail() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();

  const state = location.state as { email?: string; devToken?: string | null } | null;
  const [token, setToken] = useState(params.get("token") ?? state?.devToken ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A token arriving via the link is consumed automatically.
  useEffect(() => {
    if (params.get("token")) void verify(params.get("token")!);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function verify(value: string) {
    setBusy(true);
    setError(null);
    try {
      await authApi.verifyEmail(value.trim());
      toast("success", "Email verified", "You can sign in now.");
      navigate("/login");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Verification failed");
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (token.trim()) void verify(token);
  }

  return (
    <AuthLayout
      title="Verify your email"
      subtitle={
        state?.email ? (
          <>
            We sent a verification link to <span className="text-ink-soft">{state.email}</span>.
            Open it — or paste the token below.
          </>
        ) : (
          "Open the link from your inbox, or paste the verification token below."
        )
      }
      footer={
        <Link to="/login" className="font-medium text-accent-300 hover:text-accent-400">
          Back to sign in
        </Link>
      }
    >
      <div className="mb-5 flex justify-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-accent-600/15 text-accent-300">
          <MailCheck size={22} />
        </span>
      </div>
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Verification token" error={error}>
          <Input
            required
            placeholder="Paste the token from your email"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </Field>
        <Button type="submit" size="lg" loading={busy} className="w-full">
          Verify email
        </Button>
      </form>
    </AuthLayout>
  );
}
