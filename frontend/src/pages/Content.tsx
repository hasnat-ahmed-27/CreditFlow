import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  CalendarClock,
  CheckCircle2,
  FileText,
  ImageIcon,
  Pencil,
  Plus,
  Send,
  Tag,
  Trash2,
  Undo2,
} from "lucide-react";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input, Textarea } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Skeleton } from "../components/ui/Skeleton";
import { Tabs } from "../components/ui/Tabs";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { contentApi, type ContentDraft } from "../lib/api/endpoints";
import type { ContentItem, ContentStatus } from "../lib/api/types";
import { formatRelative, truncate } from "../lib/format";

/** Legal transitions, mirrored from the Content service's status machine. */
const TRANSITIONS: Record<ContentStatus, ContentStatus[]> = {
  draft: ["approved"],
  approved: ["draft", "published"],
  scheduled: ["approved"],
  published: [],
};

const STATUS_TABS = [
  { id: "all", label: "All" },
  { id: "draft", label: "Drafts" },
  { id: "approved", label: "Approved" },
  { id: "scheduled", label: "Scheduled" },
  { id: "published", label: "Published" },
];

export default function Content() {
  const { toast } = useToast();
  const [params] = useSearchParams();
  const [status, setStatus] = useState("all");
  const [tag, setTag] = useState<string | null>(null);
  const [editing, setEditing] = useState<ContentItem | "new" | null>(
    params.get("new") !== null ? "new" : null,
  );
  const [deleting, setDeleting] = useState<ContentItem | null>(null);
  const [busy, setBusy] = useState(false);

  const list = useApi(
    () =>
      contentApi.list({
        status: status === "all" ? undefined : status,
        tag: tag ?? undefined,
        limit: 50,
      }),
    [status, tag],
  );

  const allTags = useMemo(() => {
    const tags = new Set<string>();
    for (const item of list.data?.items ?? []) item.tags.forEach((t) => tags.add(t));
    return [...tags].sort();
  }, [list.data]);

  const focusId = params.get("focus");

  async function transition(item: ContentItem, next: ContentStatus) {
    try {
      await contentApi.setStatus(item.content_id, next);
      toast("success", `Moved to ${next}`);
      list.reload();
    } catch (err) {
      toast("error", "Status change failed", err instanceof ApiError ? err.message : undefined);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await contentApi.remove(deleting.content_id);
      toast("success", "Content deleted");
      setDeleting(null);
      list.reload();
    } catch (err) {
      toast("error", "Delete failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Content"
        subtitle="Drafts and their lifecycle: draft → approved → scheduled → published."
        actions={
          <Button icon={<Plus size={15} />} onClick={() => setEditing("new")}>
            New draft
          </Button>
        }
      />

      <Tabs tabs={STATUS_TABS} active={status} onChange={setStatus} />

      {allTags.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <Tag size={13} className="text-ink-faint" />
          {allTags.map((t) => (
            <button
              key={t}
              onClick={() => setTag(tag === t ? null : t)}
              className={
                "rounded-full border px-2.5 py-0.5 text-2xs font-medium transition-colors " +
                (tag === t
                  ? "border-accent-500/60 bg-accent-600/20 text-accent-300"
                  : "border-edge-strong text-ink-faint hover:border-accent-600/40 hover:text-ink-soft")
              }
            >
              {t}
            </button>
          ))}
        </div>
      )}

      <div className="mt-4">
        {list.error ? (
          <Card><ErrorState error={list.error} onRetry={list.reload} /></Card>
        ) : list.loading ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-44 w-full rounded-card" />
            ))}
          </div>
        ) : (list.data?.items.length ?? 0) === 0 ? (
          <Card>
            <EmptyState
              icon={<FileText size={18} />}
              title={status === "all" ? "No content yet" : `Nothing ${status}`}
              body="Write a draft here, or generate one in the AI Studio and save it to the library."
              action={
                <div className="flex gap-2">
                  <Button size="sm" icon={<Plus size={13} />} onClick={() => setEditing("new")}>
                    New draft
                  </Button>
                  <Link to="/generate">
                    <Button size="sm" variant="secondary">
                      Open AI Studio
                    </Button>
                  </Link>
                </div>
              }
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {list.data!.items.map((item) => (
              <Card
                key={item.content_id}
                padded={false}
                className={
                  "flex flex-col overflow-hidden transition-shadow hover:shadow-pop " +
                  (item.content_id === focusId ? "border-accent-500/60" : "")
                }
              >
                {item.image_url && (
                  <div className="h-28 w-full overflow-hidden border-b border-edge bg-surface-2">
                    <img
                      src={item.image_url}
                      alt=""
                      className="h-full w-full object-cover"
                      onError={(e) => {
                        (e.target as HTMLImageElement).style.display = "none";
                      }}
                    />
                  </div>
                )}
                <div className="flex flex-1 flex-col p-4">
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="text-sm font-semibold leading-snug text-ink">
                      {truncate(item.title, 60)}
                    </h3>
                    <StatusBadge status={item.status} />
                  </div>
                  <p className="mt-2 flex-1 text-xs leading-relaxed text-ink-faint">
                    {truncate(item.body, 140)}
                  </p>
                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    {item.tags.slice(0, 3).map((t) => (
                      <Badge key={t} tone="neutral">
                        {t}
                      </Badge>
                    ))}
                    {item.generation_job_id && <Badge tone="accent">AI</Badge>}
                    <span className="ml-auto text-2xs text-ink-faint">
                      v{item.version} · {formatRelative(item.updated_at)}
                    </span>
                  </div>

                  <div className="mt-3 flex items-center gap-1.5 border-t border-edge pt-3">
                    {item.status !== "published" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={<Pencil size={13} />}
                        onClick={() => setEditing(item)}
                      >
                        Edit
                      </Button>
                    )}
                    {TRANSITIONS[item.status].includes("approved") && item.status === "draft" && (
                      <Button
                        variant="secondary"
                        size="sm"
                        icon={<CheckCircle2 size={13} />}
                        onClick={() => void transition(item, "approved")}
                      >
                        Approve
                      </Button>
                    )}
                    {item.status === "approved" && (
                      <>
                        <Link to={`/calendar?content=${item.content_id}`}>
                          <Button variant="secondary" size="sm" icon={<CalendarClock size={13} />}>
                            Schedule
                          </Button>
                        </Link>
                        <Link to={`/social?content=${item.content_id}`}>
                          <Button variant="ghost" size="sm" icon={<Send size={13} />}>
                            Publish
                          </Button>
                        </Link>
                      </>
                    )}
                    {item.status === "scheduled" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={<Undo2 size={13} />}
                        onClick={() => void transition(item, "approved")}
                      >
                        Unschedule
                      </Button>
                    )}
                    {item.status === "approved" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={<Undo2 size={13} />}
                        onClick={() => void transition(item, "draft")}
                      >
                        Rework
                      </Button>
                    )}
                    <button
                      onClick={() => setDeleting(item)}
                      aria-label="Delete"
                      className="ml-auto rounded-md p-1.5 text-ink-faint transition-colors hover:bg-danger/10 hover:text-danger"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>

      <EditorModal
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          list.reload();
        }}
      />

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => void remove()}
        title="Delete content"
        busy={busy}
        confirmLabel="Delete"
        body={deleting ? `Delete "${truncate(deleting.title, 60)}" and its version history? This can't be undone.` : ""}
      />
    </div>
  );
}

