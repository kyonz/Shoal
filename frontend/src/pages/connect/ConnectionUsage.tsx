/**
 * A connection's usage history: deliveries to its webhook (accepted or rejected, and what they fired),
 * the runs and steps that used it, and actions called or tests run from the UI or API.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight, FilterX, FlaskConical, History, PlayCircle, Webhook, Zap } from "lucide-react";
import { Fragment, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { rawJson } from "@/api/client";
import { Actor } from "@/lib/actor";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import { nsPath } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, JsonBlock, NativeSelect, Skeleton, Td, Th } from "@/components/ui";

type FiredTrigger = { trigger: string; namespace?: string; status: string; run_id?: string; campaign_id?: string; reason?: string };
type ActivityData = {
  request?: { bytes?: number; user_agent?: string | null; content_type?: string | null };
  auth?: string;
  registered_for?: string;
  event_types?: string[];
  events?: number;
  triggers?: FiredTrigger[];
  woken_runs?: string[];
  interactions?: number;
  actions?: string[];
  action?: string;
};
export type ConnectionActivity = {
  id: number;
  kind: "delivery" | "use" | "call" | "test" | string;
  status: string;
  reason?: string | null;
  actor?: string | null;
  run_id?: string | null;
  run_namespace?: string | null;
  workflow_name?: string | null;
  step_id?: string | null;
  count: number;
  data: ActivityData;
  created_at: string;
  updated_at: string;
};
type ActivityPage = {
  items: ConnectionActivity[];
  next_cursor?: string | null;
  retention_days: number;
  max_entries: number;
  counts: Record<string, Record<string, number>>;
  oldest?: string | null;
};

type Tone = "neutral" | "ok" | "warn" | "bad" | "info";
const STATUS: Record<string, { tone: Tone; label: string }> = {
  accepted: { tone: "ok", label: "Accepted" },
  queued: { tone: "info", label: "Queued" },
  rejected: { tone: "bad", label: "Rejected" },
  error: { tone: "bad", label: "Error" },
  ok: { tone: "ok", label: "OK" },
};
const TRIGGER_TONE: Record<string, Tone> = { fired: "ok", error: "bad", rate_limited: "warn", skipped: "neutral", triaged_out: "neutral", duplicate: "neutral" };

const KINDS = [
  { value: "", label: "Everything" },
  { value: "delivery", label: "Webhook deliveries" },
  { value: "use", label: "Used by runs" },
  { value: "call,test", label: "Calls and tests" },
];
const STATUSES = [
  { value: "", label: "Any outcome" },
  { value: "accepted,ok", label: "Accepted / OK" },
  { value: "rejected", label: "Rejected" },
  { value: "error", label: "Errors" },
  { value: "queued", label: "Queued" },
];
const RANGES = [
  { value: "", label: "Everything kept" },
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
];

function plural(n: number, one: string, many = `${one}s`) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** How many deliveries were accepted and rejected, and how many run steps used it, over everything kept. */
function Summary({ page }: { page: ActivityPage }) {
  const deliveries = page.counts.delivery ?? {};
  const accepted = (deliveries.accepted ?? 0) + (deliveries.queued ?? 0);
  const rejected = (deliveries.rejected ?? 0) + (deliveries.error ?? 0);
  const uses = Object.values(page.counts.use ?? {}).reduce((a, b) => a + b, 0);
  const calls = Object.values(page.counts.call ?? {}).reduce((a, b) => a + b, 0) + Object.values(page.counts.test ?? {}).reduce((a, b) => a + b, 0);
  const keep = [page.retention_days ? `${page.retention_days} days` : null, page.max_entries ? `up to ${page.max_entries.toLocaleString()} entries` : null].filter(Boolean).join(", ");
  const none = !(accepted || rejected || uses || calls);
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border px-4 py-3 text-[13px]">
      {(accepted > 0 || rejected > 0) && (
        <span className="flex items-center gap-1.5">
          <Webhook className="size-3.5 text-faint" /> {plural(accepted, "delivery", "deliveries")} accepted
          {rejected > 0 && <span className="text-bad">· {rejected.toLocaleString()} rejected</span>}
        </span>
      )}
      {uses > 0 && <span className="flex items-center gap-1.5"><PlayCircle className="size-3.5 text-faint" /> Used by {plural(uses, "run step")}</span>}
      {calls > 0 && <span className="flex items-center gap-1.5"><Zap className="size-3.5 text-faint" /> {plural(calls, "call or test", "calls and tests")}</span>}
      {none && <span className="text-muted">Nothing recorded yet</span>}
      <span className="ml-auto text-xs text-muted">
        {keep ? `Kept for ${keep}` : "Kept indefinitely"}
        {page.oldest ? ` · oldest ${relTime(page.oldest)}` : ""}
      </span>
    </div>
  );
}

