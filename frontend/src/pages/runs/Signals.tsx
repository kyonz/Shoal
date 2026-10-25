/** Signals: data sent into a running workflow, taken by wait steps with `signal:`. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Radio, Send } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { client, unwrap, type RunDetail, type Schemas } from "@/api/client";
import { keys } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime, dateTime } from "@/lib/format";
import { toastError } from "@/lib/setup";
import { Badge, Button, EmptyState, Field, Input, JsonBlock, SectionTitle, Textarea } from "@/components/ui";
import type { StepDoc } from "@/features/workflow/types";

type Signal = Schemas["SignalOut"];

export const signalKeys = { list: (runId: string) => ["run-signals", runId] as const };

export function useSignals(runId: string) {
  return useQuery({ queryKey: signalKeys.list(runId), queryFn: () => unwrap(client.GET("/v1/runs/{run_id}/signals", { params: { path: { run_id: runId } } })) });
}

/** Steps currently waiting for a signal, with the name or event key they wait for. */
export function waitingForSignal(run: RunDetail) {
  const defs = (run.workflow.steps as StepDoc[] | undefined) ?? [];
  return run.steps
    .filter((s) => s.status === "waiting")
    .flatMap((s) => {
      const def = defs.find((d) => d.id === s.step_id);
      const sig = def?.type === "wait" ? (def.signal as { name?: string; event?: string; match?: string } | undefined) : undefined;
      if (!sig) return [];
      return [{ stepId: s.step_id, name: sig.name, event: sig.event ? ((s.state as { event?: string }).event ?? sig.event) : undefined, match: sig.match }];
    });
}

function parsePayload(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  if (!text.trim()) return { ok: true, value: null };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (e) {
    return { ok: false, error: `Payload isn't valid JSON: ${(e as Error).message}` };
  }
}

function useSend(run: RunDetail, onSent?: () => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, payload }: { name: string; payload: unknown }) =>
      unwrap(client.POST("/v1/runs/{run_id}/signals/{name}", { params: { path: { run_id: run.id, name } }, body: { payload } })),
    onSuccess: (_, v) => {
      toast.success(`Sent signal ${v.name}`);
      void qc.invalidateQueries({ queryKey: signalKeys.list(run.id) });
      void qc.invalidateQueries({ queryKey: keys.run(run.id) });
      onSent?.();
    },
    onError: (e) => toastError(e),
  });
}

/** Banner for steps waiting on a named signal: send one right here. */
export function SignalBanner({ run }: { run: RunDetail }) {
  const { can } = useAuth();
  const waiting = waitingForSignal(run);
  if (!waiting.length) return null;
  return (
    <div className="shrink-0 space-y-3 border-b border-info/30 bg-info-soft px-4 py-3">
      {waiting.map((w) => (
        <div key={w.stepId} className="flex flex-wrap items-start gap-3">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-info text-white">
            <Radio className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-fg">
              {w.name ? (
                <>
                  Waiting for signal <span className="font-mono">{w.name}</span>
                </>
              ) : (
                "Waiting for an integration event"
              )}{" "}
              <span className="font-mono font-normal text-muted">· {w.stepId}</span>
            </div>
            <div className="text-[12.5px] text-muted">
              {w.name ? "Send a JSON payload below, or have another system post it to the run." : <span className="font-mono">{w.event}</span>}
              {w.match && (
                <>
                  {" "}
                  Only accepted when <span className="font-mono">{w.match}</span>.
                </>
              )}
            </div>
          </div>
          {w.name &&
            (can("runs:signal", run.namespace) ? (
              <InlineSend run={run} name={w.name} />
            ) : (
              <span className="text-xs text-muted">You don't have permission to send signals here.</span>
            ))}
        </div>
      ))}
    </div>
  );
}

