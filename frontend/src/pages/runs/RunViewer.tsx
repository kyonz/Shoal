import { Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, Check, ChevronRight, Clock, Hand, RotateCcw, Workflow as WorkflowIcon, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { client, unwrap, type RunDetail, type Step } from "@/api/client";
import { keys, useResource, useRun } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { between, dateTime, relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  EmptyState,
  JsonBlock,
  SectionTitle,
  Spinner,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  Tooltip,
  useConfirm,
} from "@/components/ui";
import { edgeTypes } from "@/features/workflow/DependencyEdge";
import { toEdges, toNodes } from "@/features/workflow/flow";
import { autoLayout, decodeLayout } from "@/features/workflow/graph";
import { nodeTypes, type StepNodeType } from "@/features/workflow/StepNode";
import { stepType } from "@/features/workflow/stepTypes";
import { LAYOUT_ANNOTATION, type StepDoc } from "@/features/workflow/types";
import { EventRow, Terminal, Transcript } from "./EventViews";
import { CredentialBanner } from "./CredentialBanner";
import { SignalBanner, SignalsTab, useSignals } from "./Signals";
import { useRunEvents, type StreamState } from "./useRunEvents";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export default function RunViewerPage() {
  return (
    <ReactFlowProvider>
      <RunViewer />
    </ReactFlowProvider>
  );
}

function useNow(active: boolean) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function RunViewer() {
  const ns = useNs();
  const { runId } = useParams();
  const run = useRun(runId);
  const terminal = !!run.data && TERMINAL.has(run.data.status);
  const { events, state } = useRunEvents(runId, terminal);
  const [selected, setSelected] = useState<string | null>(null);
  const now = useNow(!terminal);

  if (run.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-5" />
      </div>
    );
  }
  if (run.error || !run.data) {
    return <EmptyState className="h-full" title="Run not found" description={(run.error as Error)?.message} action={<Link to={nsPath(ns, "runs")}><Button>All runs</Button></Link>} />;
  }
  const r = run.data;
  return (
    <div className="flex h-full flex-col">
      <RunHeader run={r} state={state} now={now} />
      <ApprovalBanner run={r} />
      <SignalBanner run={r} />
      <CredentialBanner run={r} events={events} />
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          <RunGraph run={r} selected={selected} onSelect={setSelected} />
        </div>
        <aside className="flex w-[460px] shrink-0 flex-col border-l border-border bg-panel">
          <SidePanel run={r} events={events} selected={selected} onSelect={setSelected} now={now} terminal={terminal} />
        </aside>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- header

function StreamPill({ state, terminal }: { state: StreamState; terminal: boolean }) {
  if (terminal || state === "ended") return null;
  const look = {
    live: { tip: "Receiving live updates", label: "Live", cls: "border-ok/25 bg-ok-soft text-ok" },
    polling: { tip: "Live events aren't available from this server, so the run refreshes every few seconds", label: "Refreshing", cls: "border-info/25 bg-info-soft text-info" },
    connecting: { tip: "Connecting to the live stream…", label: "Connecting", cls: "border-border bg-subtle text-muted" },
    reconnecting: { tip: "Reconnecting to the live stream…", label: "Reconnecting", cls: "border-warn/25 bg-warn-soft text-warn" },
  }[state];
  return (
    <Tooltip content={look.tip}>
      <span className={cn("flex h-6 items-center gap-1.5 rounded-md border px-2 text-[11px] font-medium", look.cls)}>
        <span className={cn("size-1.5 rounded-full bg-current", state === "live" && "animate-pulse")} />
        {look.label}
      </span>
    </Tooltip>
  );
}

function RunHeader({ run, state, now }: { run: RunDetail; state: StreamState; now: number }) {
  const ns = useNs();
  const navigate = useNavigate();
  const { can } = useAuth();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const terminal = TERMINAL.has(run.status);
  const cost = (run.usage as { cost_usd?: number }).cost_usd;
  const cancel = useMutation({
    mutationFn: () => unwrap(client.POST("/v1/runs/{run_id}/cancel", { params: { path: { run_id: run.id } } })),
    onSuccess: () => {
      toast.success("Cancellation requested");
      void qc.invalidateQueries({ queryKey: keys.run(run.id) });
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const rerun = useMutation({
    mutationFn: () => unwrap(client.POST("/v1/runs/{run_id}/rerun", { params: { path: { run_id: run.id } } })),
    onSuccess: (r) => {
      toast.success("Started a new run with the same inputs");
      navigate(nsPath(run.namespace, "runs", r.id));
    },
    onError: (e) => toast.error((e as Error).message),
  });
  return (
    <div className="flex min-h-12 shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-panel px-4 py-2">
      <Link to={nsPath(ns, "runs")} className="text-[13px] text-muted hover:text-fg">
        Runs
      </Link>
      <ChevronRight className="size-3.5 text-faint" />
      <Link to={nsPath(run.namespace, "workflows", run.workflow_name)} className="font-mono text-[13px] font-semibold hover:text-accent">
        {run.workflow_name}
      </Link>
      <Badge>v{run.workflow_version}</Badge>
      <span className="font-mono text-[11px] text-faint">{run.id}</span>
      <StatusBadge status={run.status} />
      <StreamPill state={state} terminal={terminal} />
      <div className="flex-1" />
      <div className="flex items-center gap-4 text-xs text-muted">
        <span title={dateTime(run.created_at)}>
          <span className="text-faint">by </span>
          {(run.initiator as { display_name?: string })?.display_name ?? run.started_by ?? "—"}
          {run.trigger_name && <span className="text-faint"> via trigger {run.trigger_name}</span>}
        </span>
        <span className="tabular-nums">
          <span className="text-faint">duration </span>
          {between(run.started_at ?? run.created_at, run.finished_at, now)}
        </span>
        {cost != null && (
          <span className="tabular-nums">
            <span className="text-faint">cost </span>
            {usd(cost)}
          </span>
        )}
        {run.campaign_id && (
          <Link to={nsPath(run.namespace, "campaign-runs", run.campaign_id)} className="text-accent hover:underline">
            campaign
          </Link>
        )}
        {run.parent_run_id && (
          <Link to={nsPath(run.namespace, "runs", run.parent_run_id)} className="text-accent hover:underline">
            parent run
          </Link>
        )}
      </div>
      {!terminal && can("runs:cancel", run.namespace) && (
        <Button
          variant="secondary"
          size="sm"
          icon={<Ban className="size-3.5" />}
          loading={cancel.isPending}
          onClick={async () => {
            if (await confirm({ title: "Cancel this run?", description: "Running steps are stopped and no further steps start.", confirmLabel: "Cancel run", danger: true })) cancel.mutate();
          }}
        >
          Cancel
        </Button>
      )}
      {can("runs:create", run.namespace) && (
        <Button variant={terminal ? "primary" : "secondary"} size="sm" icon={<RotateCcw className="size-3.5" />} loading={rerun.isPending} onClick={() => rerun.mutate()}>
          Rerun
        </Button>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- approvals

function ApprovalBanner({ run }: { run: RunDetail }) {
  // A decided step stays "waiting" until the run's next pass; don't offer the decision again.
  const pending = run.steps.filter((s) => s.type === "approval" && s.status === "waiting" && !(s.state as { decision?: unknown }).decision);
  if (!pending.length) return null;
  return (
    <div className="shrink-0 space-y-3 border-b border-warn/30 bg-warn-soft px-4 py-3">
      {pending.map((s) => (
        <ApprovalRequest key={s.step_id} run={run} step={s} />
      ))}
    </div>
  );
}

function ApprovalRequest({ run, step: s }: { run: RunDetail; step: Step }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (approved: boolean) =>
      unwrap(client.POST("/v1/runs/{run_id}/steps/{step_id}/approval", { params: { path: { run_id: run.id, step_id: s.step_id } }, body: { approved, comment: comment || null } })),
    onSuccess: (_, approved) => {
      toast.success(approved ? "Approved" : "Rejected");
      setComment("");
      void qc.invalidateQueries({ queryKey: keys.run(run.id) });
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const def = (run.workflow.steps as StepDoc[] | undefined)?.find((x) => x.id === s.step_id);
  const message = (s.state as { message?: string }).message ?? (def?.message as string) ?? "Approval required";
  const approvers = (def?.approvers as string[] | undefined) ?? [];
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="flex size-8 items-center justify-center rounded-lg bg-warn text-white">
        <Hand className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-fg">
          Waiting for approval <span className="font-mono font-normal text-muted">· {s.step_id}</span>
        </div>
        <div className="text-[13px] text-muted">{message}</div>
        {approvers.length > 0 && <div className="mt-0.5 text-xs text-faint">Approvers: {approvers.join(", ")}</div>}
      </div>
      {can("runs:approve", run.namespace) ? (
        <>
          <Textarea value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (optional)" className="min-h-8 w-64 py-1.5" rows={1} />
          <Button variant="secondary" icon={<X className="size-3.5" />} loading={decide.isPending && decide.variables === false} disabled={decide.isPending} onClick={() => decide.mutate(false)}>
            Reject
          </Button>
          <Button variant="primary" icon={<Check className="size-3.5" />} loading={decide.isPending && decide.variables === true} disabled={decide.isPending} onClick={() => decide.mutate(true)}>
            Approve
          </Button>
        </>
      ) : (
        <span className="text-xs text-muted">You don't have permission to approve runs here.</span>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- graph

function RunGraph({ run, selected, onSelect }: { run: RunDetail; selected: string | null; onSelect: (id: string | null) => void }) {
  const flow = useReactFlow();
  const wf = useResource("workflows", run.namespace, run.workflow_name);
  const steps = (run.workflow.steps as StepDoc[] | undefined) ?? [];
  const saved = decodeLayout((wf.data?.metadata as { annotations?: Record<string, string> } | undefined)?.annotations?.[LAYOUT_ANNOTATION]);
  const layoutKey = JSON.stringify(saved);
  const layout = useMemo(() => autoLayout(steps, Object.fromEntries(Object.entries(saved).filter(([id]) => steps.some((s) => s.id === id)))), [run.workflow, layoutKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const byId = useMemo(() => new Map(run.steps.map((s) => [s.step_id, s])), [run.steps]);
  const nodes = useMemo(
    () =>
      toNodes(
        steps,
        layout,
        (s) => {
          const st = byId.get(s.id);
          return { run: { status: st?.status ?? "pending", attempt: st?.attempt, started_at: st?.started_at, finished_at: st?.finished_at, wake_at: st?.wake_at } };
        },
        selected,
      ),
    [steps, layout, byId, selected],
  );
  const edges = useMemo(
    () =>
      toEdges(steps, (from, to) => {
        const a = byId.get(from)?.status;
        const b = byId.get(to)?.status;
        if (a === "succeeded" && (b === "running" || b === "waiting")) return { state: "active" };
        if (a === "succeeded" && b && b !== "pending" && b !== "skipped") return { state: "done" };
        return { state: "idle" };
      }),
    [steps, byId],
  );
  useEffect(() => {
    requestAnimationFrame(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
  }, [layoutKey, flow]);
  return (
    <ReactFlow<StepNodeType>
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      onNodeClick={(_, n) => onSelect(n.id)}
      onPaneClick={() => onSelect(null)}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
      minZoom={0.2}
      maxZoom={1.6}
      proOptions={{ hideAttribution: true }}
      deleteKeyCode={null}
    >
      <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  );
}

// --------------------------------------------------------------------------- side panel

function SidePanel({ run, events, selected, onSelect, now, terminal }: {
  run: RunDetail;
  terminal: boolean;
  events: import("@/api/client").RunEvent[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  now: number;
}) {
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    if (selected) setTab("step");
  }, [selected]);
  const step = selected ? run.steps.find((s) => s.step_id === selected) : undefined;
  const def = selected ? (run.workflow.steps as StepDoc[] | undefined)?.find((s) => s.id === selected) : undefined;
  // Show signals for workflows that wait on them, or once any has been sent.
  const signals = useSignals(run.id);
  const signalCount = signals.data?.length ?? 0;
  const hasSignals = signalCount > 0 || ((run.workflow.steps as StepDoc[] | undefined) ?? []).some((s) => s.type === "wait" && s.signal);
  return (
    <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
      <TabsList className="shrink-0 px-3">
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="step">{selected ? <span className="font-mono">{selected}</span> : "Step"}</TabsTrigger>
        <TabsTrigger value="events" count={events.length}>
          Events
        </TabsTrigger>
        {hasSignals && (
          <TabsTrigger value="signals" count={signalCount || undefined}>
            Signals
          </TabsTrigger>
        )}
      </TabsList>
      <TabsContent value="overview" className="min-h-0 flex-1 overflow-y-auto p-4">
        <Overview run={run} onSelect={onSelect} now={now} />
      </TabsContent>
      <TabsContent value="step" className="min-h-0 flex-1 overflow-y-auto p-4">
        {selected && def ? (
          <StepDetail step={step} def={def} events={events.filter((e) => e.step_id === selected)} now={now} />
        ) : (
          <EmptyState icon={<WorkflowIcon className="size-5" />} title="Select a step" description="Click a step in the graph to see its output, logs and agent transcript." />
        )}
      </TabsContent>
      <TabsContent value="signals" className="min-h-0 flex-1 overflow-y-auto p-4">
        <SignalsTab run={run} terminal={terminal} />
      </TabsContent>
      <TabsContent value="events" className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="relative space-y-3 before:absolute before:top-2 before:bottom-2 before:left-[9px] before:w-px before:bg-border">
          {events.map((e) => (
            <EventRow key={e.id} event={e} showStep onStep={(id) => onSelect(id)} />
          ))}
          {!events.length && <p className="text-center text-xs text-faint">No events yet.</p>}
        </div>
      </TabsContent>
    </Tabs>
  );
}

function Overview({ run, onSelect, now }: { run: RunDetail; onSelect: (id: string) => void; now: number }) {
  const order = (run.workflow.steps as StepDoc[] | undefined) ?? [];
  const byId = new Map(run.steps.map((s) => [s.step_id, s]));
  const start = new Date(run.started_at ?? run.created_at).getTime();
  const end = run.finished_at ? new Date(run.finished_at).getTime() : now;
  const span = Math.max(1, end - start);
  return (
    <div className="space-y-5">
      {run.error && run.status === "cancelled" && (
        <div className="rounded-lg border border-border bg-subtle p-3 text-[13px] text-muted">
          <div className="font-semibold text-fg">Run cancelled</div>
        </div>
      )}
      {run.error && run.status !== "cancelled" && (
        <div className="rounded-lg border border-bad/25 bg-bad-soft p-3 text-[13px] text-bad">
          <div className="font-semibold">Run failed</div>
          <div className="mt-0.5 break-words">{String((run.error as { message?: string }).message ?? JSON.stringify(run.error))}</div>
        </div>
      )}
      <div>
        <SectionTitle>Timeline</SectionTitle>
        <div className="space-y-1">
          {order.map((d) => {
            const s = byId.get(d.id);
            const info = stepType(d.type);
            const a = s?.started_at ? new Date(s.started_at).getTime() : null;
            const b = s?.finished_at ? new Date(s.finished_at).getTime() : a ? now : null;
            const left = a ? ((a - start) / span) * 100 : 0;
            const width = a && b ? Math.max(1.2, ((b - a) / span) * 100) : 0;
            const color = { succeeded: "bg-ok", failed: "bg-bad", running: "bg-info", waiting: "bg-warn" }[s?.status ?? ""] ?? "bg-border-strong";
            return (
              <button key={d.id} onClick={() => onSelect(d.id)} className="flex w-full items-center gap-2 rounded-md px-1 py-1 text-left hover:bg-subtle">
                <span className="flex size-5 shrink-0 items-center justify-center rounded text-white" style={{ background: info.color }}>
                  <info.icon className="size-3" />
                </span>
                <span className="w-28 truncate font-mono text-[11.5px]">{d.id}</span>
                <span className="relative h-2 flex-1 rounded-full bg-subtle">
                  {width > 0 && <span className={cn("absolute inset-y-0 rounded-full", color, s?.status === "running" && "animate-pulse")} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />}
                </span>
                <span className="w-14 text-right text-[11px] text-faint tabular-nums">{s?.started_at ? between(s.started_at, s.finished_at, now) : s?.status === "skipped" ? "skipped" : "—"}</span>
              </button>
            );
          })}
        </div>
      </div>
      <div>
        <SectionTitle>Inputs</SectionTitle>
        <JsonBlock value={run.inputs} />
      </div>
      {run.outputs && (
        <div>
          <SectionTitle>Outputs</SectionTitle>
          <JsonBlock value={run.outputs} />
        </div>
      )}
      <div>
        <SectionTitle>Details</SectionTitle>
        <dl className="grid grid-cols-[120px_1fr] gap-y-1.5 text-[12.5px]">
          <dt className="text-faint">Created</dt>
          <dd>{dateTime(run.created_at)}</dd>
          <dt className="text-faint">Finished</dt>
          <dd>{run.finished_at ? dateTime(run.finished_at) : "—"}</dd>
          {run.next_wake_at && (
            <>
              <dt className="text-faint">Next wake</dt>
              <dd>{relTime(run.next_wake_at)}</dd>
            </>
          )}
          <dt className="text-faint">Initiator</dt>
          <dd className="truncate">{(run.initiator as { actor?: string }).actor ?? run.started_by}</dd>
          {Object.keys(run.labels ?? {}).length > 0 && (
            <>
              <dt className="text-faint">Labels</dt>
              <dd className="flex flex-wrap gap-1">
                {Object.entries(run.labels).map(([k, v]) => (
                  <Badge key={k}>
                    {k}={String(v)}
                  </Badge>
                ))}
              </dd>
            </>
          )}
          {Object.keys(run.usage ?? {}).length > 0 && (
            <>
              <dt className="text-faint">Usage</dt>
              <dd className="font-mono text-[11.5px]">
                {Object.entries(run.usage).map(([k, v]) => (
                  <div key={k}>
                    {k}: {typeof v === "number" ? Math.round(v * 100) / 100 : String(v)}
                  </div>
                ))}
              </dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}

function StepDetail({ step, def, events, now }: { step: Step | undefined; def: StepDoc; events: import("@/api/client").RunEvent[]; now: number }) {
  const info = stepType(def.type);
  const status = step?.status ?? "pending";
  const out = (step?.output ?? {}) as { stdout?: string; stderr?: string };
  const logs = def.type === "script" && (out.stdout || out.stderr) ? out : null;
  const hasTranscript = events.some((e) => e.type.startsWith("agent."));
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        <div className="flex size-9 items-center justify-center rounded-lg text-white" style={{ background: info.color }}>
          <info.icon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[14px] font-semibold">{def.id}</span>
            <StatusBadge status={status} />
          </div>
          <div className="mt-0.5 text-xs text-muted">
            {info.label} · {info.summary(def)}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-center">
        <Metric label="Attempt" value={step?.attempt ? String(step.attempt) : "—"} />
        <Metric label="Duration" value={step?.started_at ? between(step.started_at, step.finished_at, now) : "—"} />
        <Metric label={step?.wake_at ? "Next check" : "Cost"} value={step?.wake_at ? relTime(step.wake_at) : usd((step?.usage as { cost_usd?: number } | undefined)?.cost_usd ?? null)} />
      </div>
      {status === "waiting" && step?.wake_at && (
        <div className="flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
          <Clock className="size-3.5" /> Waiting durably; the run wakes {relTime(step.wake_at)} or when a matching event arrives.
        </div>
      )}
      {def.type === "workflow" && step && <ChildRuns step={step} />}
      {step?.error && (
        <div>
          <SectionTitle>Error</SectionTitle>
          <JsonBlock value={step.error} />
        </div>
      )}
      {step?.output && (
        <div>
          <SectionTitle>Output</SectionTitle>
          <JsonBlock value={step.output} />
        </div>
      )}
      {logs && (
        <div>
          <SectionTitle>Logs</SectionTitle>
          <Terminal stdout={logs.stdout} stderr={logs.stderr} />
        </div>
      )}
      {hasTranscript && (
        <div>
          <SectionTitle>Agent transcript</SectionTitle>
          <Transcript events={events} />
        </div>
      )}
      <div>
        <SectionTitle>Events</SectionTitle>
        <div className="relative space-y-3 before:absolute before:top-2 before:bottom-2 before:left-[9px] before:w-px before:bg-border">
          {events.filter((e) => !(hasTranscript && (e.type === "agent.turn" || e.type === "agent.tool_result"))).map((e) => (
            <EventRow key={e.id} event={e} />
          ))}
          {!events.length && <p className="text-xs text-faint">This step has not started.</p>}
        </div>
      </div>
      <div>
        <SectionTitle>Definition</SectionTitle>
        <JsonBlock value={def} maxHeight="260px" />
      </div>
    </div>
  );
}

/** The child runs a sub-workflow step started, linked, with their outcome once the step has finished. */
function ChildRuns({ step }: { step: Step }) {
  const ns = useNs();
  const state = step.state as { children?: (string | null)[]; items?: unknown[] };
  const results = ((step.output as { results?: { run_id: string; status: string }[] } | null)?.results ??
    (step.error as { results?: { run_id: string; status: string }[] } | null)?.results ??
    []) as { run_id: string; status: string }[];
  const outcome = new Map(results.map((r) => [r.run_id, r.status]));
  const single = (step.output as { run_id?: string } | null)?.run_id ?? (step.error as { run_id?: string } | null)?.run_id;
  const children = state.children ?? (single ? [single] : []);
  if (!children.length) return null;
  const started = children.filter(Boolean).length;
  return (
    <div>
      <SectionTitle>
        Child runs{" "}
        <span className="font-normal text-faint">
          {started} of {children.length} started
        </span>
      </SectionTitle>
      <div className="space-y-1">
        {children.map((id, index) =>
          id ? (
            <Link key={id} to={nsPath(ns, "runs", id)} className="flex items-center gap-2 rounded-md px-1 py-1 text-[12.5px] hover:bg-subtle">
              <span className="w-8 text-right text-faint tabular-nums">#{index}</span>
              <span className="flex-1 truncate font-mono text-accent">{id}</span>
              {outcome.get(id) ? <StatusBadge status={outcome.get(id)!} /> : <Badge>started</Badge>}
            </Link>
          ) : (
            <div key={`pending-${index}`} className="flex items-center gap-2 px-1 py-1 text-[12.5px] text-faint">
              <span className="w-8 text-right tabular-nums">#{index}</span>
              <span className="flex-1">waiting for a free slot</span>
            </div>
          ),
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-subtle/40 px-2 py-2">
      <div className="text-[10.5px] tracking-wide text-faint uppercase">{label}</div>
      <div className="mt-0.5 truncate text-[13px] font-semibold tabular-nums">{value}</div>
    </div>
  );
}
