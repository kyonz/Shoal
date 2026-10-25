import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowRight, Bot, CircleCheck, DollarSign, Hand, Plug, Store, Workflow as WorkflowIcon, Zap } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { rawJson } from "@/api/client";
import { useResources, useRuns } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, CardHeader, EmptyState, Page, PageHeader, Segmented, Skeleton, Stat, StatusIcon } from "@/components/ui";
import { actorName } from "@/lib/actor";

interface Stats {
  since: string;
  runs: Record<string, number>;
  total_runs: number;
  cost_usd: number;
  workflows: { workflow: string; runs: number; succeeded: number; failed: number; cost_usd: number }[];
  active_campaigns: number;
}

const BAR: Record<string, string> = {
  succeeded: "bg-ok",
  failed: "bg-bad",
  running: "bg-info",
  waiting: "bg-warn",
  cancelled: "bg-idle",
  pending: "bg-border-strong",
};

export default function Overview() {
  const ns = useNs();
  const navigate = useNavigate();
  const { me, can } = useAuth();
  const [days, setDays] = useState<"1" | "7" | "30">("7");
  const canRuns = can("runs:read", ns);
  const stats = useQuery({
    queryKey: ["stats", ns, days],
    queryFn: () => rawJson<Stats>(`/v1/stats?namespace=${encodeURIComponent(ns)}&days=${days}`),
    enabled: canRuns,
    refetchInterval: 15_000,
  });
  const recent = useRuns(ns, {}, 10_000, canRuns);
  const waiting = useRuns(ns, { status: "waiting" }, 10_000, canRuns);
  const workflows = useResources("workflows", ns, { enabled: can("workflows:read", ns) });

  const s = stats.data;
  const finished = (s?.runs.succeeded ?? 0) + (s?.runs.failed ?? 0);
  const successRate = finished ? Math.round(((s?.runs.succeeded ?? 0) / finished) * 100) : null;
  const firstName = (me?.display_name ?? "").split(" ")[0];
  const noWorkflows = workflows.data && workflows.data.length === 0;

  return (
    <Page wide>
      <PageHeader
        title={firstName && me?.kind !== "service" ? `Welcome back, ${firstName}` : "Overview"}
        description={`What's happening in the ${ns} namespace.`}
        actions={
          <Segmented
            value={days}
            onChange={setDays}
            options={[
              { value: "1", label: "24h" },
              { value: "7", label: "7 days" },
              { value: "30", label: "30 days" },
            ]}
          />
        }
      />

      {noWorkflows && <GettingStarted ns={ns} />}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {stats.isLoading ? (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)
        ) : (
          <>
            <Stat label="Runs" value={s?.total_runs ?? 0} sub={`since ${relTime(s?.since)}`} icon={<Activity className="size-4" />} />
            <Stat label="Success rate" value={successRate == null ? "—" : `${successRate}%`} sub={`${s?.runs.failed ?? 0} failed`} tone={successRate != null && successRate < 80 ? "warn" : "ok"} icon={<CircleCheck className="size-4" />} />
            <Stat label="Waiting" value={waiting.data?.items.length ?? 0} sub="approvals, reviews and timers" tone="warn" icon={<Hand className="size-4" />} />
            <Stat label="Spend" value={usd(s?.cost_usd ?? 0)} sub={`${s?.active_campaigns ?? 0} active campaign${s?.active_campaigns === 1 ? "" : "s"}`} icon={<DollarSign className="size-4" />} />
          </>
        )}
      </div>

      {s && s.total_runs > 0 && (
        <Card className="mt-3 p-4">
          <div className="mb-2 flex items-center justify-between text-xs text-muted">
            <span>Runs by status</span>
            <span className="flex flex-wrap gap-3">
              {Object.entries(s.runs).map(([k, v]) => (
                <span key={k} className="flex items-center gap-1.5">
                  <span className={cn("size-2 rounded-full", BAR[k] ?? "bg-border-strong")} />
                  {k} <b className="font-semibold text-fg tabular-nums">{v}</b>
                </span>
              ))}
            </span>
          </div>
          <div className="flex h-2.5 overflow-hidden rounded-full bg-subtle">
            {Object.entries(s.runs).map(([k, v]) => (
              <div key={k} className={cn("h-full transition-all", BAR[k] ?? "bg-border-strong")} style={{ width: `${(v / s.total_runs) * 100}%` }} title={`${k}: ${v}`} />
            ))}
          </div>
        </Card>
      )}

      <div className="mt-5 grid gap-5 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader title="Recent runs" actions={<Link to={nsPath(ns, "runs")} className="flex items-center gap-1 text-xs text-accent hover:underline">All runs <ArrowRight className="size-3" /></Link>} />
          {recent.isLoading ? (
            <div className="space-y-2 p-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-8" />)}</div>
          ) : !recent.data?.items.length ? (
            <EmptyState icon={<Activity className="size-5" />} title="No runs yet" description="Runs appear here as soon as one starts." />
          ) : (
            <ul className="divide-y divide-border">
              {recent.data.items.slice(0, 8).map((r) => (
                <li key={r.id}>
                  <button onClick={() => navigate(nsPath(ns, "runs", r.id))} className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-subtle/60">
                    <StatusIcon status={r.status} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[12.5px] font-medium">{r.workflow_name}</span>
                      <span className="block truncate text-[11px] text-faint">
                        {(r.initiator as { display_name?: string })?.display_name ?? actorName(r.started_by)}
                        {r.trigger_name ? ` · trigger ${r.trigger_name}` : ""}
                        {r.campaign_id ? " · campaign" : ""}
                      </span>
                    </span>
                    <span className="text-xs text-faint">{relTime(r.created_at)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-5 lg:col-span-2">
          <Card>
            <CardHeader title="Needs attention" description="Runs waiting on an approval, a review or a timer." />
            {!waiting.data?.items.length ? (
              <div className="px-4 py-6 text-center text-[13px] text-muted">Nothing is waiting. 🎉</div>
            ) : (
              <ul className="divide-y divide-border">
                {waiting.data.items.slice(0, 6).map((r) => (
                  <li key={r.id}>
                    <Link to={nsPath(ns, "runs", r.id)} className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle/60">
                      <span className="flex size-7 items-center justify-center rounded-lg bg-warn-soft text-warn">
                        <Hand className="size-3.5" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-mono text-[12.5px] font-medium">{r.workflow_name}</span>
                        <span className="block text-[11px] text-faint">waiting since {relTime(r.started_at ?? r.created_at)}</span>
                      </span>
                      <ArrowRight className="size-3.5 text-faint" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card>
            <CardHeader title="Top workflows" />
            {!s?.workflows.length ? (
              <div className="px-4 py-6 text-center text-[13px] text-muted">No activity in this period.</div>
            ) : (
              <ul className="divide-y divide-border">
                {s.workflows.slice(0, 6).map((w) => {
                  const rate = w.runs ? Math.round((w.succeeded / w.runs) * 100) : 0;
                  return (
                    <li key={w.workflow}>
                      <Link to={nsPath(ns, "workflows", w.workflow)} className="flex items-center gap-3 px-4 py-2.5 hover:bg-subtle/60">
                        <WorkflowIcon className="size-4 text-faint" />
                        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{w.workflow}</span>
                        <Badge tone={w.failed ? "warn" : "ok"}>{rate}% ok</Badge>
                        <span className="w-14 text-right text-xs text-muted tabular-nums">{w.runs} runs</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </Page>
  );
}

function GettingStarted({ ns }: { ns: string }) {
  const steps = [
    { icon: Plug, title: "Connect your tools", text: "Add GitHub, Jira or any integration as a connection.", to: nsPath(ns, "integrations") },
    { icon: Bot, title: "Define an agent", text: "Pick a model, give it instructions, skills and tools.", to: nsPath(ns, "agents") },
    { icon: WorkflowIcon, title: "Build a workflow", text: "Drag steps onto the canvas and connect them.", to: nsPath(ns, "workflows", "new") },
    { icon: Zap, title: "Automate it", text: "Start runs from webhooks, schedules or campaigns.", to: nsPath(ns, "triggers") },
  ];
  return (
    <Card className="mb-5 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-gradient-to-r from-accent-soft to-transparent px-5 py-4">
        <div>
          <h2 className="text-[15px] font-semibold">Get started with Shoal</h2>
          <p className="text-[13px] text-muted">Four steps from zero to an automated agent workflow. Or adopt a ready-made blueprint.</p>
        </div>
        <Link to={nsPath(ns, "blueprints")}>
          <Button icon={<Store className="size-3.5" />}>Browse blueprints</Button>
        </Link>
      </div>
      <div className="grid divide-border sm:grid-cols-2 sm:divide-x lg:grid-cols-4">
        {steps.map((st, i) => (
          <Link key={st.title} to={st.to} className="group flex gap-3 p-4 hover:bg-subtle/50">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-panel text-accent shadow-card">
              <st.icon className="size-4" />
            </span>
            <span>
              <span className="block text-[13px] font-semibold">
                {i + 1}. {st.title}
              </span>
              <span className="block text-xs text-muted">{st.text}</span>
            </span>
          </Link>
        ))}
      </div>
    </Card>
  );
}
