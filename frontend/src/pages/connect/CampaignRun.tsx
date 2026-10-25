import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Coins, Gauge, Pause, Play, RotateCcw, Rocket, Target } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { rawJson, type Schemas } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { between, dateTime, relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Button, Card, CardHeader, EmptyState, ErrorNotice, JsonBlock, Page, PageHeader, Skeleton, Stat, StatusBadge, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { TARGET_SEGMENTS, TargetBar, type CampaignRun as CampaignRunT } from "./Campaigns";
import { Crumb } from "./shared";

type TargetRow = Schemas["TargetOut"];

export default function CampaignRun() {
  const ns = useNs();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const { can } = useAuth();
  const [filter, setFilter] = useState<string>("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const run = useQuery({
    queryKey: ["campaign-run", id],
    queryFn: () => rawJson<CampaignRunT>(`/v1/campaign-runs/${id}`),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 5000 : false),
  });
  const active = run.data?.status === "running";
  const targets = useQuery({
    queryKey: ["campaign-targets", id, filter],
    queryFn: () => rawJson<TargetRow[]>(`/v1/campaign-runs/${id}/targets?${new URLSearchParams(filter ? { status: filter, limit: "5000" } : { limit: "5000" })}`),
    refetchInterval: active ? 5000 : false,
  });

  const op = useMutation({
    mutationFn: (operation: string) => rawJson<CampaignRunT>(`/v1/campaign-runs/${id}/${operation}`, { method: "POST" }),
    onSuccess: (data, operation) => {
      qc.setQueryData(["campaign-run", id], data);
      void qc.invalidateQueries({ queryKey: ["campaign-targets", id] });
      void qc.invalidateQueries({ queryKey: ["campaign-runs", ns] });
      toast.success({ pause: "Paused", resume: "Resumed", cancel: "Cancelled", retry: "Retrying failed targets" }[operation] ?? "Done");
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Failed"),
  });

  if (run.isLoading)
    return (
      <Page wide>
        <Skeleton className="h-14" />
        <div className="mt-4 grid grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="mt-4 h-80" />
      </Page>
    );
  if (run.error || !run.data)
    return (
      <Page>
        <EmptyState title="Campaign launch not found" description={run.error?.message} action={<Link to={nsPath(ns, "campaigns")}><Button>Back to campaigns</Button></Link>} />
      </Page>
    );

  const c = run.data;
  const s = c.stats as Record<string, number>;
  const total = s.total ?? 0;
  const done = (s.succeeded ?? 0) + (s.failed ?? 0) + (s.skipped ?? 0) + (s.cancelled ?? 0);
  const policy = (c.spec.policy ?? {}) as { concurrency?: number; budget_usd?: number; runs_per_hour?: number; max_failures?: number };
  const cost = s.cost_usd ?? 0;
  const manage = can("campaigns:manage", c.namespace);

  const cancel = async () => {
    if (await confirm({ title: `Cancel ${c.name}?`, description: "Pending targets will not start and running runs are cancelled.", confirmLabel: "Cancel campaign", danger: true })) op.mutate("cancel");
  };

  return (
    <Page wide>
      <PageHeader
        breadcrumb={
          <>
            <Crumb to={nsPath(ns, "campaigns")}>Campaigns</Crumb>
            <Link to={nsPath(ns, "campaigns", c.name)} className="hover:text-fg">
              {c.name}
            </Link>
          </>
        }
        icon={<Rocket className="size-4" />}
        title={
          <span className="flex items-center gap-2.5">
            {c.name}
            <StatusBadge status={c.status} />
          </span>
        }
        description={
          <>
            Runs <Link className="text-accent hover:underline" to={nsPath(ns, "workflows", String(c.spec.workflow))}>{String(c.spec.workflow)}</Link> · launched by {String(c.initiator?.display_name ?? c.created_by ?? "unknown")} {relTime(c.created_at)} · <span className="font-mono text-xs">{c.id}</span>
          </>
        }
        actions={
          manage && (
            <>
              {c.status === "running" && (
                <Button icon={<Pause className="size-4" />} loading={op.isPending && op.variables === "pause"} onClick={() => op.mutate("pause")}>
                  Pause
                </Button>
              )}
              {c.status === "paused" && (
                <Button variant="primary" icon={<Play className="size-4" />} loading={op.isPending && op.variables === "resume"} onClick={() => op.mutate("resume")}>
                  Resume
                </Button>
              )}
              {(s.failed ?? 0) > 0 && (
                <Button icon={<RotateCcw className="size-4" />} loading={op.isPending && op.variables === "retry"} onClick={() => op.mutate("retry")}>
                  Retry failed
                </Button>
              )}
              {(c.status === "running" || c.status === "paused" || c.status === "draft") && (
                <Button variant="ghost" icon={<Ban className="size-4" />} onClick={() => void cancel()}>
                  Cancel
                </Button>
              )}
            </>
          )
        }
      />
      {c.error && <ErrorNotice className="mb-4" error={String(c.error.message ?? JSON.stringify(c.error))} />}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Progress" icon={<Target className="size-4" />} value={total ? `${Math.round((done / total) * 100)}%` : "—"} sub={`${done} of ${total} targets finished`} />
        <Stat label="Succeeded" tone="ok" value={s.succeeded ?? 0} sub={(s.failed ?? 0) ? <span className="text-bad">{s.failed} failed</span> : "no failures"} />
        <Stat
          label="Active runs"
          tone="info"
          icon={<Gauge className="size-4" />}
          value={
            <>
              {s.running ?? 0}
              <span className="text-base font-normal text-faint"> / {policy.concurrency ?? 5}</span>
            </>
          }
          sub={policy.runs_per_hour ? `max ${policy.runs_per_hour} starts per hour` : `${s.pending ?? 0} pending`}
        />
        <Card className="p-4">
          <div className="flex items-center justify-between text-xs text-muted">
            <span>Cost</span>
            <Coins className="size-4 text-faint" />
          </div>
          <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">{usd(cost)}</div>
          {policy.budget_usd != null ? (
            <>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-subtle">
                <div className={cn("h-full rounded-full", cost / policy.budget_usd > 0.9 ? "bg-bad" : cost / policy.budget_usd > 0.7 ? "bg-warn" : "bg-accent")} style={{ width: `${Math.min(100, (cost / Math.max(policy.budget_usd, 1e-9)) * 100)}%` }} />
              </div>
              <div className="mt-1 text-xs text-faint">of {usd(policy.budget_usd)} budget</div>
            </>
          ) : (
            <div className="mt-0.5 text-xs text-faint">no budget set</div>
          )}
        </Card>
      </div>

      <Card className="mt-4 p-4">
        <TargetBar stats={s} className="h-3" />
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
          {TARGET_SEGMENTS.map((seg) => (
            <button key={seg.key} onClick={() => setFilter(filter === seg.key ? "" : seg.key)} className={cn("flex items-center gap-1.5 rounded-md px-1 text-xs transition-colors", filter === seg.key ? "text-fg" : "text-muted hover:text-fg")}>
              <span className={cn("size-2 rounded-full", seg.className)} />
              {seg.label}
              <span className="font-medium text-fg tabular-nums">{s[seg.key] ?? 0}</span>
            </button>
          ))}
          <span className="ml-auto text-xs text-faint">
            {c.started_at ? `Running for ${between(c.started_at, c.finished_at)}` : "Not started"}
            {c.finished_at && ` · finished ${dateTime(c.finished_at)}`}
          </span>
        </div>
      </Card>

      <Card className="mt-4">
        <CardHeader
          title="Targets"
          description={filter ? `Showing ${filter} targets` : "Every target and the run it got."}
          actions={
            <div className="flex flex-wrap gap-1">
              {["", ...TARGET_SEGMENTS.map((x) => x.key)].map((k) => (
                <button key={k || "all"} onClick={() => setFilter(k)} className={cn("h-6 rounded-md px-2 text-xs capitalize", filter === k ? "bg-accent-soft text-accent" : "text-muted hover:bg-subtle hover:text-fg")}>
                  {k || "All"}
                </button>
              ))}
            </div>
          }
        />
        {targets.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : targets.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Target</Th>
                <Th>Status</Th>
                <Th>Attempts</Th>
                <Th>Duration</Th>
                <Th>Cost</Th>
                <Th>Run</Th>
              </tr>
            </thead>
            <tbody>
              {targets.data.map((t) => (
                <TargetRowView key={t.id} t={t} ns={ns} expanded={expanded === t.id} onToggle={() => setExpanded(expanded === t.id ? null : t.id)} onOpenRun={() => t.run_id && navigate(nsPath(ns, "runs", t.run_id))} />
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState title={filter ? `No ${filter} targets` : "No targets"} description={filter ? "Try another status." : "The target set was empty when the campaign launched."} />
        )}
      </Card>
    </Page>
  );
}

function TargetRowView({ t, expanded, onToggle, onOpenRun }: { t: TargetRow; ns: string; expanded: boolean; onToggle: () => void; onOpenRun: () => void }) {
  return (
    <>
      <Tr onClick={onToggle} className={cn(expanded && "bg-subtle/50")}>
        <Td>
          <span className="font-mono text-[12.5px] font-medium">{t.key}</span>
        </Td>
        <Td>
          <StatusBadge status={t.status} />
        </Td>
        <Td className="tabular-nums">{t.attempts}</Td>
        <Td className="text-xs text-muted tabular-nums">{t.started_at ? between(t.started_at, t.finished_at) : "—"}</Td>
        <Td className="text-xs tabular-nums">{usd(t.cost_usd)}</Td>
        <Td onClick={(e) => e.stopPropagation()}>
          {t.run_id ? (
            <button onClick={onOpenRun} className="font-mono text-xs text-accent hover:underline">
              {t.run_id.slice(-10)}
            </button>
          ) : (
            <span className="text-xs text-faint">—</span>
          )}
        </Td>
      </Tr>
      {expanded && (
        <tr>
          <td colSpan={6} className="border-b border-border bg-subtle/30 px-4 py-3">
            <div className="grid gap-3 lg:grid-cols-2">
              <div>
                <div className="mb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Target</div>
                <JsonBlock value={t.data} maxHeight="220px" className="bg-panel" />
              </div>
              <div>
                <div className="mb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Result</div>
                {t.result ? <JsonBlock value={t.result} maxHeight="220px" className="bg-panel" /> : <p className="text-xs text-faint">No result yet.</p>}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
