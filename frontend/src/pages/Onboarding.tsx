import { useState, type FormEvent, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Building2, Check, Mail, User } from "lucide-react";
import { accountLabel } from "../components/layout/AccountSwitcher";
import { Logo } from "../components/layout/Logo";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Field, Input } from "../components/ui/Input";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { userApi } from "../lib/api/endpoints";
import { markOnboarded } from "../lib/onboarding";

type Choice = "individual" | "team" | "invite";

/**
 * Spec §4 Onboarding: "Create or Join Account — new user chooses to create an
 * Individual account, create a Team account, or accept a pending team invite."
 *
 * One note on the Individual option. The backend creates the individual
 * account AUTOMATICALLY at signup (spec §8 Service 3, and the User service
 * refuses to create one through POST /accounts on purpose), so this option
 * does not create anything — it confirms the workspace the user already has
 * and switches into it. Presenting it as a choice keeps the three-way decision
 * the spec describes without pretending to issue a call the API doesn't have.
 *
 * Reachable three ways: once after a first sign-in, from the account
 * switcher's "Create or join an account", and directly from an invite email's
 * /onboarding?invite=<token> link.
 */
export default function Onboarding() {
  const { accounts, activeAccount, claims, switchAccount } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const invitedToken = params.get("invite");
  const individual = accounts.find((a) => a.type === "individual") ?? null;

  const [choice, setChoice] = useState<Choice>(invitedToken ? "invite" : "individual");
  const [teamName, setTeamName] = useState("");
  const [inviteToken, setInviteToken] = useState(invitedToken ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function finish(message: string, detail?: string) {
    markOnboarded(claims?.sub);
    toast("success", message, detail);
    navigate("/dashboard", { replace: true });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (choice === "individual") {
        // Already the active scope for a brand-new user — only switch when
        // they're coming from some other account.
        if (individual && individual.account_id !== activeAccount?.account_id) {
          await switchAccount(individual.account_id);
        }
        finish("You're all set", "Using your personal workspace.");
        return;
      }

      if (choice === "team") {
        const name = teamName.trim();
        if (!name) {
          setError("Give your team a name.");
          return;
        }
        const created = await userApi.createTeam(name);
        // Creating a team does NOT re-scope the session by itself: the token
        // still names the old account. Switching is what mints a JWT for the
        // team the user just made an owner of.
        await switchAccount(created.account_id);
        finish(`${name} is ready`, "You're the owner — invite your teammates from Team.");
        return;
      }

      const token = inviteToken.trim();
      if (!token) {
        setError("Paste the invite token from your email.");
        return;
      }
      const accepted = await userApi.acceptInvite(token);
      // switchAccount re-resolves membership server-side and reloads the list,
      // so accepting and switching is all it takes to land in the new team.
      await switchAccount(accepted.account_id);
      finish(
        `Joined ${accepted.account_name ?? "the team"}`,
        `You've been added as ${accepted.role}.`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen bg-bg">
      <div className="mx-auto w-full max-w-3xl px-4 py-10 sm:px-6 sm:py-16">
        <div className="mb-8 flex items-center justify-between">
          <Logo />
          <button
            onClick={() => {
              markOnboarded(claims?.sub);
              navigate("/dashboard", { replace: true });
            }}
            className="text-xs text-ink-faint transition-colors hover:text-ink-soft"
          >
            Skip for now
          </button>
        </div>

        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Create or join an account
        </h1>
        <p className="mt-1.5 max-w-xl text-sm leading-relaxed text-ink-faint">
          Everything in CreditFlow — credits, content, billing — belongs to an
          account. Work solo, start a team, or join one you've been invited to.
        </p>

        <form onSubmit={submit} className="mt-8 space-y-3">
          <ChoiceCard
            selected={choice === "individual"}
            onSelect={() => setChoice("individual")}
            icon={<User size={16} />}
            title="Keep it personal"
            badge={<Badge tone="neutral">Ready now</Badge>}
            description={
              individual
                ? `Use ${accountLabel(individual)} — created for you at signup, with you as the owner.`
                : "Use the personal workspace created for you at signup."
            }
          />

          <ChoiceCard
            selected={choice === "team"}
            onSelect={() => setChoice("team")}
            icon={<Building2 size={16} />}
            title="Create a team account"
            description="A shared workspace with its own credits, content and billing. You'll be the owner."
          >
            <Field label="Team name">
              <Input
                autoFocus
                placeholder="Acme Marketing"
                value={teamName}
                maxLength={120}
                onChange={(e) => setTeamName(e.target.value)}
              />
            </Field>
          </ChoiceCard>

          <ChoiceCard
            selected={choice === "invite"}
            onSelect={() => setChoice("invite")}
            icon={<Mail size={16} />}
            title="Join a team"
            description="Accept an invite you received by email."
          >
            <Field
              label="Invite token"
              hint="Opening the link from your invite email fills this in automatically."
            >
              <Input
                placeholder="Paste your invite token"
                value={inviteToken}
                onChange={(e) => setInviteToken(e.target.value)}
              />
            </Field>
          </ChoiceCard>

          {error && (
            <p className="rounded-field border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger">
              {error}
            </p>
          )}

          <div className="flex items-center gap-3 pt-2">
            <Button type="submit" size="lg" loading={busy} icon={<ArrowRight size={15} />}>
              Continue
            </Button>
            {accounts.length > 1 && (
              <span className="text-xs text-ink-faint">
                You already belong to {accounts.length} accounts — switch any time from the
                top bar.
              </span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

function ChoiceCard({
  selected,
  onSelect,
  icon,
  title,
  description,
  badge,
  children,
}: {
  selected: boolean;
  onSelect: () => void;
  icon: ReactNode;
  title: string;
  description: string;
  badge?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div
      className={
        "rounded-card border bg-surface transition-colors " +
        (selected ? "border-accent-600/50 shadow-glow-accent" : "border-edge hover:border-edge-strong")
      }
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={selected}
        className="flex w-full items-start gap-3.5 p-5 text-left"
      >
        <span
          className={
            "flex h-9 w-9 shrink-0 items-center justify-center rounded-field border " +
            (selected
              ? "border-accent-600/40 bg-accent-600/20 text-accent-300"
              : "border-edge-strong bg-surface-2 text-ink-faint")
          }
        >
          {icon}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="text-sm font-semibold text-ink">{title}</span>
            {badge}
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-ink-faint">
            {description}
          </span>
        </span>
        <span
          className={
            "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border " +
            (selected ? "border-accent-500 bg-accent-600 text-white" : "border-edge-strong")
          }
        >
          {selected && <Check size={10} strokeWidth={3} />}
        </span>
      </button>
      {selected && children && <div className="border-t border-edge px-5 py-4">{children}</div>}
    </div>
  );
}
