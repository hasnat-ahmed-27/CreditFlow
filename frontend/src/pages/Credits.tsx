import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Coins, Plus, ShoppingCart, Store, Tag } from "lucide-react";
import { AXIS_PROPS, CHART_INK, ChartTooltip, SERIES } from "../components/charts";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { Table, type Column } from "../components/ui/Table";
import { Tabs } from "../components/ui/Tabs";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { creditsApi } from "../lib/api/endpoints";
import type { LedgerEntry, MarketplaceListing } from "../lib/api/types";
import {
  formatCents,
  formatDateTime,
  formatNumber,
  shortId,
} from "../lib/format";

export default function Credits() {
  const [tab, setTab] = useState("ledger");
  const history = useApi(() => creditsApi.history(200), []);

  const balanceSeries = useMemo(() => {
    if (!history.data) return [];
    // Rebuild the running balance from newest-first ledger entries.
    const entries = [...history.data.entries].reverse();
    let running = history.data.balance - entries.reduce((sum, e) => sum + e.amount, 0);
    return entries.slice(-30).map((entry) => {
      running += entry.amount;
      return {
        at: formatDateTime(entry.created_at),
        balance: running,
      };
    });
  }, [history.data]);

  return (
    <div className="animate-fade-up">
      <PageHeader title="Credits" subtitle="Account balance, ledger history, and the marketplace." />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card glow className="flex flex-col justify-between">
          <div>
            <p className="text-xs font-medium text-ink-faint">Current balance</p>
            {history.loading ? (
              <Skeleton className="mt-2 h-10 w-32" />
            ) : (
              <p className="mt-1 text-4xl font-semibold tracking-tight text-ink tnum">
                {history.data ? formatNumber(history.data.balance) : "—"}
              </p>
            )}
            <p className="mt-1 text-xs text-ink-faint">credits</p>
          </div>
          <p className="mt-4 text-2xs leading-relaxed text-ink-faint">
            Credits are shared by everyone in this account and spent by AI generations.
          </p>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader title="Balance over time" subtitle="Last 30 ledger movements" />
          {history.loading ? (
            <Skeleton className="h-40 w-full" />
          ) : history.error ? (
            <ErrorState error={history.error} onRetry={history.reload} />
          ) : balanceSeries.length === 0 ? (
            <EmptyState
              icon={<Coins size={18} />}
              title="No movements yet"
              body="Purchases and AI usage will draw this chart."
            />
          ) : (
            <div className="h-40">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={balanceSeries} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
                  <defs>
                    <linearGradient id="balanceFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={SERIES[1]} stopOpacity={0.25} />
                      <stop offset="100%" stopColor={SERIES[1]} stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="at" {...AXIS_PROPS} hide />
                  <YAxis {...AXIS_PROPS} width={52} tickFormatter={(v: number) => formatNumber(v)} />
                  <Tooltip
                    cursor={{ stroke: CHART_INK.cursor, strokeWidth: 1 }}
                    content={<ChartTooltip formatter={(v) => `${formatNumber(v)} credits`} />}
                  />
                  <Area
                    type="stepAfter"
                    dataKey="balance"
                    name="Balance"
                    stroke={SERIES[1]}
                    strokeWidth={2}
                    fill="url(#balanceFill)"
                    dot={false}
                    activeDot={{ r: 4, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
      </div>

      <div className="mt-6">
        <Tabs
          tabs={[
            { id: "ledger", label: "Transaction history", count: history.data?.entries.length },
            { id: "marketplace", label: "Marketplace" },
          ]}
          active={tab}
          onChange={setTab}
        />
        <div className="mt-4">
          {tab === "ledger" ? <LedgerTable history={history} /> : <Marketplace />}
        </div>
      </div>
    </div>
  );
}

// ---- ledger --------------------------------------------------------------

function LedgerTable({ history }: { history: ReturnType<typeof useApi<Awaited<ReturnType<typeof creditsApi.history>>>> }) {
  const columns: Column<LedgerEntry>[] = [
    {
      key: "when",
      header: "When",
      render: (e) => <span className="whitespace-nowrap">{formatDateTime(e.created_at)}</span>,
    },
    {
      key: "type",
      header: "Type",
      render: (e) => <Badge tone={e.amount > 0 ? "success" : "neutral"}>{e.entry_type.replace(/_/g, " ")}</Badge>,
    },
    {
      key: "reason",
      header: "Detail",
      render: (e) => (
        <span className="text-ink-soft">
          {e.reason ??
            (e.counterparty_account_id
              ? `with account ${shortId(e.counterparty_account_id)}`
              : e.stripe_ref
                ? `Stripe ${shortId(e.stripe_ref, 14)}`
                : "—")}
        </span>
      ),
    },
    {
      key: "money",
      header: "Paid",
      align: "right",
      render: (e) => <span>{e.money_amount_cents !== null ? formatCents(e.money_amount_cents) : "—"}</span>,
    },
    {
      key: "amount",
      header: "Credits",
      align: "right",
      render: (e) => (
        <span className={"font-semibold " + (e.amount > 0 ? "text-success" : "text-ink")}>
          {e.amount > 0 ? "+" : ""}
          {formatNumber(e.amount)}
        </span>
      ),
    },
  ];

  if (history.error) return <Card><ErrorState error={history.error} onRetry={history.reload} /></Card>;

  return (
    <Card padded={false} className="p-2">
      <Table
        columns={columns}
        rows={history.data?.entries ?? []}
        rowKey={(e) => e.id}
        loading={history.loading}
        empty={
          <EmptyState
            icon={<Coins size={18} />}
            title="No transactions yet"
            body="Buy credits from the Billing screen or via the marketplace, and every movement lands in this ledger."
          />
        }
      />
    </Card>
  );
}

// ---- marketplace ---------------------------------------------------------

function Marketplace() {
  const { claims, role } = useAuth();
  const { toast } = useToast();
  const listings = useApi(() => creditsApi.listings(), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [buying, setBuying] = useState<MarketplaceListing | null>(null);
  const [cancelling, setCancelling] = useState<MarketplaceListing | null>(null);
  const [busy, setBusy] = useState(false);

  const isOwner = role === "owner";

  async function purchase() {
    if (!buying) return;
    setBusy(true);
    try {
      await creditsApi.purchaseListing(buying.listing_id);
      toast("success", "Credits purchased", `${formatNumber(buying.credits_amount)} credits added to your balance.`);
      setBuying(null);
      listings.reload();
    } catch (err) {
      toast("error", "Purchase failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!cancelling) return;
    setBusy(true);
    try {
      await creditsApi.cancelListing(cancelling.listing_id);
      toast("success", "Listing cancelled");
      setCancelling(null);
      listings.reload();
    } catch (err) {
      toast("error", "Cancel failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="mb-4 flex items-center justify-between">
        <p className="text-xs text-ink-faint">
          Open listings from every account on the platform. Buying and selling is owner-only.
        </p>
        {isOwner && (
          <Button size="sm" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>
            List credits
          </Button>
        )}
      </div>

      {listings.error ? (
        <Card><ErrorState error={listings.error} onRetry={listings.reload} /></Card>
      ) : listings.loading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-36 w-full rounded-card" />
          ))}
        </div>
      ) : (listings.data?.listings.length ?? 0) === 0 ? (
        <Card>
          <EmptyState
            icon={<Store size={18} />}
            title="No open listings"
            body="When an account lists surplus credits for sale, they show up here for anyone to buy."
            action={
              isOwner && (
                <Button size="sm" variant="secondary" icon={<Plus size={13} />} onClick={() => setCreateOpen(true)}>
                  Be the first to list
                </Button>
              )
            }
          />
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {listings.data!.listings.map((listing) => {
            const mine = listing.seller_account_id === claims?.account_id;
            return (
              <Card key={listing.listing_id} className="flex flex-col justify-between">
                <div>
                  <div className="flex items-start justify-between">
                    <span className="flex h-9 w-9 items-center justify-center rounded-full bg-warning/10 text-warning">
                      <Coins size={16} />
                    </span>
                    {mine ? <Badge tone="accent">your listing</Badge> : <StatusBadge status={listing.status} />}
                  </div>
                  <p className="mt-3 text-2xl font-semibold tracking-tight text-ink tnum">
                    {formatNumber(listing.credits_amount)}
                    <span className="ml-1.5 text-xs font-normal text-ink-faint">credits</span>
                  </p>
                  <p className="mt-1 flex items-center gap-1.5 text-xs text-ink-faint">
                    <Tag size={12} />
                    {formatCents(listing.price_cents)}
                    <span className="text-ink-faint/70">
                      · seller {shortId(listing.seller_account_id)}
                    </span>
                  </p>
                </div>
                <div className="mt-4">
                  {mine ? (
                    <Button variant="danger" size="sm" className="w-full" onClick={() => setCancelling(listing)}>
                      Cancel listing
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      className="w-full"
                      icon={<ShoppingCart size={13} />}
                      disabled={!isOwner}
                      title={isOwner ? undefined : "Buying is owner-only"}
                      onClick={() => setBuying(listing)}
                    >
                      Buy
                    </Button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <CreateListingModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false);
          listings.reload();
        }}
      />

      <ConfirmDialog
        open={buying !== null}
        onClose={() => setBuying(null)}
        onConfirm={() => void purchase()}
        title="Buy credits"
        danger={false}
        busy={busy}
        confirmLabel={buying ? `Buy for ${formatCents(buying.price_cents)}` : "Buy"}
        body={
          buying
            ? `Purchase ${formatNumber(buying.credits_amount)} credits from account ${shortId(buying.seller_account_id)} for ${formatCents(buying.price_cents)}? Credits transfer to this account immediately.`
            : ""
        }
      />

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        onConfirm={() => void cancel()}
        title="Cancel listing"
        busy={busy}
        confirmLabel="Cancel listing"
        body={
          cancelling
            ? `Take the ${formatNumber(cancelling.credits_amount)}-credit listing off the marketplace? The credits stay in your balance.`
            : ""
        }
      />
    </>
  );
}

function CreateListingModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { toast } = useToast();
  const [amount, setAmount] = useState("100");
  const [price, setPrice] = useState("5.00");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const credits = parseInt(amount, 10);
    const cents = Math.round(parseFloat(price) * 100);
    if (!Number.isFinite(credits) || credits <= 0 || !Number.isFinite(cents) || cents <= 0) {
      toast("warning", "Enter a valid amount and price");
      return;
    }
    setBusy(true);
    try {
      await creditsApi.createListing(credits, cents);
      toast("success", "Listing created", "Your credits are now on the marketplace.");
      onCreated();
    } catch (err) {
      toast("error", "Could not create listing", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="List credits for sale"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={busy}>
            Create listing
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Credits to sell" hint="Must be covered by your current balance.">
          <Input type="number" min={1} value={amount} onChange={(e) => setAmount(e.target.value)} />
        </Field>
        <Field label="Asking price (USD)">
          <Input type="number" min={0.01} step={0.01} value={price} onChange={(e) => setPrice(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}
