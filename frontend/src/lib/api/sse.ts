/**
 * SSE consumption for the AI generation stream.
 *
 * EventSource cannot send an Authorization header, so this uses fetch +
 * ReadableStream and parses the text/event-stream frames by hand. The
 * backend frame format (services/ai/store.py):
 *
 *   event: token|done|cancelled|error
 *   data: {"seq": n, "type": "...", ...}
 */
import { GATEWAY_URL, session } from "./client";
import type { StreamMessage } from "./types";

export interface StreamHandlers {
  onMessage: (message: StreamMessage) => void;
  onError: (error: Error) => void;
  onClose: () => void;
}

/** Open the stream; returns an abort function. */
export function openGenerationStream(jobId: string, handlers: StreamHandlers): () => void {
  const controller = new AbortController();

  (async () => {
    let response: Response;
    try {
      response = await fetch(`${GATEWAY_URL}/generations/${jobId}/stream`, {
        headers: { Authorization: `Bearer ${session.access ?? ""}` },
        signal: controller.signal,
      });
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onError(new Error("Cannot reach the generation stream"));
      }
      return;
    }
    if (!response.ok || !response.body) {
      handlers.onError(new Error(`Stream failed (${response.status})`));
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line.
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const dataLine = frame
            .split("\n")
            .find((line) => line.startsWith("data:"));
          if (!dataLine) continue;
          try {
            const message = JSON.parse(dataLine.slice(5).trim()) as StreamMessage;
            handlers.onMessage(message);
            if (message.type !== "token") {
              controller.abort(); // terminal frame — we're done
              handlers.onClose();
              return;
            }
          } catch {
            // Malformed frame — skip it rather than killing the stream.
          }
        }
      }
      handlers.onClose();
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onError(err instanceof Error ? err : new Error("Stream interrupted"));
      }
    }
  })();

  return () => controller.abort();
}
