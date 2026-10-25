import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { rawJson, type RunEvent } from "@/api/client";
import { subscribe } from "@/api/sse";
import { keys } from "@/api/hooks";

export type StreamState = "connecting" | "live" | "polling" | "ended" | "reconnecting";

/**
 * All events of a run: the history first, then live over SSE. Any step or
 * run event refreshes the run detail (throttled) so statuses stay current.
 * Where the server cannot stream (a federated tenant answers 501), the
 * history is polled instead until the run finishes.
 */
export function useRunEvents(runId: string | undefined, terminal: boolean) {
  const qc = useQueryClient();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const seen = useRef(new Set<number>());
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    let close: (() => void) | null = null;
    let poll: ReturnType<typeof setTimeout> | null = null;
    seen.current = new Set();
    setEvents([]);
    setState("connecting");

    const refresh = () => {
      if (pending.current) return;
      pending.current = setTimeout(() => {
        pending.current = null;
        void qc.invalidateQueries({ queryKey: keys.run(runId) });
        void qc.invalidateQueries({ queryKey: ["run-signals", runId] });
      }, 350);
    };

    const add = (list: RunEvent[]) => {
      const fresh = list.filter((e) => !seen.current.has(e.id));
      if (!fresh.length) return;
      fresh.forEach((e) => seen.current.add(e.id));
      setEvents((cur) => [...cur, ...fresh].sort((a, b) => a.id - b.id));
    };

    void (async () => {
      try {
        const history = await rawJson<RunEvent[]>(`/v1/runs/${runId}/events`);
        if (cancelled) return;
        add(history);
        if (terminal) {
          setState("ended");
          return;
        }
        const last = history.length ? String(history[history.length - 1].id) : undefined;
        close = subscribe(`/v1/runs/${runId}/events`, {
          lastEventId: last,
          onOpen: () => setState("live"),
          onError: () => setState("reconnecting"),
          onEnd: () => {
            setState("ended");
            refresh();
          },
          onUnsupported: () => {
            setState("polling");
            const tick = async () => {
              try {
                add(await rawJson<RunEvent[]>(`/v1/runs/${runId}/events`));
                const run = await rawJson<{ status: string }>(`/v1/runs/${runId}`);
                void qc.invalidateQueries({ queryKey: keys.run(runId) });
                if (["succeeded", "failed", "cancelled"].includes(run.status)) {
                  setState("ended");
                  return;
                }
              } catch {
                /* keep polling */
              }
              if (!cancelled) poll = setTimeout(tick, 3000);
            };
            poll = setTimeout(tick, 3000);
          },
          onMessage: (msg) => {
            if (msg.event === "end") return;
            try {
              const ev = JSON.parse(msg.data) as RunEvent;
              add([ev]);
              if (/^(run|step|approval|signal|credential)\./.test(ev.type)) refresh();
            } catch {
              /* ignore malformed */
            }
          },
        });
      } catch {
        if (!cancelled) setState("reconnecting");
      }
    })();

    return () => {
      cancelled = true;
      close?.();
      if (poll) clearTimeout(poll);
      if (pending.current) clearTimeout(pending.current);
      pending.current = null;
    };
    // `terminal` only matters for the initial decision; a run that finishes
    // while streaming ends the stream itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, qc]);

  return { events, state };
}
