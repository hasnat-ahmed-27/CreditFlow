import { useState } from "react";
import { Bell, Inbox, Mail, MailX } from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { Tabs } from "../components/ui/Tabs";
import { useApi } from "../hooks/useApi";
import { notificationsApi } from "../lib/api/endpoints";
import { formatRelative } from "../lib/format";

export default function Notifications() {
  const [tab, setTab] = useState("all");
  const list = useApi(
    () => notificationsApi.list({ status: tab === "all" ? undefined : tab, limit: 50 }),
    [tab],
  );

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Notifications"
        subtitle="Every email the platform sent (or tried to send) for this account."
      />

      <Tabs
        tabs={[
          { id: "all", label: "All", count: tab === "all" ? list.data?.total : undefined },
          { id: "sent", label: "Sent" },
          { id: "failed", label: "Failed" },
        ]}
        active={tab}
        onChange={setTab}
      />

      <Card className="mt-4" padded={false}>
        {list.error ? (
          <ErrorState error={list.error} onRetry={list.reload} />
        ) : list.loading ? (
          <div className="space-y-3 p-5">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex items-center gap-3">
                <Skeleton className="h-9 w-9 rounded-full" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-3.5 w-1/2" />
                  <Skeleton className="h-3 w-1/3" />
                </div>
              </div>
            ))}
          </div>
        ) : (list.data?.items.length ?? 0) === 0 ? (
          <EmptyState
            icon={<Inbox size={18} />}
            title="Inbox zero"
            body="Verification emails, low-balance warnings, and publish updates will collect here."
          />
        ) : (
          <ul className="divide-y divide-edge/60">
            {list.data!.items.map((n) => (
              <li key={n.notification_id} className="flex items-start gap-3 px-5 py-3.5">
                <span
                  className={
                    "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full " +
                    (n.status === "sent"
                      ? "bg-accent-600/10 text-accent-300"
                      : "bg-danger/10 text-danger")
                  }
                >
                  {n.status === "sent" ? <Mail size={15} /> : <MailX size={15} />}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-[13px] font-medium text-ink">
                      {n.subject ?? n.template ?? "Notification"}
                    </p>
                    {n.template && <Badge tone="neutral">{n.template}</Badge>}
                    {n.status === "failed" && <Badge tone="danger">failed</Badge>}
                  </div>
                  <p className="mt-0.5 text-xs text-ink-faint">
                    {n.recipient ? `to ${n.recipient} · ` : ""}
                    {n.routing_key ? `${n.routing_key} · ` : ""}
                    {formatRelative(n.created_at)}
                  </p>
                  {n.error && (
                    <p className="mt-1 text-2xs text-danger">{n.error}</p>
                  )}
                </div>
                <Bell size={13} className="mt-1 shrink-0 text-ink-faint/50" />
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
