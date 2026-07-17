export function ProgressBar({
  value,
  max,
  tone = "accent",
}: {
  value: number;
  max: number;
  /** Auto: switches to warning/danger as the meter fills. */
  tone?: "accent" | "auto";
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const color =
    tone === "auto"
      ? pct >= 90
        ? "bg-danger"
        : pct >= 70
          ? "bg-warning"
          : "bg-accent-500"
      : "bg-accent-500";
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-2 w-full overflow-hidden rounded-full bg-surface-3"
    >
      <div
        className={`h-full rounded-full transition-[width] duration-500 ease-out ${color}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
