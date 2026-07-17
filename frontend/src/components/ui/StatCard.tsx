import type { ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { Card } from "./Card";
import { Skeleton } from "./Skeleton";

export function StatCard({
  label,
  value,
  sub,
  icon,
  trend,
  trendLabel,
  loading,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: ReactNode;
  /** Percent delta vs. previous period; sign picks the arrow + color. */
  trend?: number | null;
  trendLabel?: string;
  loading?: boolean;
}) {
  return (
    <Card className="relative overflow-hidden">
      <div className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-accent-600/[0.07] blur-2xl" />
      <div className="flex items-start justify-between">
        <p className="text-xs font-medium text-ink-faint">{label}</p>
        {icon && <span className="text-ink-faint">{icon}</span>}
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-8 w-24" />
      ) : (
        <p className="mt-1.5 text-2xl font-semibold tracking-tight text-ink">{value}</p>
      )}
      <div className="mt-1.5 flex items-center gap-2 text-xs">
        {trend !== undefined && trend !== null && !loading && <TrendChip value={trend} />}
        {(sub || trendLabel) && (
          <span className="text-ink-faint">{sub ?? trendLabel}</span>
        )}
      </div>
    </Card>
  );
}

function TrendChip({ value }: { value: number }) {
  const Icon = value > 0 ? ArrowUpRight : value < 0 ? ArrowDownRight : Minus;
  const color =
    value > 0 ? "text-success bg-success/10" : value < 0 ? "text-danger bg-danger/10" : "text-ink-faint bg-surface-3";
  return (
    <span className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 font-medium tnum ${color}`}>
      <Icon size={12} />
      {Math.abs(value).toFixed(1)}%
    </span>
  );
}
