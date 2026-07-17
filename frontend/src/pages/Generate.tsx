import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CircleStop,
  Copy,
  FileText,
  Save,
  Sparkles,
  Wand2,
  Zap,
} from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Field, Select, Textarea } from "../components/ui/Input";
import { PageHeader } from "../components/ui/PageHeader";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { aiApi, contentApi } from "../lib/api/endpoints";
import { openGenerationStream } from "../lib/api/sse";
import type { StreamMessage } from "../lib/api/types";
import { formatNumber } from "../lib/format";

type Phase = "idle" | "starting" | "streaming" | "done" | "cancelled" | "error";

interface RunStats {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
}

const PROMPT_IDEAS = [
  "Write a LinkedIn post announcing our new AI-powered analytics dashboard",
  "Draft a friendly product-update email about faster export speeds",
  "Summarize the key benefits of switching to usage-based pricing",
];

export default function Generate() {
  const { toast } = useToast();
  const navigate = useNavigate();

  const models = useApi(() => aiApi.models(), []);
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [output, setOutput] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [chunkCount, setChunkCount] = useState(0);
  const [stats, setStats] = useState<RunStats>({ inputTokens: null, outputTokens: null, totalTokens: null });
  const [errorReason, setErrorReason] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const closeStream = useRef<(() => void) | null>(null);
  const outputRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!model && models.data?.models.length) {
      setModel(models.data.models[0].alias);
    }
  }, [models.data, model]);

  // Follow the stream as it grows.
  useEffect(() => {
    const el = outputRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [output]);

  useEffect(() => () => closeStream.current?.(), []);

  const busy = phase === "starting" || phase === "streaming";

  const onMessage = useCallback((message: StreamMessage) => {
    switch (message.type) {
      case "token":
        setPhase("streaming");
        setOutput((current) => current + (message.content ?? ""));
        setChunkCount((n) => n + 1);
        break;
      case "done":
      case "cancelled":
        setPhase(message.type === "done" ? "done" : "cancelled");
        setStats({
          inputTokens: message.input_tokens ?? null,
          outputTokens: message.output_tokens ?? null,
          totalTokens: message.total_tokens ?? null,
        });
        break;
      case "error":
        setPhase("error");
        setErrorReason(message.reason ?? "Generation failed");
        break;
    }
  }, []);

  async function start() {
    if (!prompt.trim() || busy) return;
    setPhase("starting");
    setOutput("");
    setChunkCount(0);
    setStats({ inputTokens: null, outputTokens: null, totalTokens: null });
    setErrorReason(null);
    try {
      const accepted = await aiApi.create(prompt.trim(), model || "fast");
      setJobId(accepted.job_id);
      closeStream.current = openGenerationStream(accepted.job_id, {
        onMessage,
        onError: (err) => {
          setPhase("error");
          setErrorReason(err.message);
        },
        onClose: () => {},
      });
    } catch (err) {
      setPhase("error");
      if (err instanceof ApiError && err.status === 429) {
        setErrorReason("Token quota exhausted for this account — check your usage meter.");
        toast("warning", "Quota exhausted", "This account has used its token quota for the period.");
      } else {
        setErrorReason(err instanceof ApiError ? err.message : "Could not start the generation");
      }
    }
  }

  async function cancel() {
    if (!jobId) return;
    try {
      await aiApi.cancel(jobId);
      toast("info", "Cancelling", "The stream will stop at the next chunk.");
    } catch (err) {
      toast("error", "Cancel failed", err instanceof ApiError ? err.message : undefined);
    }
  }

  async function saveDraft() {
    if (!output.trim()) return;
    setSaving(true);
    try {
      const firstLine = output.trim().split("\n")[0].replace(/^#+\s*/, "");
      const item = await contentApi.create({
        title: firstLine.slice(0, 120) || "AI generation",
        body: output.trim(),
        tags: ["ai-generated"],
        generation_job_id: jobId,
      });
      toast("success", "Draft saved", "The generation is now in your content library.");
      navigate(`/content?focus=${item.content_id}`);
    } catch (err) {
      toast("error", "Save failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setSaving(false);
    }
  }

  function copyOutput() {
    void navigator.clipboard.writeText(output);
    toast("success", "Copied to clipboard");
  }

  const phaseBadge =
    phase === "streaming" ? (
      <Badge tone="accent" dot>
        streaming
      </Badge>
    ) : phase === "starting" ? (
      <Badge tone="warning" dot>
        starting
      </Badge>
    ) : phase === "done" ? (
      <Badge tone="success" dot>
        completed
      </Badge>
    ) : phase === "cancelled" ? (
      <Badge tone="danger" dot>
        cancelled
      </Badge>
    ) : phase === "error" ? (
      <Badge tone="danger" dot>
        failed
      </Badge>
    ) : null;

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="AI Studio"
        subtitle="Generate content live, token by token, straight from the model."
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {/* composer */}
        <Card className="lg:col-span-2">
          <CardHeader title="Composer" subtitle="Pick a model, write a prompt." />
          <div className="space-y-4">
            <Field label="Model">
              <Select value={model} onChange={(e) => setModel(e.target.value)} disabled={busy}>
                {models.loading && <option>Loading models…</option>}
                {models.error && <option value="fast">fast (default)</option>}
                {models.data?.models.map((m) => (
                  <option key={m.alias} value={m.alias}>
                    {m.alias} — {m.model}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Prompt">
              <Textarea
                rows={9}
                placeholder="What should we write today?"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                disabled={busy}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void start();
                }}
              />
            </Field>

            {phase === "idle" && !prompt && (
              <div className="space-y-1.5">
                <p className="text-2xs font-medium uppercase tracking-wider text-ink-faint">
                  Try one of these
                </p>
                {PROMPT_IDEAS.map((idea) => (
                  <button
                    key={idea}
                    onClick={() => setPrompt(idea)}
                    className="flex w-full items-start gap-2 rounded-field border border-edge bg-surface-2/50 px-3 py-2 text-left text-xs text-ink-soft transition-colors hover:border-accent-600/40 hover:text-ink"
                  >
                    <Wand2 size={13} className="mt-0.5 shrink-0 text-accent-400" />
                    {idea}
                  </button>
                ))}
              </div>
            )}

            <div className="flex gap-2">
              <Button
                onClick={() => void start()}
                loading={phase === "starting"}
                disabled={busy || !prompt.trim()}
                icon={<Sparkles size={15} />}
                className="flex-1"
                size="lg"
              >
                {phase === "streaming" ? "Streaming…" : "Generate"}
              </Button>
              {busy && jobId && (
                <Button variant="danger" size="lg" onClick={() => void cancel()} icon={<CircleStop size={15} />}>
                  Stop
                </Button>
              )}
            </div>
            <p className="text-2xs text-ink-faint">⌘/Ctrl + Enter to generate</p>
          </div>
        </Card>

        {/* output */}
        <Card className="flex min-h-[28rem] flex-col lg:col-span-3" glow={phase === "streaming"}>
          <div className="mb-3 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2.5">
              <h3 className="text-sm font-semibold text-ink">Output</h3>
              {phaseBadge}
            </div>
            <div className="flex items-center gap-3 text-2xs text-ink-faint">
              {(phase === "streaming" || chunkCount > 0) && (
                <span className="flex items-center gap-1 tnum">
                  <Zap size={11} className="text-warning" />
                  {stats.outputTokens !== null
                    ? `${formatNumber(stats.outputTokens)} tokens`
                    : `${formatNumber(chunkCount)} chunks`}
                </span>
              )}
              {stats.totalTokens !== null && (
                <span className="tnum">{formatNumber(stats.totalTokens)} total</span>
              )}
            </div>
          </div>

          <div
            ref={outputRef}
            className="min-h-0 flex-1 overflow-y-auto rounded-field border border-edge bg-bg/60 p-4"
          >
            {output ? (
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">
                {output}
                {phase === "streaming" && (
                  <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-blink bg-accent-400" />
                )}
              </p>
            ) : phase === "starting" ? (
              <div className="flex h-full items-center justify-center">
                <span className="flex items-center gap-2 text-sm text-ink-faint">
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-edge-strong border-t-accent-500" />
                  Waking the model…
                </span>
              </div>
            ) : phase === "error" ? (
              <div className="flex h-full items-center justify-center text-center">
                <div>
                  <p className="text-sm font-medium text-danger">Generation failed</p>
                  <p className="mx-auto mt-1 max-w-xs text-xs text-ink-faint">{errorReason}</p>
                </div>
              </div>
            ) : (
              <div className="flex h-full items-center justify-center text-center">
                <div>
                  <span className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-accent-600/10 text-accent-300">
                    <Sparkles size={19} />
                  </span>
                  <p className="text-sm text-ink-soft">Your generation appears here</p>
                  <p className="mt-1 text-xs text-ink-faint">Streamed live over SSE as the model writes.</p>
                </div>
              </div>
            )}
          </div>

          {(phase === "done" || phase === "cancelled") && output && (
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 animate-fade-up">
              <div className="flex gap-4 text-2xs text-ink-faint">
                {stats.inputTokens !== null && (
                  <span className="tnum">in {formatNumber(stats.inputTokens)}</span>
                )}
                {stats.outputTokens !== null && (
                  <span className="tnum">out {formatNumber(stats.outputTokens)}</span>
                )}
              </div>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" icon={<Copy size={13} />} onClick={copyOutput}>
                  Copy
                </Button>
                <Button
                  size="sm"
                  icon={<Save size={13} />}
                  loading={saving}
                  onClick={() => void saveDraft()}
                >
                  Save as draft
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={<FileText size={13} />}
                  onClick={() => navigate("/content")}
                >
                  Library
                </Button>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
