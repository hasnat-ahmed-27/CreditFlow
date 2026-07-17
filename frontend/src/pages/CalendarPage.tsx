import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Plus,
  Repeat,
} from "lucide-react";
import { StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input, Select } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { contentApi, schedulerApi } from "../lib/api/endpoints";
import type { Schedule } from "../lib/api/types";
import { formatDateTime, shortId, truncate } from "../lib/format";

const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const RECURRENCES = ["", "daily", "weekly", "monthly"] as const;

function monthBounds(anchor: Date): { start: Date; end: Date } {
  const start = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const end = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1);
  return { start, end };
}

function toNaiveIso(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
}

export default function CalendarPage() {
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();
  const [anchor, setAnchor] = useState(() => new Date());
  const [createOpen, setCreateOpen] = useState(params.get("content") !== null);
  const [selected, setSelected] = useState<Schedule | null>(null);
  const [cancelling, setCancelling] = useState<Schedule | null>(null);
  const [busy, setBusy] = useState(false);

  const { start, end } = monthBounds(anchor);

  // The calendar query bounds are exclusive-end UTC datetimes.
  const calendar = useApi(
    () => schedulerApi.calendar(start.toISOString(), end.toISOString()),
    [start.getTime(), end.getTime()],
  );

  const byDay = useMemo(() => {
    const map = new Map<string, Schedule[]>();
    for (const item of calendar.data?.items ?? []) {
      const local = new Date(item.publish_at);
      const key = `${local.getFullYear()}-${local.getMonth()}-${local.getDate()}`;
      map.set(key, [...(map.get(key) ?? []), item]);
    }
    return map;
  }, [calendar.data]);

  // Build the 6x7 grid: pad from the previous month to the first Sunday.
  const cells = useMemo(() => {
    const firstDay = new Date(start);
    firstDay.setDate(firstDay.getDate() - firstDay.getDay());
    return Array.from({ length: 42 }, (_, i) => {
      const date = new Date(firstDay);
      date.setDate(firstDay.getDate() + i);
      return date;
    });
  }, [start]);

  const today = new Date();

  async function cancelSchedule() {
    if (!cancelling) return;
    setBusy(true);
    try {
      await schedulerApi.cancel(cancelling.schedule_id);
      toast("success", "Schedule cancelled");
      setCancelling(null);
      setSelected(null);
      calendar.reload();
    } catch (err) {
      toast("error", "Cancel failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Calendar"
        subtitle={`Scheduled and recurring posts, shown in ${LOCAL_TZ}.`}
        actions={
          <Button icon={<Plus size={15} />} onClick={() => setCreateOpen(true)}>
            Schedule a post
          </Button>
        }
      />

      <Card padded={false}>
        {/* month switcher */}
        <div className="flex items-center justify-between border-b border-edge px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">
            {anchor.toLocaleDateString("en-US", { month: "long", year: "numeric" })}
          </h2>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              aria-label="Previous month"
              onClick={() => setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() - 1, 1))}
            >
              <ChevronLeft size={15} />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setAnchor(new Date())}>
              Today
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-label="Next month"
              onClick={() => setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1))}
            >
              <ChevronRight size={15} />
            </Button>
          </div>
        </div>

        {calendar.error ? (
          <ErrorState error={calendar.error} onRetry={calendar.reload} />
        ) : (
          <>
            <div className="grid grid-cols-7 border-b border-edge">
              {WEEKDAYS.map((day) => (
                <div
                  key={day}
                  className="px-2 py-1.5 text-center text-2xs font-semibold uppercase tracking-wider text-ink-faint"
                >
                  {day}
                </div>
              ))}
            </div>
            <div className="grid grid-cols-7">
              {cells.map((date, i) => {
                const inMonth = date.getMonth() === anchor.getMonth();
                const isToday =
                  date.getDate() === today.getDate() &&
                  date.getMonth() === today.getMonth() &&
                  date.getFullYear() === today.getFullYear();
                const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
                const events = byDay.get(key) ?? [];
                return (
                  <div
                    key={i}
                    className={
                      "min-h-[5.5rem] border-b border-r border-edge/60 p-1.5 [&:nth-child(7n)]:border-r-0 " +
                      (inMonth ? "" : "bg-surface-2/30 opacity-50")
                    }
                  >
                    <span
                      className={
                        "inline-flex h-5 w-5 items-center justify-center rounded-full text-2xs tnum " +
                        (isToday ? "bg-accent-600 font-semibold text-white" : "text-ink-faint")
                      }
                    >
                      {date.getDate()}
                    </span>
                    <div className="mt-1 space-y-1">
                      {calendar.loading && inMonth && i % 9 === 3 ? (
                        <Skeleton className="h-4 w-full" />
                      ) : (
                        events.slice(0, 3).map((event) => (
                          <button
                            key={event.schedule_id}
                            onClick={() => setSelected(event)}
                            className={
                              "flex w-full items-center gap-1 truncate rounded px-1.5 py-0.5 text-left text-2xs font-medium transition-colors " +
                              (event.status === "pending"
                                ? "bg-accent-600/20 text-accent-300 hover:bg-accent-600/30"
                                : event.status === "fired"
                                  ? "bg-success/10 text-success hover:bg-success/20"
                                  : "bg-surface-3 text-ink-faint line-through hover:bg-surface-2")
                            }
                          >
                            {event.recurrence && <Repeat size={9} className="shrink-0" />}
                            <span className="truncate">
                              {new Date(event.publish_at).toLocaleTimeString("en-US", {
                                hour: "numeric",
                                minute: "2-digit",
                              })}
                            </span>
                          </button>
                        ))
                      )}
                      {events.length > 3 && (
                        <p className="px-1.5 text-2xs text-ink-faint">+{events.length - 3} more</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </Card>

      {!calendar.loading && !calendar.error && (calendar.data?.items.length ?? 0) === 0 && (
        <Card className="mt-4">
          <EmptyState
            icon={<CalendarClock size={18} />}
            title="Nothing scheduled this month"
            body="Approve a content draft, then schedule it for a publish date — one-off or recurring."
            action={
              <Button size="sm" variant="secondary" icon={<Plus size={13} />} onClick={() => setCreateOpen(true)}>
                Schedule a post
              </Button>
            }
          />
        </Card>
      )}

      <CreateScheduleModal
        open={createOpen}
        preselectedContent={params.get("content")}
        onClose={() => {
          setCreateOpen(false);
          if (params.get("content")) setParams({}, { replace: true });
        }}
        onCreated={() => {
          setCreateOpen(false);
          if (params.get("content")) setParams({}, { replace: true });
          calendar.reload();
        }}
      />

      {/* detail modal */}
      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title="Scheduled post"
        footer={
          selected?.status === "pending" ? (
            <Button variant="danger" onClick={() => setCancelling(selected)}>
              Cancel schedule
            </Button>
          ) : undefined
        }
      >
        {selected && (
          <div className="space-y-3 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-ink-faint">Status</span>
              <StatusBadge status={selected.status} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-faint">Publishes</span>
              <span className="text-ink">{formatDateTime(selected.publish_at)}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-faint">Recurrence</span>
              <span className="capitalize text-ink">{selected.recurrence ?? "one-off"}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-faint">Fired</span>
              <span className="text-ink tnum">
                {selected.fire_count}×
                {selected.last_fired_at ? ` · last ${formatDateTime(selected.last_fired_at)}` : ""}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-faint">Content</span>
              <span className="font-mono text-xs text-ink-soft">{shortId(selected.content_id, 12)}</span>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        onConfirm={() => void cancelSchedule()}
        title="Cancel scheduled post"
        busy={busy}
        confirmLabel="Cancel schedule"
        body="This post won't publish. The content item itself stays in your library."
      />
    </div>
  );
}

// ---- create --------------------------------------------------------------

function CreateScheduleModal({
  open,
  preselectedContent,
  onClose,
  onCreated,
}: {
  open: boolean;
  preselectedContent: string | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { toast } = useToast();
  // Only approved (or already scheduled) content can be placed on the calendar.
  const approved = useApi(
    () => (open ? contentApi.list({ status: "approved", limit: 100 }) : Promise.resolve(null)),
    [open],
  );
  const [contentId, setContentId] = useState(preselectedContent ?? "");
  const [when, setWhen] = useState(() => {
    const soon = new Date(Date.now() + 60 * 60 * 1000);
    soon.setMinutes(0, 0, 0);
    return toNaiveIso(soon).slice(0, 16);
  });
  const [recurrence, setRecurrence] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const options = approved.data?.items ?? [];
  const effectiveContent = contentId || preselectedContent || options[0]?.content_id || "";

  async function submit() {
    if (!effectiveContent) {
      toast("warning", "Pick a content item", "Only approved content can be scheduled.");
      return;
    }
    setBusy(true);
    try {
      await schedulerApi.create({
        content_id: effectiveContent,
        publish_at: `${when}:00`,
        timezone: LOCAL_TZ,
        recurrence: recurrence || null,
      });
      toast("success", "Post scheduled");
      onCreated();
    } catch (err) {
      toast("error", "Scheduling failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Schedule a post"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={busy}>
            Schedule
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field
          label="Content"
          hint={
            options.length === 0 && !approved.loading
              ? "No approved content yet — approve a draft first."
              : "Only approved content is schedulable."
          }
        >
          <Select value={effectiveContent} onChange={(e) => setContentId(e.target.value)}>
            {approved.loading && <option>Loading…</option>}
            {preselectedContent && !options.some((o) => o.content_id === preselectedContent) && (
              <option value={preselectedContent}>{shortId(preselectedContent, 12)} (selected)</option>
            )}
            {options.map((item) => (
              <option key={item.content_id} value={item.content_id}>
                {truncate(item.title, 60)}
              </option>
            ))}
          </Select>
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Publish at" hint={LOCAL_TZ}>
            <Input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
          </Field>
          <Field label="Repeat">
            <Select value={recurrence} onChange={(e) => setRecurrence(e.target.value)}>
              {RECURRENCES.map((r) => (
                <option key={r} value={r}>
                  {r === "" ? "One-off" : r.charAt(0).toUpperCase() + r.slice(1)}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </div>
    </Modal>
  );
}
