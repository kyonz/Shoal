import { useMutation, useQuery } from "@tanstack/react-query";
import { Plus, Rocket, Target } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { rawJson, type Resource, type Schemas } from "@/api/client";
import { useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { toastError } from "@/lib/setup";
import { Button, Card, EmptyState, ErrorNotice, Page, PageHeader, StatusBadge, Table, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th, Tr, useConfirm } from "@/components/ui";
import { RowSkeleton } from "./shared";

export type CampaignRun = Schemas["CampaignRunOut"];

export const TARGET_SEGMENTS: { key: string; label: string; className: string }[] = [
  { key: "succeeded", label: "Succeeded", className: "bg-ok" },
  { key: "running", label: "Running", className: "bg-info" },
  { key: "failed", label: "Failed", className: "bg-bad" },
  { key: "skipped", label: "Skipped", className: "bg-idle/60" },
  { key: "cancelled", label: "Cancelled", className: "bg-idle" },
  { key: "pending", label: "Pending", className: "bg-border-strong" },
];

/** Stacked bar of target statuses. */
export function TargetBar({ stats, className = "h-1.5" }: { stats: Record<string, unknown>; className?: string }) {
  const total = Number(stats.total ?? 0);
  return (
    <div className={`flex w-full overflow-hidden rounded-full bg-subtle ${className}`}>
      {total > 0 &&
        TARGET_SEGMENTS.filter((s) => s.key !== "pending").map((s) => {
          const n = Number(stats[s.key] ?? 0);
          return n ? <div key={s.key} title={`${s.label}: ${n}`} className={`${s.className} transition-[width] duration-500`} style={{ width: `${(n / total) * 100}%` }} /> : null;
        })}
    </div>
  );
}

export function useCampaignRuns(ns: string, enabled = true) {
  return useQuery({
    queryKey: ["campaign-runs", ns],
    queryFn: () => rawJson<CampaignRun[]>(`/v1/namespaces/${ns}/campaign-runs`),
    enabled,
    refetchInterval: (q) => ((q.state.data ?? []).some((c) => c.status === "running") ? 5000 : false),
  });
}

export function useLaunchCampaign(ns: string) {
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (name: string) => rawJson<CampaignRun>(`/v1/namespaces/${ns}/campaigns/${name}/launch`, { method: "POST", body: JSON.stringify({}) }),
    onSuccess: (run) => {
      toast.success(`Launched ${run.name}`);
      navigate(nsPath(ns, "campaign-runs", run.id));
    },
    onError: (e) => toastError(e, "Launch failed"),
  });
}

