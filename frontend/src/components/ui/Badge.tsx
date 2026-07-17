import type { ReactNode } from "react";

export type BadgeTone =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | "info";

const TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface-3 text-ink-soft border-edge-strong",
  accent: "bg-accent-600/15 text-accent-300 border-accent-600/30",
  success: "bg-success/10 text-success border-success/25",
  warning: "bg-warning/10 text-warning border-warning/25",
  danger: "bg-danger/10 text-danger border-danger/25",
  info: "bg-info/10 text-info border-info/25",
};

export function Badge({
  tone = "neutral",
  dot,
  children,
  className = "",
}: {
  tone?: BadgeTone;
  dot?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-2xs font-medium " +
        `${TONES[tone]} ${className}`
      }
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/** Content lifecycle / job statuses mapped to their semantic tone. */
export function statusTone(status: string | null | undefined): BadgeTone {
  switch (status) {
    case "draft":
      return "neutral";
    case "approved":
    case "connected":
    case "active":
    case "sent":
    case "paid":
    case "open":
      return "info";
    case "scheduled":
    case "pending":
    case "running":
    case "accepted":
      return "warning";
    case "published":
    case "completed":
    case "fired":
    case "succeeded":
    case "sold":
      return "success";
    case "failed":
    case "cancelled":
    case "canceled":
    case "suspended":
    case "disconnected":
    case "past_due":
      return "danger";
    default:
      return "neutral";
  }
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-ink-faint">—</span>;
  return (
    <Badge tone={statusTone(status)} dot>
      {status}
    </Badge>
  );
}
