import { useState, type FormEvent } from "react";
import { ExternalLink, Globe, RefreshCw, Trash2 } from "lucide-react";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input, Select } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Table, type Column } from "../components/ui/Table";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { scraperApi } from "../lib/api/endpoints";
import type { ScrapeJob, ScrapedDocument } from "../lib/api/types";
import { formatRelative, truncate } from "../lib/format";

export default function Scraper() {
  const { toast } = useToast();
  const jobs = useApi(() => scraperApi.list({ limit: 50 }), []);
  const [url, setUrl] = useState("");
  const [recurrence, setRecurrence] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [viewing, setViewing] = useState<ScrapeJob | null>(null);
  const [deleting, setDeleting] = useState<ScrapeJob | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    try {
      await scraperApi.create(url.trim(), "page", recurrence || null);
      toast("success", "Scrape job queued", "The worker picks it up in the background.");
      setUrl("");
      jobs.reload();
    } catch (err) {
      toast("error", "Could not queue the job", err instanceof ApiError ? err.message : undefined);
    } finally {
      setSubmitting(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await scraperApi.remove(deleting.job_id);
      toast("success", "Job deleted");
      setDeleting(null);
      jobs.reload();
    } catch (err) {
      toast("error", "Delete failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<ScrapeJob>[] = [
    {
      key: "url",
      header: "URL",
      render: (j) => (
        <span className="block max-w-[16rem] truncate font-medium text-ink" title={j.url}>
          {j.url.replace(/^https?:\/\//, "")}
        </span>
      ),
    },
    { key: "status", header: "Status", render: (j) => <StatusBadge status={j.status} /> },
    {
      key: "recurrence",
      header: "Repeat",
      render: (j) =>
        j.recurrence ? <Badge tone="accent">{j.recurrence}</Badge> : <span className="text-ink-faint">one-off</span>,
    },
    { key: "runs", header: "Runs", align: "right", render: (j) => <span>{j.run_count}</span> },
    {
      key: "last",
      header: "Last run",
      render: (j) => <span className="whitespace-nowrap">{formatRelative(j.last_run_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (j) => (
        <span className="flex items-center justify-end gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={!j.result_document_id}
            onClick={(e) => {
              e.stopPropagation();
              setViewing(j);
            }}
          >
            Results
          </Button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              setDeleting(j);
            }}
            aria-label="Delete job"
            className="rounded-md p-1.5 text-ink-faint transition-colors hover:bg-danger/10 hover:text-danger"
          >
            <Trash2 size={14} />
          </button>
        </span>
      ),
    },
  ];

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Scraper"
        subtitle="Queue a URL, let the worker extract the page, browse the results."
        actions={
          <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={jobs.reload}>
            Refresh
          </Button>
        }
      />

      <Card>
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <Field label="URL to scrape">
              <Input
                type="url"
                required
                placeholder="https://example.com/article"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </Field>
          </div>
          <div className="w-full sm:w-36">
            <Field label="Repeat">
              <Select value={recurrence} onChange={(e) => setRecurrence(e.target.value)}>
                <option value="">One-off</option>
                <option value="hourly">Hourly</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
              </Select>
            </Field>
          </div>
          <Button type="submit" loading={submitting} icon={<Globe size={14} />}>
            Scrape
          </Button>
        </form>
      </Card>

      <Card className="mt-4" padded={false}>
        <div className="px-2 py-2">
          {jobs.error ? (
            <ErrorState error={jobs.error} onRetry={jobs.reload} />
          ) : (
            <Table
              columns={columns}
              rows={jobs.data?.items ?? []}
              rowKey={(j) => j.job_id}
              loading={jobs.loading}
              onRowClick={(j) => j.result_document_id && setViewing(j)}
              empty={
                <EmptyState
                  icon={<Globe size={18} />}
                  title="No scrape jobs yet"
                  body="Submit a URL above — jobs run in the background and their extracted content lands here."
                />
              }
            />
          )}
        </div>
      </Card>

      <ResultsModal job={viewing} onClose={() => setViewing(null)} />

      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => void remove()}
        title="Delete scrape job"
        busy={busy}
        confirmLabel="Delete"
        body={
          deleting
            ? `Delete the job for ${truncate(deleting.url, 60)} and every document it produced?`
            : ""
        }
      />
    </div>
  );
}

function ResultsModal({ job, onClose }: { job: ScrapeJob | null; onClose: () => void }) {
  const detail = useApi(
    () => (job ? scraperApi.get(job.job_id) : Promise.resolve(null)),
    [job?.job_id],
  );
  const doc: ScrapedDocument | null = detail.data?.document ?? null;

  return (
    <Modal open={job !== null} onClose={onClose} wide title="Extracted content">
      {detail.loading ? (
        <div className="space-y-3">
          <div className="h-5 w-2/3 animate-pulse rounded bg-surface-3" />
          <div className="h-3.5 w-full animate-pulse rounded bg-surface-3" />
          <div className="h-3.5 w-full animate-pulse rounded bg-surface-3" />
          <div className="h-3.5 w-1/2 animate-pulse rounded bg-surface-3" />
        </div>
      ) : detail.error ? (
        <ErrorState error={detail.error} onRetry={detail.reload} />
      ) : doc ? (
        <div className="space-y-4">
          <div>
            <h3 className="text-base font-semibold text-ink">{doc.title ?? "Untitled page"}</h3>
            {doc.description && (
              <p className="mt-1 text-xs leading-relaxed text-ink-soft">{doc.description}</p>
            )}
            <a
              href={doc.final_url ?? doc.url ?? "#"}
              target="_blank"
              rel="noreferrer"
              className="mt-1.5 inline-flex items-center gap-1 text-xs text-accent-300 hover:text-accent-400"
            >
              {truncate(doc.final_url ?? doc.url ?? "", 60)} <ExternalLink size={11} />
            </a>
          </div>
          {doc.headings && doc.headings.length > 0 && (
            <div>
              <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-ink-faint">
                Headings
              </p>
              <ul className="space-y-1">
                {doc.headings.slice(0, 8).map((h, i) => (
                  <li key={i} className="text-xs text-ink-soft">
                    · {truncate(h, 90)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {doc.text && (
            <div>
              <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-ink-faint">
                Extracted text
              </p>
              <div className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-field border border-edge bg-bg/60 p-3 text-xs leading-relaxed text-ink-soft">
                {truncate(doc.text, 6000)}
              </div>
            </div>
          )}
        </div>
      ) : (
        <EmptyState
          icon={<Globe size={18} />}
          title="No document yet"
          body="The job hasn't produced a result — it may still be pending or it failed."
        />
      )}
    </Modal>
  );
}