export default function Campaigns() {
  const ns = useNs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { can } = useAuth();
  const runs = useCampaignRuns(ns);
  const defs = useResources("campaigns", ns);
  const launch = useLaunchCampaign(ns);
  const confirm = useConfirm();
  const doLaunch = async (name: string) => {
    const ok = await confirm({
      title: `Launch ${name}?`,
      description: "This resolves the target set now and starts runs within the policy limits. You can pause or cancel it from its dashboard.",
      confirmLabel: "Launch",
    });
    if (ok) launch.mutate(name);
  };
  const tab = params.get("tab") ?? "runs";

  return (
    <Page>
      <PageHeader
        icon={<Rocket className="size-4" />}
        title="Campaigns"
        description="Fan one workflow out over many targets, like every repository in an org, with concurrency, rate and budget limits and per-target progress."
        actions={
          can("campaigns:write", ns) && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, "campaigns", "new"))}>
              New campaign
            </Button>
          )
        }
      />
      <Tabs value={tab} onValueChange={(t) => setParams({ tab: t })}>
        <TabsList className="mb-4">
          <TabsTrigger value="runs" count={runs.data?.length}>
            Launches
          </TabsTrigger>
          <TabsTrigger value="definitions" count={defs.data?.length}>
            Definitions
          </TabsTrigger>
        </TabsList>
        <TabsContent value="runs">
          <ErrorNotice error={runs.error} className="mb-4" />
          <Card>
            {runs.isLoading ? (
              <RowSkeleton />
            ) : runs.data?.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Campaign</Th>
                    <Th>Status</Th>
                    <Th className="w-[30%]">Progress</Th>
                    <Th>Cost</Th>
                    <Th>Started</Th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data.map((c) => {
                    const total = Number(c.stats.total ?? 0);
                    const done = ["succeeded", "failed", "skipped", "cancelled"].reduce((a, k) => a + Number(c.stats[k] ?? 0), 0);
                    const budget = (c.spec.policy as { budget_usd?: number } | undefined)?.budget_usd;
                    return (
                      <Tr key={c.id} onClick={() => navigate(nsPath(ns, "campaign-runs", c.id))}>
                        <Td>
                          <div className="font-medium">{c.name}</div>
                          <div className="text-xs text-muted">
                            {String(c.spec.workflow)} · <span className="font-mono">{c.id.slice(-8)}</span>
                          </div>
                        </Td>
                        <Td>
                          <StatusBadge status={c.status} />
                        </Td>
                        <Td>
                          <TargetBar stats={c.stats} />
                          <div className="mt-1 text-[11px] text-muted tabular-nums">
                            {done} / {total} targets
                            {Number(c.stats.failed ?? 0) > 0 && <span className="text-bad"> · {String(c.stats.failed)} failed</span>}
                          </div>
                        </Td>
                        <Td className="text-xs tabular-nums">
                          {usd(Number(c.stats.cost_usd ?? 0))}
                          {budget != null && <span className="text-faint"> / {usd(budget)}</span>}
                        </Td>
                        <Td className="text-xs text-muted">{relTime(c.started_at ?? c.created_at)}</Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                icon={<Target className="size-5" />}
                title="No campaigns launched yet"
                description="Define a campaign (a workflow, a target set and policies), then launch it to start runs for every target."
                action={
                  defs.data?.length ? (
                    <Button onClick={() => setParams({ tab: "definitions" })}>See definitions</Button>
                  ) : (
                    can("campaigns:write", ns) && (
                      <Button variant="primary" onClick={() => navigate(nsPath(ns, "campaigns", "new"))}>
                        Define a campaign
                      </Button>
                    )
                  )
                }
              />
            )}
          </Card>
        </TabsContent>
        <TabsContent value="definitions">
          <ErrorNotice error={defs.error} className="mb-4" />
          <Card>
            {defs.isLoading ? (
              <RowSkeleton />
            ) : defs.data?.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Workflow</Th>
                    <Th>Targets</Th>
                    <Th>Policy</Th>
                    <Th>Last launch</Th>
                    <Th />
                  </tr>
                </thead>
                <tbody>
                  {defs.data.map((d) => (
                    <DefinitionRow key={d.metadata.name} d={d} last={runs.data?.find((r) => r.name === d.metadata.name)} canLaunch={can("campaigns:launch", ns)} launching={launch.isPending && launch.variables === d.metadata.name} onLaunch={() => void doLaunch(d.metadata.name)} onOpen={() => navigate(nsPath(ns, "campaigns", d.metadata.name))} />
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                icon={<Rocket className="size-5" />}
                title="No campaign definitions"
                description="A definition names the workflow, how to find targets (e.g. GitHub repositories matching a filter) and the limits to respect."
                action={can("campaigns:write", ns) && <Button variant="primary" onClick={() => navigate(nsPath(ns, "campaigns", "new"))}>New campaign</Button>}
              />
            )}
          </Card>
        </TabsContent>
      </Tabs>
    </Page>
  );
}

function DefinitionRow({ d, last, canLaunch, launching, onLaunch, onOpen }: { d: Resource; last?: CampaignRun; canLaunch: boolean; launching: boolean; onLaunch: () => void; onOpen: () => void }) {
  const ns = useNs();
  const navigate = useNavigate();
  const t = (d.spec.targets ?? {}) as { connection?: string; resource?: string; items?: unknown[] };
  const p = (d.spec.policy ?? {}) as { concurrency?: number; budget_usd?: number; runs_per_hour?: number };
  return (
    <Tr onClick={onOpen}>
      <Td>
        <div className="font-medium">{d.metadata.name}</div>
        {d.metadata.description && <div className="max-w-xs truncate text-xs text-muted">{d.metadata.description}</div>}
      </Td>
      <Td className="text-[13px]">{String(d.spec.workflow)}</Td>
      <Td className="font-mono text-xs text-muted">{t.items ? `${t.items.length} static items` : `${t.connection} › ${t.resource}`}</Td>
      <Td className="text-xs text-muted">
        {p.concurrency ?? 5} at a time
        {p.runs_per_hour ? ` · ${p.runs_per_hour}/h` : ""}
        {p.budget_usd != null ? ` · ${usd(p.budget_usd)}` : ""}
      </Td>
      <Td>
        {last ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              navigate(nsPath(ns, "campaign-runs", last.id));
            }}
            className="flex items-center gap-2"
          >
            <StatusBadge status={last.status} />
            <span className="text-xs text-muted">{relTime(last.created_at)}</span>
          </button>
        ) : (
          <span className="text-xs text-faint">Never</span>
        )}
      </Td>
      <Td onClick={(e) => e.stopPropagation()}>
        {canLaunch && (
          <Button size="sm" icon={<Rocket className="size-3.5" />} loading={launching} onClick={onLaunch}>
            Launch
          </Button>
        )}
      </Td>
    </Tr>
  );
}
