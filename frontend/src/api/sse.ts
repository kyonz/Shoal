/**
 * Server-sent events over fetch. EventSource cannot send an Authorization
 * header, so API-key sessions would not work with it; this reader sends the
 * same credentials as every other call and resumes with Last-Event-ID.
 */
import { credentials } from "./client";

export interface SseMessage {
  id?: string;
  event: string;
  data: string;
}

export interface SseOptions {
  onMessage: (msg: SseMessage) => void;
  onOpen?: () => void;
  onError?: (err: unknown) => void;
  /** Called when the server ends the stream on purpose (e.g. run finished). */
  onEnd?: () => void;
  lastEventId?: string;
}

/** Parse one SSE block (lines between blank lines). Exported for tests. */
export function parseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const i = line.indexOf(":");
    const field = i === -1 ? line : line.slice(0, i);
    let value = i === -1 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  if (!data.length && event === "message") return null;
  return { id, event, data: data.join("\n") };
}

/** Open a stream; returns a function that closes it. Reconnects with backoff. */
export function subscribe(url: string, opts: SseOptions): () => void {
  const controller = new AbortController();
  let lastId = opts.lastEventId;
  let stopped = false;
  let attempt = 0;

  const run = async () => {
    while (!stopped) {
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream", ...credentials.headers() };
        if (lastId) headers["Last-Event-ID"] = lastId;
        const response = await fetch(url, { headers, credentials: "include", signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`stream failed: HTTP ${response.status}`);
        attempt = 0;
        opts.onOpen?.();
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let ended = false;
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buffer.search(/\r?\n\r?\n/)) !== -1) {
            const block = buffer.slice(0, idx);
            buffer = buffer.slice(idx).replace(/^\r?\n\r?\n/, "");
            const msg = parseBlock(block);
            if (!msg) continue;
            if (msg.id) lastId = msg.id;
            if (msg.event === "end") ended = true;
            opts.onMessage(msg);
          }
        }
        if (ended) {
          opts.onEnd?.();
          return;
        }
      } catch (err) {
        if (stopped || controller.signal.aborted) return;
        opts.onError?.(err);
      }
      attempt += 1;
      await new Promise((r) => setTimeout(r, Math.min(15000, 500 * 2 ** attempt)));
    }
  };
  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}
