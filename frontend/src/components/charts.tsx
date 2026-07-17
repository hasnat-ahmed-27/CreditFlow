/**
 * Shared Recharts chrome for the dark theme: recessive grid/axes, a
 * token-styled tooltip, and the validated series colors. Chart marks stay
 * thin; text wears ink tokens, never the series color.
 */
import type { TooltipProps } from "recharts";

export const SERIES = {
  1: "rgb(118 127 242)",
  2: "rgb(25 158 112)",
  3: "rgb(201 133 0)",
  4: "rgb(213 81 129)",
} as const;

export const CHART_INK = {
  grid: "rgb(35 35 50)",
  axis: "rgb(106 106 128)",
  cursor: "rgb(46 46 66)",
};

export const AXIS_PROPS = {
  stroke: CHART_INK.axis,
  tick: { fill: CHART_INK.axis, fontSize: 11 },
  tickLine: false,
  axisLine: false,
} as const;

export function ChartTooltip({
  active,
  payload,
  label,
  formatter,
}: TooltipProps<number, string> & {
  formatter?: (value: number, name: string) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-field border border-edge-strong bg-surface-2/95 px-3 py-2 text-xs shadow-pop backdrop-blur">
      {label !== undefined && <p className="mb-1 font-medium text-ink">{label}</p>}
      {payload.map((entry) => (
        <p key={entry.dataKey as string} className="flex items-center gap-2 text-ink-soft">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: entry.color ?? SERIES[1] }}
          />
          {formatter
            ? formatter(entry.value as number, entry.name as string)
            : `${entry.name}: ${(entry.value as number).toLocaleString()}`}
        </p>
      ))}
    </div>
  );
}
