import type { ReactNode } from "react";

export interface TabDef {
  id: string;
  label: ReactNode;
  count?: number;
}

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: TabDef[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div role="tablist" className="flex gap-1 border-b border-edge">
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(tab.id)}
            className={
              "-mb-px inline-flex items-center gap-2 border-b-2 px-3.5 py-2.5 text-sm font-medium transition-colors " +
              (selected
                ? "border-accent-500 text-ink"
                : "border-transparent text-ink-faint hover:border-edge-strong hover:text-ink-soft")
            }
          >
            {tab.label}
            {tab.count !== undefined && (
              <span
                className={
                  "rounded-full px-1.5 py-0.5 text-2xs tnum " +
                  (selected ? "bg-accent-600/20 text-accent-300" : "bg-surface-3 text-ink-faint")
                }
              >
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
