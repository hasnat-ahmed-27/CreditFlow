import { useMemo } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, ArrowRight, Coins, DollarSign, Sparkles, Zap } from "lucide-react";
import { AXIS_PROPS, CHART_INK, ChartTooltip, SERIES } from "../components/charts";
import { Badge } from "../components/ui/Badge";
import { Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { PageHeader } from "../components/ui/PageHeader";
import { ProgressBar } from "../components/ui/ProgressBar";
import { Skeleton } from "../components/ui/Skeleton";
import { StatCard } from "../components/ui/StatCard";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { creditsApi, usageApi } from "../lib/api/endpoints";
import type { UsageSummary } from "../lib/api/types";
import {
  formatCompact,
  formatNumber,
  formatRelative,
  formatUsd,
  periodKey,
  periodLabel,
} from "../lib/format";

const HISTORY_MONTHS = 6;

export default function Dashboard() {
  const { claims } = useAuth();

  const balance = useApi(() => creditsApi.balance(), [claims?.account_id]);
  const history = useApi(() => creditsApi.history(8), [claims?.account_id]);

  // Usage for the last N periods — months with no usage may 404/500 on some
  // backends, so each one degrades to null independently.
  const usage = useApi(async () => {
    const periods = Array.from({ length: HISTORY_MONTHS }, (_, i) =>
      periodKey(i - (HISTORY_MONTHS - 1)),
    );
    const results = await Promise.all(
      periods.map((p) => usageApi.summary(p).catch(() => null)),
    );
    return { periods, results };
  }, [claims?.account_id]);

  const current: UsageSummary | null =
    usage.data?.results[HISTORY_MONTHS - 1] ?? null;
  const previous: UsageSummary | null =
    usage.data?.results[HISTORY_MONTHS - 2] ?? null;

  const trendTokens = useMemo(() => {
    if (!current || !previous || previous.used_tokens === 0) return null;
    return ((current.used_tokens - previous.used_tokens) / previous.used_tokens) * 100;
  }, [current, previous]);

  const usageSeries = useMemo(
    () =>
      (usage.data?.periods ?? []).map((period, i) => ({
        period: periodLabel(period),
        tokens: usage.data?.results[i]?.used_tokens ?? 0,
        cost: usage.data?.results[i]?.total_cost_usd ?? 0,
      })),
    [usage.data],
  );

  const byModel = useMemo(
    () =>
      (current?.by_model ?? []).slice(0, 6).map((m) => ({
        model: m.model.split("/").pop() ?? m.model,
        tokens: m.total_tokens,
      })),
    [current],
  );

  const quotaPct =
    current && current.quota_tokens > 0
      ? Math.round((current.used_tokens / current.quota_tokens) * 100)
      : 0;

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Dashboard"
        subtitle="Account overview — credits, usage, and recent activity."
        actions={
          <Link
            to="/generate"
            className="inline-flex h-9 items-center gap-2 rounded-field bg-accent-600 px-4 text-sm font-medium text-white transition-colors hover:bg-accent-500"
          >
            <Sparkles size={15} />
            New generation
          </Link>
        }
      />

      {/* stat row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Credit balance"
          value={balance.data ? formatNumber(balance.data.balance) : "—"}
          sub={
            balance.data && balance.data.balance <= balance.data.low_balance_threshold
              ? "Low balance"
              : "credits available"
          }
          icon={<Coins size={15} />}
          loading={balance.loading}
        />
        <StatCard
          label="Tokens this period"
          value={current ? formatCompact(current.used_tokens) : "0"}
          trend={trendTokens}
          trendLabel="vs last month"
          icon={<Zap size={15} />}
          loading={usage.loading}
        />
        <StatCard
          label="Generations"
          value={current ? formatNumber(current.total_generations) : "0"}
          sub="this period"
          icon={<Activity size={15} />}
          loading={usage.loading}
        />
        <StatCard
          label="Spend"
          value={current ? formatUsd(current.total_cost_usd) : "$0.00"}
          sub="this period"
          icon={<DollarSign size={15} />}
          loading={usage.loading}
        />
      </div>

      {/* quota meter */}
      <Card className="mt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-ink">Usage this period</h3>
            <p className="mt-0.5 text-xs text-ink-faint">
              {current
                ? `${formatNumber(current.used_tokens)} of ${formatNumber(current.quota_tokens)} tokens`
                : "No usage recorded yet"}
            </p>
          </div>
          <Badge tone={quotaPct >= 90 ? "danger" : quotaPct >= 70 ? "warning" : "accent"}>
            {quotaPct}% of quota
          </Badge>
        </div>
        <div className="mt-3">
          {usage.loading ? (
            <Skeleton className="h-2 w-full" />
          ) : (
            <ProgressBar
              value={current?.used_tokens ?? 0}
              max={current?.quota_tokens ?? 1}
              tone="auto"
            />
          )}
        </div>
      </Card>

      {/* charts */}
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader
            title="Token usage"
            subtitle={`Monthly total, last ${HISTORY_MONTHS} months`}
          />
          {usage.loading ? (
            <Skeleton className="h-56 w-full" />
          ) : usage.error ? (
            <ErrorState error={usage.error} onRetry={usage.reload} />
          ) : (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={usageSeries} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
                  <defs>
                    <linearGradient id="tokensFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={SERIES[1]} stopOpacity={0.28} />
                      <stop offset="100%" stopColor={SERIES[1]} stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke={CHART_INK.grid} strokeDasharray="0" vertical={false} />
                  <XAxis dataKey="period" {...AXIS_PROPS} />
                  <YAxis {...AXIS_PROPS} tickFormatter={(v: number) => formatCompact(v)} width={52} />
                  <Tooltip
                    cursor={{ stroke: CHART_INK.cursor, strokeWidth: 1 }}
                    content={
                      <ChartTooltip
                        formatter={(v) => `${formatNumber(v)} tokens`}
                      />
                    }
                  />
                  <Area
                    type="monotone"
                    dataKey="tokens"
                    name="Tokens"
                    stroke={SERIES[1]}
                    strokeWidth={2}
                    fill="url(#tokensFill)"
                    dot={false}
                    activeDot={{ r: 4, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader title="Tokens by model" subtitle="This period" />
          {usage.loading ? (
            <Skeleton className="h-56 w-full" />
          ) : byModel.length === 0 ? (
            <EmptyState
              icon={<Sparkles size={18} />}
              title="No generations yet"
              body="Run your first generation in the AI Studio and per-model usage will appear here."
              action={
                <Link
                  to="/generate"
                  className="inline-flex items-center gap-1 text-xs font-medium text-accent-300 hover:text-accent-400"
                >
                  Open AI Studio <ArrowRight size={13} />
                </Link>
              }
            />
          ) : (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={byModel}
                  layout="vertical"
                  margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
                >
                  <CartesianGrid stroke={CHART_INK.grid} horizontal={false} />
                  <XAxis type="number" {...AXIS_PROPS} tickFormatter={(v: number) => formatCompact(v)} />
                  <YAxis
                    type="category"
                    dataKey="model"
                    {...AXIS_PROPS}
                    width={110}
                    tick={{ fill: "rgb(160 160 180)", fontSize: 11 }}
                  />
                  <Tooltip
                    cursor={{ fill: "rgb(24 24 36 / 0.6)" }}
                    content={<ChartTooltip formatter={(v) => `${formatNumber(v)} tokens`} />}
                  />
                  <Bar
                    dataKey="tokens"
                    name="Tokens"
                    fill={SERIES[1]}
                    radius={[0, 4, 4, 0]}
                    barSize={14}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
      </div>

      {/* recent activity */}
      <Card className="mt-4">
        <CardHeader
          title="Recent activity"
          subtitle="Latest credit movements"
          actions={
            <Link
              to="/credits"
              className="inline-flex items-center gap-1 text-xs font-medium text-accent-300 transition-colors hover:text-accent-400"
            >
              View all <ArrowRight size={13} />
            </Link>
          }
        />
        {history.loading ? (
          <div className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : history.error ? (
          <ErrorState error={history.error} onRetry={history.reload} />
        ) : history.data && history.data.entries.length > 0 ? (
          <ul className="divide-y divide-edge/60">
            {history.data.entries.slice(0, 6).map((entry) => (
              <li key={entry.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="flex min-w-0 items-center gap-3">
                  <span
                    className={
                      "flex h-7 w-7 shrink-0 items-center justify-center rounded-full " +
                      (entry.amount > 0 ? "bg-success/10 text-success" : "bg-surface-3 text-ink-faint")
                    }
                  >
                    <Coins size={13} />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-[13px] text-ink">
                      {entry.reason ?? entry.entry_type.replace(/_/g, " ")}
                    </p>
                    <p className="text-2xs text-ink-faint">{formatRelative(entry.created_at)}</p>
                  </div>
                </div>
                <span
                  className={
                    "shrink-0 text-[13px] font-semibold tnum " +
                    (entry.amount > 0 ? "text-success" : "text-ink-soft")
                  }
                >
                  {entry.amount > 0 ? "+" : ""}
                  {formatNumber(entry.amount)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState
            icon={<Coins size={18} />}
            title="No activity yet"
            body="Credit purchases, transfers, and AI usage will show up here."
          />
        )}
      </Card>
    </div>
  );
}