function InlineSend({ run, name }: { run: RunDetail; name: string }) {
  const [text, setText] = useState("");
  const send = useSend(run, () => setText(""));
  const parsed = parsePayload(text);
  return (
    <div className="flex items-start gap-2">
      <div>
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='{"status": "done"}'
          rows={1}
          className="min-h-8 w-72 py-1.5 font-mono text-[12px]"
          aria-label={`Payload for ${name}`}
        />
        {!parsed.ok && <div className="mt-1 max-w-72 text-[11px] text-bad">{parsed.error}</div>}
      </div>
      <Button variant="primary" icon={<Send className="size-3.5" />} loading={send.isPending} disabled={!parsed.ok} onClick={() => parsed.ok && send.mutate({ name, payload: parsed.value })}>
        Send signal
      </Button>
    </div>
  );
}

const SOURCE_LABEL: Record<string, string> = { api: "API", callback: "Callback URL", event: "Integration event" };

/** Side-panel tab: every signal sent to the run, plus a form to send any named signal. */
export function SignalsTab({ run, terminal }: { run: RunDetail; terminal: boolean }) {
  const { can } = useAuth();
  const signals = useSignals(run.id);
  const waiting = waitingForSignal(run);
  const list = signals.data ?? [];
  return (
    <div className="space-y-5">
      {!terminal && can("runs:signal", run.namespace) && <SendForm run={run} suggested={waiting.find((w) => w.name)?.name ?? ""} />}
      <div>
        <SectionTitle>Received</SectionTitle>
        {!list.length ? (
          <EmptyState
            icon={<Radio className="size-5" />}
            title="No signals yet"
            description="Signals sent to this run appear here. A wait step in Signal mode takes the first one that matches; earlier ones are kept until then."
          />
        ) : (
          <div className="space-y-2">
            {[...list].reverse().map((s) => (
              <SignalRow key={s.id} signal={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SignalRow({ signal: s }: { signal: Signal }) {
  const taken = !!s.consumed_by;
  return (
    <div className="rounded-lg border border-border">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className="font-mono text-[12.5px] font-medium">{s.name}</span>
        <Badge tone={taken ? "ok" : "warn"} dot>
          {taken ? `Taken by ${s.consumed_by}` : "Queued"}
        </Badge>
        <Badge>{SOURCE_LABEL[s.source] ?? s.source}</Badge>
        <span className="ml-auto text-[11px] text-faint" title={dateTime(s.created_at)}>
          {relTime(s.created_at)}
        </span>
      </div>
      <div className="border-t border-border px-3 py-2 text-[11.5px] text-muted">
        {s.sender && <div className="mb-1.5">from {s.sender}</div>}
        {s.payload == null ? <span className="text-faint">No payload</span> : <JsonBlock value={s.payload} maxHeight="180px" />}
      </div>
    </div>
  );
}

function SendForm({ run, suggested }: { run: RunDetail; suggested: string }) {
  const [name, setName] = useState(suggested);
  const [text, setText] = useState("");
  const send = useSend(run, () => setText(""));
  const parsed = parsePayload(text);
  const validName = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/.test(name);
  return (
    <div className="rounded-lg border border-border p-3">
      <SectionTitle>Send a signal</SectionTitle>
      <div className="space-y-2.5">
        <Field label="Name" error={name && !validName ? "Letters, digits and _ . : - (up to 100)" : undefined}>
          <Input mono value={name} onChange={(e) => setName(e.target.value)} placeholder="approved" />
        </Field>
        <Field label="Payload" description="Any JSON value. The waiting step outputs it as payload." error={parsed.ok ? undefined : parsed.error}>
          <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} className="font-mono text-[12px]" placeholder='{"status": "done", "by": "ci"}' />
        </Field>
        <div className="flex justify-end">
          <Button variant="primary" size="sm" icon={<Send className="size-3.5" />} loading={send.isPending} disabled={!validName || !parsed.ok} onClick={() => parsed.ok && send.mutate({ name, payload: parsed.value })}>
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}
