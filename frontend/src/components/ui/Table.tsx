import type { ReactNode } from "react";
import { Skeleton } from "./Skeleton";

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  align?: "left" | "right";
  className?: string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  loading,
  skeletonRows = 5,
  empty,
  onRowClick,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  loading?: boolean;
  skeletonRows?: number;
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-edge">
            {columns.map((col) => (
              <th
                key={col.key}
                className={
                  "whitespace-nowrap px-3 py-2.5 text-2xs font-semibold uppercase tracking-wider text-ink-faint " +
                  (col.align === "right" ? "text-right " : "text-left ") +
                  (col.className ?? "")
                }
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: skeletonRows }).map((_, i) => (
                <tr key={i} className="border-b border-edge/60">
                  {columns.map((col) => (
                    <td key={col.key} className="px-3 py-3">
                      <Skeleton className="h-3.5 w-full max-w-32" />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={
                    "border-b border-edge/60 transition-colors last:border-0 " +
                    (onRowClick ? "cursor-pointer hover:bg-surface-2/60" : "hover:bg-surface-2/30")
                  }
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={
                        "px-3 py-2.5 text-ink-soft " +
                        (col.align === "right" ? "text-right tnum " : "") +
                        (col.className ?? "")
                      }
                    >
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
      {!loading && rows.length === 0 && empty}
    </div>
  );
}
