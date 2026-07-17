import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ExternalLink,
  Globe2,
  ImageIcon,
  Link2,
  Linkedin,
  MessageSquare,
  Repeat2,
  Send,
  ThumbsUp,
  Unplug,
  User,
} from "lucide-react";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Select } from "../components/ui/Input";
import { ConfirmDialog } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { Table, type Column } from "../components/ui/Table";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { contentApi, socialApi } from "../lib/api/endpoints";
import type { ContentItem, PublishJob, SocialConnection } from "../lib/api/types";
import { formatDateTime, formatRelative, truncate } from "../lib/format";

export default function Social() {
  const { toast } = useToast();
  const [params, setParams] = useSearchParams();

  const connections = useApi(() => socialApi.connections(), []);
  const jobs = useApi(() => socialApi.publishJobs({ limit: 30 }), []);
  const publishable = useApi(() => contentApi.list({ status: "approved", limit: 100 }), []);

  const [contentId, setContentId] = useState(params.get("content") ?? "");
  const [connecting, setConnecting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [disconnecting, setDisconnecting] = useState<SocialConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const callbackHandled = useRef(false);

  const connected = connections.data?.items.find((c) => c.status === "connected") ?? null;

  // OAuth return leg: LinkedIn redirected back with ?code=&state= — relay
  // both to the backend along with our own bearer token.
  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");
    if (!code || !state || callbackHandled.current) return;
    callbackHandled.current = true;
    (async () => {
      try {
        await socialApi.finishLinkedIn(code, state);
        toast("success", "LinkedIn connected");
        connections.reload();
      } catch (err) {
        toast("error", "LinkedIn connection failed", err instanceof ApiError ? err.message : undefined);
      } finally {
        setParams({}, { replace: true });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  async function startConnect() {
    setConnecting(true);
    try {
      const start = await socialApi.startLinkedIn();
      window.location.href = start.authorization_url;
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 503
          ? "LinkedIn app credentials aren't configured on the backend yet."
          : err instanceof ApiError
            ? err.message
            : undefined;
      toast("warning", "Can't start LinkedIn connect", message);
      setConnecting(false);
    }
  }

  async function disconnect() {
    if (!disconnecting) return;
    setBusy(true);
    try {
      await socialApi.disconnect(disconnecting.connection_id);
      toast("success", "LinkedIn disconnected");
      setDisconnecting(null);
      connections.reload();
    } catch (err) {
      toast("error", "Disconnect failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  const selected: ContentItem | null =
    publishable.data?.items.find((i) => i.content_id === contentId) ??
    publishable.data?.items[0] ??
    null;

  async function publishNow() {
    if (!selected) return;
    setPublishing(true);
    try {
      const job = await socialApi.publish(selected.content_id);
      toast("success", "Published to LinkedIn", job.linkedin_post_url ?? undefined);
      jobs.reload();
      publishable.reload();
    } catch (err) {
      toast("error", "Publish failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setPublishing(false);
    }
  }

  const jobColumns: Column<PublishJob>[] = [
    { key: "when", header: "When", render: (j) => formatDateTime(j.created_at) },
    { key: "status", header: "Status", render: (j) => <StatusBadge status={j.status} /> },
    {
      key: "source",
      header: "Source",
      render: (j) => <Badge tone="neutral">{j.source}</Badge>,
    },
    {
      key: "text",
      header: "Post",
      render: (j) => <span className="text-ink-soft">{truncate(j.text ?? "—", 60)}</span>,
    },
    {
      key: "image",
      header: "Image",
      render: (j) =>
        j.image_included ? <ImageIcon size={14} className="text-accent-300" /> : <span className="text-ink-faint">—</span>,
    },
    {
      key: "link",
      header: "",
      align: "right",
      render: (j) =>
        j.linkedin_post_url ? (
          <a
            href={j.linkedin_post_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs font-medium text-accent-300 hover:text-accent-400"
          >
            Open <ExternalLink size={12} />
          </a>
        ) : j.error ? (
          <span className="text-2xs text-danger">{truncate(j.error, 40)}</span>
        ) : null,
    },
  ];

  return (
    <div className="animate-fade-up">
      <PageHeader title="Social" subtitle="Connect LinkedIn, compose, preview, and publish." />

      {/* connection card */}
      <Card>
        {connections.loading ? (
          <div className="flex items-center gap-3">
            <Skeleton className="h-10 w-10 rounded-full" />
            <div className="space-y-2">
              <Skeleton className="h-4 w-44" />
              <Skeleton className="h-3 w-28" />
            </div>
          </div>
        ) : connections.error ? (
          <ErrorState error={connections.error} onRetry={connections.reload} />
        ) : connected ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-full bg-[#0a66c2]/15 text-[#4a9fe0]">
                <Linkedin size={17} />
              </span>
              <div>
                <p className="flex items-center gap-2 text-sm font-semibold text-ink">
                  {connected.display_name ?? "LinkedIn member"}
                  <StatusBadge status="connected" />
                </p>
                <p className="mt-0.5 text-xs text-ink-faint">
                  Connected {formatRelative(connected.created_at)}
                  {connected.token_expires_at &&
                    ` · token expires ${formatRelative(connected.token_expires_at)}`}
                </p>
              </div>
            </div>
            <Button
              variant="secondary"
              size="sm"
              icon={<Unplug size={13} />}
              onClick={() => setDisconnecting(connected)}
            >
              Disconnect
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-full bg-surface-3 text-ink-faint">
                <Linkedin size={17} />
              </span>
              <div>
                <p className="text-sm font-semibold text-ink">No LinkedIn account connected</p>
                <p className="mt-0.5 text-xs text-ink-faint">
                  Connect via OAuth to publish posts straight from CreditFlow.
                </p>
              </div>
            </div>
            <Button icon={<Link2 size={14} />} loading={connecting} onClick={() => void startConnect()}>
              Connect LinkedIn
            </Button>
          </div>
        )}
      </Card>

      {/* composer + preview */}
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Compose"
            subtitle="Pick an approved content item to publish."
          />
          <div className="space-y-4">
            <Field
              label="Content"
              hint={
                (publishable.data?.items.length ?? 0) === 0 && !publishable.loading
                  ? "No approved content — approve a draft in the Content screen first."
                  : undefined
              }
            >
              <Select
                value={selected?.content_id ?? ""}
                onChange={(e) => setContentId(e.target.value)}
              >
                {publishable.loading && <option>Loading…</option>}
                {publishable.data?.items.map((item) => (
                  <option key={item.content_id} value={item.content_id}>
                    {truncate(item.title, 60)}
                  </option>
                ))}
              </Select>
            </Field>
            <Button
              icon={<Send size={14} />}
              size="lg"
              className="w-full"
              disabled={!connected || !selected}
              loading={publishing}
              title={!connected ? "Connect LinkedIn first" : undefined}
              onClick={() => void publishNow()}
            >
              Publish to LinkedIn
            </Button>
            {!connected && (
              <p className="text-center text-2xs text-ink-faint">
                Publishing needs a connected LinkedIn account.
              </p>
            )}
          </div>
        </Card>

        {/* LinkedIn-style preview */}
        <Card padded={false} className="overflow-hidden">
          <div className="border-b border-edge px-5 py-3">
            <h3 className="text-sm font-semibold text-ink">Preview</h3>
          </div>
          {selected ? (
            <div className="p-5">
              <div className="rounded-lg border border-edge-strong bg-[#1b1f23] p-4">
                <div className="flex items-center gap-2.5">
                  <span className="flex h-10 w-10 items-center justify-center rounded-full bg-surface-3 text-ink-faint">
                    <User size={17} />
                  </span>
                  <div>
                    <p className="text-[13px] font-semibold text-ink">
                      {connected?.display_name ?? "Your name"}
                    </p>
                    <p className="flex items-center gap-1 text-2xs text-ink-faint">
                      Just now · <Globe2 size={10} />
                    </p>
                  </div>
                </div>
                <p className="mt-3 whitespace-pre-wrap text-[13px] leading-relaxed text-ink">
                  {truncate(selected.body, 400)}
                </p>
                {selected.image_url && (
                  <div className="mt-3 overflow-hidden rounded-md">
                    <img
                      src={selected.image_url}
                      alt=""
                      className="max-h-56 w-full object-cover"
                      onError={(e) => {
                        (e.target as HTMLImageElement).style.display = "none";
                      }}
                    />
                  </div>
                )}
                <div className="mt-3 flex items-center justify-around border-t border-edge pt-2 text-ink-faint">
                  <span className="flex items-center gap-1.5 text-2xs"><ThumbsUp size={13} /> Like</span>
                  <span className="flex items-center gap-1.5 text-2xs"><MessageSquare size={13} /> Comment</span>
                  <span className="flex items-center gap-1.5 text-2xs"><Repeat2 size={13} /> Repost</span>
                  <span className="flex items-center gap-1.5 text-2xs"><Send size={13} /> Send</span>
                </div>
              </div>
            </div>
          ) : (
            <EmptyState
              icon={<Linkedin size={18} />}
              title="Nothing to preview"
              body="Approve a content draft and it becomes publishable here."
            />
          )}
        </Card>
      </div>

      {/* history */}
      <Card className="mt-4" padded={false}>
        <div className="p-5 pb-0">
          <CardHeader title="Publish history" subtitle="Every attempt, manual or scheduled" />
        </div>
        <div className="px-2 pb-2">
          {jobs.error ? (
            <ErrorState error={jobs.error} onRetry={jobs.reload} />
          ) : (
            <Table
              columns={jobColumns}
              rows={jobs.data?.items ?? []}
              rowKey={(j) => j.job_id}
              loading={jobs.loading}
              empty={
                <EmptyState
                  icon={<Send size={18} />}
                  title="No publishes yet"
                  body="Manual publishes and fired schedules both land in this history."
                />
              }
            />
          )}
        </div>
      </Card>

      <ConfirmDialog
        open={disconnecting !== null}
        onClose={() => setDisconnecting(null)}
        onConfirm={() => void disconnect()}
        title="Disconnect LinkedIn"
        busy={busy}
        confirmLabel="Disconnect"
        body="Scheduled posts will fail to publish until an account is connected again."
      />
    </div>
  );
}
