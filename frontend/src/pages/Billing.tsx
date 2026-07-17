import { useState } from "react";
import { Check, CreditCard, ExternalLink, Receipt, Sparkles } from "lucide-react";
import { StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { ConfirmDialog } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { Table, type Column } from "../components/ui/Table";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { billingApi } from "../lib/api/endpoints";
import type { Invoice } from "../lib/api/types";
import { formatCents, formatDate } from "../lib/format";

const PLANS = [
  {
    id: "free" as const,
    name: "Free",
    price: "$0",
    blurb: "Kick the tires",
    features: ["Starter credit grant", "Community support", "1 seat"],
  },
  {
    id: "pro" as const,
    name: "Pro",
    price: "$29",
    blurb: "For serious creators",
    features: ["Monthly credit allowance", "Priority generation queue", "LinkedIn publishing"],
    highlight: true,
  },
  {
    id: "team" as const,
    name: "Team",
    price: "$99",
    blurb: "Your whole crew",
    features: ["Everything in Pro", "Shared team workspace", "Role-based access"],
  },
];

export default function Billing() {
  const { role } = useAuth();
  const { toast } = useToast();
  const subscription = useApi(() => billingApi.subscription(), []);
  const invoices = useApi(() => billingApi.invoices(), []);
  const [confirmPlan, setConfirmPlan] = useState<"free" | "pro" | "team" | null>(null);
  const [busy, setBusy] = useState(false);

  const isOwner = role === "owner";
  const currentPlan = subscription.data?.plan;

  async function selectPlan(plan: "free" | "pro" | "team") {
    setBusy(true);
    try {
      if (plan === "free") {
        await billingApi.changePlan("free");
        toast("success", "Plan changed", "You're on the Free plan now.");
        subscription.reload();
      } else {
        // Paid tiers go through Stripe Checkout (test mode).
        const session = await billingApi.checkout(plan);
        window.location.href = session.checkout_url;
      }
    } catch (err) {
      toast("error", "Plan change failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
      setConfirmPlan(null);
    }
  }

  const invoiceColumns: Column<Invoice>[] = [
    { key: "date", header: "Date", render: (inv) => formatDate(inv.created_at) },
    { key: "status", header: "Status", render: (inv) => <StatusBadge status={inv.status} /> },
    {
      key: "due",
      header: "Amount",
      align: "right",
      render: (inv) => formatCents(inv.amount_due),
    },
    {
      key: "paid",
      header: "Paid",
      align: "right",
      render: (inv) => formatCents(inv.amount_paid),
    },
    {
      key: "link",
      header: "",
      align: "right",
      render: (inv) =>
        inv.hosted_invoice_url ? (
          <a
            href={inv.hosted_invoice_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs font-medium text-accent-300 hover:text-accent-400"
          >
            View <ExternalLink size={12} />
          </a>
        ) : null,
    },
  ];

  return (
    <div className="animate-fade-up">
      <PageHeader title="Billing" subtitle="Subscription plan, checkout, and invoice history." />

      {/* current subscription */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-full bg-accent-600/15 text-accent-300">
              <CreditCard size={17} />
            </span>
            <div>
              {subscription.loading ? (
                <Skeleton className="h-5 w-32" />
              ) : (
                <p className="text-sm font-semibold capitalize text-ink">
                  {subscription.data ? `${subscription.data.plan} plan` : "Plan unavailable"}
                </p>
              )}
              <p className="mt-0.5 text-xs text-ink-faint">
                {subscription.data?.grace_expires_at
                  ? `Grace period until ${formatDate(subscription.data.grace_expires_at)}`
                  : "Billed through Stripe (test mode)"}
              </p>
            </div>
          </div>
          {subscription.data && <StatusBadge status={subscription.data.status} />}
        </div>
      </Card>

      {/* plans */}
      <div className="mt-5 grid grid-cols-1 gap-4 md:grid-cols-3">
        {PLANS.map((plan) => {
          const active = currentPlan === plan.id;
          return (
            <Card
              key={plan.id}
              glow={plan.highlight && !active}
              className={active ? "border-accent-500/60" : ""}
            >
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-ink">{plan.name}</h3>
                {active ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-accent-600/20 px-2 py-0.5 text-2xs font-medium text-accent-300">
                    <Check size={11} /> Current
                  </span>
                ) : plan.highlight ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-accent-600/15 px-2 py-0.5 text-2xs font-medium text-accent-300">
                    <Sparkles size={11} /> Popular
                  </span>
                ) : null}
              </div>
              <p className="mt-3 text-3xl font-semibold tracking-tight text-ink">
                {plan.price}
                <span className="text-xs font-normal text-ink-faint">/mo</span>
              </p>
              <p className="mt-0.5 text-xs text-ink-faint">{plan.blurb}</p>
              <ul className="mt-4 space-y-2">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-start gap-2 text-xs text-ink-soft">
                    <Check size={13} className="mt-0.5 shrink-0 text-success" />
                    {feature}
                  </li>
                ))}
              </ul>
              <Button
                variant={active ? "secondary" : plan.highlight ? "primary" : "secondary"}
                size="sm"
                className="mt-5 w-full"
                disabled={active || !isOwner}
                title={isOwner ? undefined : "Plan changes are owner-only"}
                onClick={() => setConfirmPlan(plan.id)}
              >
                {active ? "Current plan" : plan.id === "free" ? "Downgrade" : `Upgrade to ${plan.name}`}
              </Button>
            </Card>
          );
        })}
      </div>

      {/* invoices */}
      <Card className="mt-5" padded={false}>
        <div className="p-5 pb-0">
          <CardHeader title="Invoices" subtitle="Owner-only billing history from Stripe" />
        </div>
        <div className="px-2 pb-2">
          {invoices.error ? (
            invoices.error.status === 403 ? (
              <EmptyState
                icon={<Receipt size={18} />}
                title="Owner-only"
                body="Invoice history is restricted to the account owner."
              />
            ) : (
              <ErrorState error={invoices.error} onRetry={invoices.reload} />
            )
          ) : (
            <Table
              columns={invoiceColumns}
              rows={invoices.data?.invoices ?? []}
              rowKey={(inv) => inv.invoice_id}
              loading={invoices.loading}
              empty={
                <EmptyState
                  icon={<Receipt size={18} />}
                  title="No invoices yet"
                  body="Complete a Stripe checkout and invoices will appear here."
                />
              }
            />
          )}
        </div>
      </Card>

      <ConfirmDialog
        open={confirmPlan !== null}
        onClose={() => setConfirmPlan(null)}
        onConfirm={() => confirmPlan && void selectPlan(confirmPlan)}
        title={confirmPlan === "free" ? "Downgrade to Free" : `Upgrade to ${confirmPlan}`}
        danger={confirmPlan === "free"}
        busy={busy}
        confirmLabel={confirmPlan === "free" ? "Downgrade" : "Continue to checkout"}
        body={
          confirmPlan === "free"
            ? "Switch this account to the Free plan? Paid features stop at the end of the period."
            : "You'll be redirected to Stripe Checkout (test mode) to complete the upgrade."
        }
      />
    </div>
  );
}