function RunLink({ ns, id }: { ns: string; id: string }) {
  return (
    <Link onClick={(e) => e.stopPropagation()} to={nsPath(ns, "runs", id)} title={id} className="inline-flex items-center gap-1 whitespace-nowrap font-mono text-xs text-accent hover:underline">
      <PlayCircle className="size-3" />
      {id.length > 14 ? `${id.slice(0, 4)}…${id.slice(-8)}` : id}
    </Link>
  );
}

function What({ entry, ns }: { entry: ConnectionActivity; ns: string }) {
  const d = entry.data ?? {};
  if (entry.kind === "delivery") {
    const types = d.event_types?.length ? d.event_types.join(", ") : null;
    return (
      <span className="flex min-w-0 items-center gap-1.5 whitespace-nowrap" title="Delivery to the webhook URL">
        <Webhook className="size-3.5 shrink-0 text-faint" />
        {types ? <code className="truncate font-mono text-xs">{types}</code> : <span>Delivery</span>}
        {d.auth && <Badge>{d.auth}</Badge>}
        {d.registered_for && <span className="text-xs text-muted">for {d.registered_for}</span>}
      </span>
    );
  }
  if (entry.kind === "use") {
    const runNs = entry.run_namespace ?? ns;
    return (
      <span className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
        <PlayCircle className="size-3.5 shrink-0 text-faint" />
        {entry.workflow_name ? (
          <Link onClick={(e) => e.stopPropagation()} to={nsPath(runNs, "workflows", entry.workflow_name)} className="font-medium hover:underline">
            {entry.workflow_name}
          </Link>
        ) : (
          <span className="text-muted">A run in another namespace</span>
        )}
        {entry.step_id && <span className="text-muted">step <code className="font-mono text-xs">{entry.step_id}</code></span>}
        {runNs !== ns && <Badge>{runNs}</Badge>}
      </span>
    );
  }
  return (
    <span className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
      {entry.kind === "test" ? <FlaskConical className="size-3.5 shrink-0 text-faint" /> : <Zap className="size-3.5 shrink-0 text-faint" />}
      <span>{entry.kind === "test" ? "Connection tested" : "Action called"}</span>
      {d.action && <code className="font-mono text-xs text-muted">{d.action}</code>}
    </span>
  );
}

/** What came of it: the triggers a delivery fired and their runs, why it was rejected, or the run a step belongs to. */
function Result({ entry, ns }: { entry: ConnectionActivity; ns: string }) {
  const d = entry.data ?? {};
  if (entry.reason) return <span className="text-xs text-bad">{entry.reason}</span>;
  if (entry.kind === "delivery") {
    if (entry.status === "queued") return <span className="text-xs text-muted">Waiting for a worker</span>;
    const parts: ReactNode[] = (d.triggers ?? []).map((f) => (
      <span key={`${f.namespace}/${f.trigger}`} className="inline-flex items-center gap-1.5 whitespace-nowrap">
        <Link onClick={(e) => e.stopPropagation()} to={nsPath(f.namespace ?? ns, "triggers", f.trigger)} className="text-xs font-medium hover:underline">
          {f.trigger}
        </Link>
        <Badge tone={TRIGGER_TONE[f.status] ?? "neutral"}>{f.status.replace("_", " ")}</Badge>
        {f.run_id && <RunLink ns={f.namespace ?? ns} id={f.run_id} />}
      </span>
    ));
    if (d.woken_runs?.length) parts.push(<span key="woken" className="text-xs text-muted">woke {plural(d.woken_runs.length, "waiting run")}</span>);
    if (d.interactions) parts.push(<span key="interactions" className="text-xs text-muted">{plural(d.interactions, "interaction")}</span>);
    if (!parts.length) return <span className="text-xs text-muted">No trigger matched</span>;
    return <span className="flex flex-wrap items-center gap-x-3 gap-y-1">{parts}</span>;
  }
  if (entry.kind === "use") {
    return (
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {entry.run_id && <RunLink ns={entry.run_namespace ?? ns} id={entry.run_id} />}
        {d.actions?.length ? <code className="font-mono text-xs text-muted">{d.actions.join(", ")}</code> : null}
        {entry.count > 1 && <span className="text-xs text-muted">{plural(entry.count, "call")}</span>}
        {entry.actor && <span className="flex items-center gap-1 text-xs text-muted">by <Actor actor={entry.actor} /></span>}
      </span>
    );
  }
  return <Actor actor={entry.actor} />;
}