// ---- editor --------------------------------------------------------------

function EditorModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: ContentItem | "new" | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const isNew = editing === "new";
  const item = isNew || editing === null ? null : editing;

  const [title, setTitle] = useState(item?.title ?? "");
  const [body, setBody] = useState(item?.body ?? "");
  const [tags, setTags] = useState((item?.tags ?? []).join(", "));
  const [imageUrl, setImageUrl] = useState(item?.image_url ?? "");
  const [busy, setBusy] = useState(false);
  const [seededFor, setSeededFor] = useState<string | null>(null);

  // Re-seed the form when a different item opens.
  const key = item?.content_id ?? (isNew ? "new" : null);
  if (key !== null && key !== seededFor) {
    setSeededFor(key);
    setTitle(item?.title ?? "");
    setBody(item?.body ?? "");
    setTags((item?.tags ?? []).join(", "));
    setImageUrl(item?.image_url ?? "");
  }

  async function save() {
    if (!title.trim() || !body.trim()) {
      toast("warning", "Title and body are required");
      return;
    }
    const draft: ContentDraft = {
      title: title.trim(),
      body: body.trim(),
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      image_url: imageUrl.trim() || null,
    };
    setBusy(true);
    try {
      if (item) {
        await contentApi.update(item.content_id, draft);
        toast("success", "Draft updated");
      } else {
        await contentApi.create(draft);
        toast("success", "Draft created");
      }
      onSaved();
    } catch (err) {
      toast("error", "Save failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={editing !== null}
      onClose={onClose}
      wide
      title={isNew ? "New draft" : "Edit draft"}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void save()} loading={busy}>
            {isNew ? "Create draft" : "Save changes"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Title">
          <Input
            placeholder="A headline for this content"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </Field>
        <Field label="Body">
          <Textarea
            rows={10}
            placeholder="Write or paste the content…"
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Tags" hint="Comma-separated.">
            <Input placeholder="launch, product" value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field>
          <Field label="Image URL" hint="Optional cover image.">
            <Input
              placeholder="https://…"
              value={imageUrl}
              onChange={(e) => setImageUrl(e.target.value)}
            />
          </Field>
        </div>
        {imageUrl.trim() && (
          <div className="overflow-hidden rounded-field border border-edge">
            <img
              src={imageUrl}
              alt="Preview"
              className="max-h-48 w-full object-cover"
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = "none";
              }}
            />
            <p className="flex items-center gap-1.5 border-t border-edge bg-surface-2/60 px-3 py-1.5 text-2xs text-ink-faint">
              <ImageIcon size={11} /> Image preview
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}
