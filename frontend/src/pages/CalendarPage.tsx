import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import timeGridPlugin from "@fullcalendar/timegrid";
import type {
  DateSelectArg,
  EventClickArg,
  EventDropArg,
  EventInput,
} from "@fullcalendar/core";
import { CalendarClock, Clock, Plus, Repeat } from "lucide-react";
import { StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input, Select } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { useApi } from "../hooks/useApi";
import { MANAGER_ROLES, hasRole, useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { contentApi, schedulerApi } from "../lib/api/endpoints";
import type { Schedule } from "../lib/api/types";
import { formatDateTime, shortId, truncate } from "../lib/format";
import "../theme/fullcalendar.css";

const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const RECURRENCES = ["", "daily", "weekly", "monthly"] as const;

/** `datetime-local` inputs and the scheduler's create/patch payloads both want
 *  a naive wall-clock string; the IANA zone travels beside it. */
function toNaiveIso(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
}

/**
 * Content calendar, built on FullCalendar (spec §3 requires FullCalendar or
 * React Big Calendar rather than a hand-rolled grid).
 *
 * The library owns the month/week grids, the date arithmetic and drag
 * interaction; this component owns the data contract with the Scheduler
 * service. Two seams matter:
 *
 *   - `datesSet` reports the visible range whenever the user changes view or
 *     navigates, and that range IS the query window for
 *     GET /schedules/calendar. So the week view isn't a client-side filter of
 *     a month's worth of rows — each view fetches exactly its own span.
 *   - `publish_at` comes back as an absolute UTC instant, so FullCalendar
 *     renders it in the viewer's local zone (the subtitle names which one).
 *     Writes go the other way: a naive wall-clock string plus LOCAL_TZ, which
 *     is what POST/PATCH /schedules expect.
 *
 * Mutating the calendar is owner/admin at the Gateway
 * (services/gateway/security.py), so a member gets a read-only calendar
 * rather than buttons that 403.
 */
export default function CalendarPage() {
  const { toast } = useToast();
  const { role } = useAuth();
  const [params, setParams] = useSearchParams();
  const calendarRef = useRef<FullCalendar | null>(null);

  const canEdit = hasRole(role, MANAGER_ROLES);

  const [range, setRange] = useState<{ start: string; end: string } | null>(null);
  const [createOpen, setCreateOpen] = useState(params.get("content") !== null);
  const [createAt, setCreateAt] = useState<Date | null>(null);
  const [selected, setSelected] = useState<Schedule | null>(null);
  const [cancelling, setCancelling] = useState<Schedule | null>(null);
  const [rescheduling, setRescheduling] = useState<{ schedule: Schedule; to: Date } | null>(null);
  const [busy, setBusy] = useState(false);
  // FullCalendar has already moved the event optimistically by the time
  // eventDrop fires; this is how we put it back if the move isn't confirmed.
  const pendingRevert = useRef<(() => void) | null>(null);

  const calendar = useApi(
    () =>
      range
        ? schedulerApi.calendar(range.start, range.end)
        : Promise.resolve(null),
    [range?.start, range?.end],
  );

  const schedules = useMemo(
    () => new Map((calendar.data?.items ?? []).map((s) => [s.schedule_id, s])),
    [calendar.data],
  );

  const events = useMemo<EventInput[]>(
    () =>
      (calendar.data?.items ?? []).map((schedule) => ({
        id: schedule.schedule_id,
        start: schedule.publish_at,
        // No end time — a publish is an instant, not a span. `allDay: false`
        // keeps it out of the week view's all-day rail.
        allDay: false,
        title: schedule.recurrence
          ? `↻ ${schedule.recurrence}`
          : shortId(schedule.content_id, 8),
        // Only pending schedules can move; a fired or cancelled one is history.
        editable: canEdit && schedule.status === "pending",
        classNames: [`cf-event-${schedule.status}`],
      })),
    [calendar.data, canEdit],
  );

  // FullCalendar hands back the rendered span (which overshoots the month into
  // the padding days) — exactly the window we want rows for.
  const onDatesSet = useCallback((arg: { start: Date; end: Date }) => {
    const start = arg.start.toISOString();
    const end = arg.end.toISOString();
    setRange((prev) => (prev?.start === start && prev?.end === end ? prev : { start, end }));
  }, []);

  function onEventClick(arg: EventClickArg) {
    const schedule = schedules.get(arg.event.id);
    if (schedule) setSelected(schedule);
  }

  /** Drag-and-drop reschedule. Confirmed before it's committed, and reverted
   *  if the user backs out — the grid must never show a time the server
   *  didn't accept. */
  function onEventDrop(arg: EventDropArg) {
    const schedule = schedules.get(arg.event.id);
    if (!schedule || !arg.event.start) {
      arg.revert();
      return;
    }
    setRescheduling({ schedule, to: arg.event.start });
    pendingRevert.current = arg.revert;
  }

  function onSelect(arg: DateSelectArg) {
    if (!canEdit) return;
    setCreateAt(arg.start);
    setCreateOpen(true);
    calendarRef.current?.getApi().unselect();
  }

  async function applyReschedule() {
    if (!rescheduling) return;
    setBusy(true);
    try {
      await schedulerApi.update(rescheduling.schedule.schedule_id, {
        publish_at: toNaiveIso(rescheduling.to),
        timezone: LOCAL_TZ,
      });
      toast("success", "Schedule moved", formatDateTime(rescheduling.to.toISOString()));
      pendingRevert.current = null;
      setRescheduling(null);
      setSelected(null);
      calendar.reload();
    } catch (err) {
      pendingRevert.current?.();
      pendingRevert.current = null;
      setRescheduling(null);
      toast("error", "Reschedule failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  function abandonReschedule() {
    pendingRevert.current?.();
    pendingRevert.current = null;
    setRescheduling(null);
  }

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

  // `range` is null until FullCalendar reports its first view, and the query
  // is skipped until then — without this guard the empty state would flash
  // before the first fetch had even been issued.
  const isEmpty =
    range !== null &&
    !calendar.loading &&
    !calendar.error &&
    (calendar.data?.items.length ?? 0) === 0;

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Calendar"
        subtitle={
          canEdit
            ? `Scheduled and recurring posts, shown in ${LOCAL_TZ}. Drag a pending post to reschedule it.`
            : `Scheduled and recurring posts, shown in ${LOCAL_TZ}.`
        }
        actions={
          canEdit && (
            <Button
              icon={<Plus size={15} />}
              onClick={() => {
                setCreateAt(null);
                setCreateOpen(true);
              }}
            >
              Schedule a post
            </Button>
          )
        }
      />

      <Card padded={false} className="overflow-hidden">
        {calendar.error ? (
          <ErrorState error={calendar.error} onRetry={calendar.reload} />
        ) : (
          <div className="cf-calendar">
            <FullCalendar
              ref={calendarRef}
              plugins={[dayGridPlugin, timeGridPlugin, interactionPlugin]}
              initialView="dayGridMonth"
              headerToolbar={{
                left: "prev,next today",
                center: "title",
                right: "dayGridMonth,timeGridWeek",
              }}
              buttonText={{ today: "Today", month: "Month", week: "Week" }}
              // "local" renders the UTC instants the API returns in the
              // viewer's own zone, which is what the subtitle promises.
              timeZone="local"
              height="auto"
              expandRows
              nowIndicator
              dayMaxEvents={3}
              firstDay={0}
              slotDuration="01:00:00"
              scrollTime="08:00:00"
              eventTimeFormat={{ hour: "numeric", minute: "2-digit", meridiem: "short" }}
              events={events}
              datesSet={onDatesSet}
              eventClick={onEventClick}
              editable={canEdit}
              eventStartEditable={canEdit}
              eventDurationEditable={false}
              eventDrop={onEventDrop}
              selectable={canEdit}
              select={onSelect}
              selectMirror
            />
          </div>
        )}
      </Card>

      {isEmpty && (
        <Card className="mt-4">
          <EmptyState
            icon={<CalendarClock size={18} />}
            title="Nothing scheduled in this view"
            body={
              canEdit
                ? "Approve a content draft, then schedule it for a publish date — one-off or recurring."
                : "Once an owner or admin schedules approved content, it shows up here."
            }
            action={
              canEdit && (
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Plus size={13} />}
                  onClick={() => {
                    setCreateAt(null);
                    setCreateOpen(true);
                  }}
                >
                  Schedule a post
                </Button>
              )
            }
          />
        </Card>
      )}

      <CreateScheduleModal
        open={createOpen}
        preselectedContent={params.get("content")}
        initialWhen={createAt}
        onClose={() => {
          setCreateOpen(false);
          setCreateAt(null);
          if (params.get("content")) setParams({}, { replace: true });
        }}
        onCreated={() => {
          setCreateOpen(false);
          setCreateAt(null);
          if (params.get("content")) setParams({}, { replace: true });
          calendar.reload();
        }}
      />

      {/* detail */}
      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title="Scheduled post"
        footer={
          canEdit && selected?.status === "pending" ? (
            <Button variant="danger" onClick={() => setCancelling(selected)}>
              Cancel schedule
            </Button>
          ) : undefined
        }
      >
        {selected && (
          <div className="space-y-3 text-sm">
            <Row label="Status">
              <StatusBadge status={selected.status} />
            </Row>
            <Row label="Publishes">
              <span className="text-ink">{formatDateTime(selected.publish_at)}</span>
            </Row>
            <Row label="Timezone">
              <span className="flex items-center gap-1.5 text-ink-soft">
                <Clock size={12} className="text-ink-faint" />
                {selected.timezone}
              </span>
            </Row>
            <Row label="Recurrence">
              <span className="flex items-center gap-1.5 capitalize text-ink">
                {selected.recurrence && <Repeat size={12} className="text-ink-faint" />}
                {selected.recurrence ?? "one-off"}
              </span>
            </Row>
            <Row label="Fired">
              <span className="tnum text-ink">
                {selected.fire_count}×
                {selected.last_fired_at ? ` · last ${formatDateTime(selected.last_fired_at)}` : ""}
              </span>
            </Row>
            <Row label="Content">
              <span className="font-mono text-xs text-ink-soft">
                {shortId(selected.content_id, 12)}
              </span>
            </Row>
            {canEdit && selected.status === "pending" && (
              <p className="border-t border-edge pt-3 text-xs leading-relaxed text-ink-faint">
                Drag this post to another day or time slot on the calendar to reschedule it.
              </p>
            )}
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={rescheduling !== null}
        onClose={abandonReschedule}
        onConfirm={() => void applyReschedule()}
        title="Move this scheduled post"
        busy={busy}
        danger={false}
        confirmLabel="Reschedule"
        body={
          rescheduling
            ? `This post will publish at ${formatDateTime(
                rescheduling.to.toISOString(),
              )} (${LOCAL_TZ}) instead.`
            : ""
        }
      />

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

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-ink-faint">{label}</span>
      {children}
    </div>
  );
}

// ---- create --------------------------------------------------------------

function CreateScheduleModal({
  open,
  preselectedContent,
  initialWhen,
  onClose,
  onCreated,
}: {
  open: boolean;
  preselectedContent: string | null;
  initialWhen: Date | null;
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
  const [when, setWhen] = useState("");
  const [recurrence, setRecurrence] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // Selecting a slot on the grid pre-fills the time; opening from the header
  // button defaults to the next whole hour. Recomputed per open rather than
  // held in state so a stale "next hour" can't drift into the past.
  //
  // Month-view selections land at midnight, which for today is already past —
  // and the scheduler rejects a publish_at that isn't in the future — so any
  // past instant is nudged forward to the next whole hour.
  const defaultWhen = useMemo(() => {
    const nextHour = new Date(Date.now() + 60 * 60 * 1000);
    nextHour.setMinutes(0, 0, 0);
    const base = initialWhen && initialWhen > new Date() ? initialWhen : nextHour;
    return toNaiveIso(base).slice(0, 16);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialWhen?.getTime()]);

  // A half-typed time from a previous open must not survive into the next one.
  useEffect(() => {
    if (open) setWhen("");
  }, [open]);

  const effectiveWhen = when || defaultWhen;
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
        publish_at: `${effectiveWhen}:00`,
        timezone: LOCAL_TZ,
        recurrence: recurrence || null,
      });
      toast("success", "Post scheduled");
      setWhen("");
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
            <Input
              type="datetime-local"
              value={effectiveWhen}
              onChange={(e) => setWhen(e.target.value)}
            />
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
