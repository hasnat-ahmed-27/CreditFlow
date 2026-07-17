import { Link } from "react-router-dom";
import {
  ArrowRight,
  CalendarClock,
  Check,
  Coins,
  Linkedin,
  Sparkles,
  Zap,
} from "lucide-react";
import { Logo } from "../components/layout/Logo";
import { useAuth } from "../hooks/useAuth";

const FEATURES = [
  {
    icon: Sparkles,
    title: "Streaming AI generation",
    body: "Watch content appear token by token, live over SSE. Pick a model, prompt it, ship it.",
  },
  {
    icon: Coins,
    title: "Credit-based metering",
    body: "One shared account balance, a transparent ledger, and an internal marketplace to trade surplus credits.",
  },
  {
    icon: CalendarClock,
    title: "Schedule everything",
    body: "One-off or recurring publishes on a real calendar, stored in UTC and shown in your timezone.",
  },
  {
    icon: Linkedin,
    title: "Publish to LinkedIn",
    body: "OAuth once, then push approved posts — with images — straight to your feed.",
  },
];

const TIERS = [
  { name: "Free", price: "$0", features: ["Starter credits", "1 seat", "Community support"] },
  {
    name: "Pro",
    price: "$29",
    features: ["Monthly credit allowance", "Priority queue", "LinkedIn publishing"],
    highlight: true,
  },
  { name: "Team", price: "$99", features: ["Everything in Pro", "Shared workspace", "Role-based access"] },
];

export default function Landing() {
  const { claims } = useAuth();

  return (
    <div className="relative min-h-screen overflow-hidden bg-bg">
      <div className="pointer-events-none absolute -top-40 left-1/2 h-[28rem] w-[56rem] -translate-x-1/2 rounded-full bg-accent-600/[0.12] blur-3xl" />

      {/* nav */}
      <header className="relative mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
        <Logo large />
        <nav className="flex items-center gap-3">
          {claims ? (
            <Link
              to="/dashboard"
              className="inline-flex h-9 items-center gap-2 rounded-field bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500"
            >
              Open app <ArrowRight size={14} />
            </Link>
          ) : (
            <>
              <Link
                to="/login"
                className="rounded-field px-3 py-2 text-sm font-medium text-ink-soft transition-colors hover:text-ink"
              >
                Sign in
              </Link>
              <Link
                to="/signup"
                className="inline-flex h-9 items-center rounded-field bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500"
              >
                Get started
              </Link>
            </>
          )}
        </nav>
      </header>

      {/* hero */}
      <section className="relative mx-auto max-w-6xl px-6 pb-20 pt-16 text-center">
        <p className="mx-auto mb-5 inline-flex items-center gap-2 rounded-full border border-accent-600/30 bg-accent-600/10 px-3.5 py-1 text-xs font-medium text-accent-300 animate-fade-up">
          <Zap size={12} /> Credit-based AI content platform
        </p>
        <h1 className="mx-auto max-w-3xl text-4xl font-semibold leading-tight tracking-tight text-ink sm:text-5xl animate-fade-up">
          Generate, schedule, and publish content — <span className="text-gradient">powered by credits</span>
        </h1>
        <p className="mx-auto mt-5 max-w-xl text-base leading-relaxed text-ink-soft animate-fade-up">
          CreditFlow turns AI generation into a metered, team-friendly workflow: stream drafts live,
          approve them, put them on a calendar, and push them to LinkedIn.
        </p>
        <div className="mt-8 flex justify-center gap-3 animate-fade-up">
          <Link
            to="/signup"
            className="inline-flex h-11 items-center gap-2 rounded-field bg-accent-600 px-6 text-sm font-semibold text-white shadow-glow-accent transition-colors hover:bg-accent-500"
          >
            Start for free <ArrowRight size={15} />
          </Link>
          <Link
            to="/login"
            className="inline-flex h-11 items-center rounded-field border border-edge-strong bg-surface-2 px-6 text-sm font-medium text-ink transition-colors hover:bg-surface-3"
          >
            Sign in
          </Link>
        </div>
      </section>

      {/* features */}
      <section className="relative mx-auto max-w-6xl px-6 pb-20">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <div
              key={title}
              className="rounded-card border border-edge bg-surface p-5 shadow-card transition-colors hover:border-accent-600/40"
            >
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent-600/15 text-accent-300">
                <Icon size={16} />
              </span>
              <h3 className="mt-3 text-sm font-semibold text-ink">{title}</h3>
              <p className="mt-1.5 text-xs leading-relaxed text-ink-faint">{body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* pricing */}
      <section className="relative mx-auto max-w-4xl px-6 pb-24">
        <h2 className="text-center text-2xl font-semibold tracking-tight text-ink">
          Simple, credit-first pricing
        </h2>
        <div className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-3">
          {TIERS.map((tier) => (
            <div
              key={tier.name}
              className={
                "rounded-card border bg-surface p-6 shadow-card " +
                (tier.highlight ? "border-accent-500/50 shadow-glow-accent" : "border-edge")
              }
            >
              <h3 className="text-sm font-semibold text-ink">{tier.name}</h3>
              <p className="mt-2 text-3xl font-semibold tracking-tight text-ink">
                {tier.price}
                <span className="text-xs font-normal text-ink-faint">/mo</span>
              </p>
              <ul className="mt-4 space-y-2">
                {tier.features.map((feature) => (
                  <li key={feature} className="flex items-start gap-2 text-xs text-ink-soft">
                    <Check size={13} className="mt-0.5 shrink-0 text-success" />
                    {feature}
                  </li>
                ))}
              </ul>
              <Link
                to="/signup"
                className={
                  "mt-6 inline-flex h-9 w-full items-center justify-center rounded-field text-sm font-medium transition-colors " +
                  (tier.highlight
                    ? "bg-accent-600 text-white hover:bg-accent-500"
                    : "border border-edge-strong bg-surface-2 text-ink hover:bg-surface-3")
                }
              >
                Choose {tier.name}
              </Link>
            </div>
          ))}
        </div>
      </section>

      <footer className="relative border-t border-edge py-8 text-center text-xs text-ink-faint">
        CreditFlow — internship project build. Not a real product (yet).
      </footer>
    </div>
  );
}