export function ConnectionUsage({ ns, name }: { ns: string; name: string }) {
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [range, setRange] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const filtered = !!(kind || status || range);

  const history = useInfiniteQuery({
    queryKey: ["connection-activity", ns, name, kind, status, range],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => {
      const q = new URLSearchParams({ limit: "50" });
      if (kind) q.set("kind", kind);
      if (status) q.set("status", status);
      if (range) q.set("since", new Date(Date.now() - Number(range) * 86400_000).toISOString());
      if (pageParam) q.set("before", pageParam);
      return rawJson<ActivityPage>(`/v1/namespaces/${ns}/connections/${name}/activity?${q}`);
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 15_000,
  });
  const first = history.data?.pages[0];
  const entries = history.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <Card>
      {first && <Summary page={first} />}
      <div className="flex flex-wrap gap-2 border-b border-border p-3">
        <NativeSelect className="w-auto" aria-label="What" value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
        </NativeSelect>
        <NativeSelect className="w-auto" aria-label="Outcome" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </NativeSelect>
        <NativeSelect className="w-auto" aria-label="When" value={range} onChange={(e) => setRange(e.target.value)}>
          {RANGES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </NativeSelect>
        {filtered && (
          <Button variant="ghost" size="icon" title="Clear filters" aria-label="Clear filters" onClick={() => { setKind(""); setStatus(""); setRange(""); }}>
            <FilterX className="size-4" />
          </Button>
        )}
      </div>
      {history.isLoading ? (
        <div className="space-y-2 p-4">
          {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-8" />)}
        </div>
      ) : history.error ? (
        <EmptyState icon={<History className="size-5" />} title="Could not load the history" description={history.error.message} />
      ) : !entries.length ? (
        <EmptyState
          icon={<History className="size-5" />}
          title={filtered ? "Nothing matches these filters" : "No usage yet"}
          description={filtered ? undefined : "Deliveries to this connection's webhook, and the runs, steps and people that use it, show up here."}
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr>
                <Th className="w-6" />
                <Th>When</Th>
                <Th>What</Th>
                <Th>Outcome</Th>
                <Th>Result</Th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const expanded = open === e.id;
                const s = e.kind === "use" ? { tone: "info" as Tone, label: "Used" } : (STATUS[e.status] ?? { tone: "neutral" as Tone, label: e.status });
                return (
                  <Fragment key={e.id}>
                    <tr onClick={() => setOpen(expanded ? null : e.id)} className={cn("cursor-pointer align-top transition-colors hover:bg-subtle/70", expanded && "bg-subtle/50")}>
                      <Td className="pr-0">
                        <ChevronRight className={cn("size-3.5 text-faint transition-transform", expanded && "rotate-90")} />
                      </Td>
                      <Td className="whitespace-nowrap text-muted" title={dateTime(e.created_at)}>{relTime(e.created_at)}</Td>
                      <Td className="max-w-72"><What entry={e} ns={ns} /></Td>
                      <Td><Badge tone={s.tone} dot>{s.label}</Badge></Td>
                      <Td className="max-w-96"><Result entry={e} ns={ns} /></Td>
                    </tr>
                    {expanded && (
                      <tr className="bg-subtle/30">
                        <td colSpan={5} className="border-b border-border px-4 py-3">
                          <div className="grid gap-4 md:grid-cols-[240px_1fr]">
                            <dl className="space-y-1.5 text-xs">
                              {(
                                [
                                  ["Time", dateTime(e.created_at)],
                                  ...(e.count > 1 || e.updated_at !== e.created_at ? [["Last", dateTime(e.updated_at)]] : []),
                                  ["Started by", e.actor],
                                  ["Sender", e.data?.request?.user_agent],
                                  ["Size", e.data?.request?.bytes != null ? `${e.data.request.bytes.toLocaleString()} bytes` : null],
                                  ["Registered for", e.data?.registered_for],
                                  ["Entry", `#${e.id}`],
                                ] as [string, string | null | undefined][]
                              )
                                .filter(([, v]) => v)
                                .map(([k, v]) => (
                                  <div key={k}>
                                    <dt className="text-faint">{k}</dt>
                                    <dd className="font-mono break-all">{v}</dd>
                                  </div>
                                ))}
                            </dl>
                            <JsonBlock value={e.data} maxHeight="260px" />
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
          {history.hasNextPage && (
            <div className="flex justify-center border-t border-border p-3">
              <Button loading={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
                Load older
              </Button>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
