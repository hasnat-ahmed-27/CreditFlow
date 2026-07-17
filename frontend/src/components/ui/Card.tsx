import type { HTMLAttributes, ReactNode } from "react";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Adds an accent glow ring — reserve for the single hero element on a page. */
  glow?: boolean;
  padded?: boolean;
}

export function Card({ glow, padded = true, className = "", children, ...rest }: CardProps) {
  return (
    <div
      className={
        "rounded-card border bg-surface shadow-card " +
        (glow ? "border-accent-600/40 shadow-glow-accent " : "border-edge ") +
        (padded ? "p-5 " : "") +
        className
      }
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-start justify-between gap-3">
      <div>
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {subtitle && <p className="mt-0.5 text-xs text-ink-faint">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
