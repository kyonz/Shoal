import { useInfiniteQuery } from "@tanstack/react-query";
import { Activity, Hand, RefreshCw } from "lucide-react";
import { useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { client, unwrap } from "@/api/client";
import { useResources } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { between, dateTime, relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Button, Card, EmptyState, ErrorNotice, NativeSelect, Page, PageHeader, Skeleton, StatusBadge, Table, Td, Th, Tooltip, Tr } from "@/components/ui";
import { actorName } from "@/lib/actor";

const STATUSES = ["", "running", "waiting", "succeeded", "failed", "cancelled", "pending"];

export default function RunsList() {
  const ns = useNs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const workflow = params.get("workflow") ?? "";
  const workflows = useResources("workflows", ns);

  const runs = useInfiniteQuery({
    queryKey: ["runs-page", ns, status, workflow],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        client.GET("/v1/namespaces/{namespace}/runs", {
          params: { path: { namespace: ns }, query: { status: status || undefined, workflow: workflow || undefined, cursor: pageParam, limit: 50 } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 5000,
  });
  const items = useMemo(() => runs.data?.pages.flatMap((p) => p.items) ?? [], [runs.data]);

  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  return (
    <Page wide>
      <PageHeader
        title="Runs"
        description="Every execution in this namespace, live. Waiting runs may need your approval."
        actions={
          <Tooltip content="Refreshes every 5 seconds">
            <Button variant="ghost" icon={<RefreshCw className={cn("size-3.5", runs.isFetching && "animate-spin")} />} onClick={() => void runs.refetch()}>
              Refresh
            </Button>
          </Tooltip>
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-lg border border-border bg-panel p-0.5 shadow-card">
          {STATUSES.map((s) => (
            <button
              key={s || "all"}
              onClick={() => set("status", s)}
              className={cn(
                "h-7 rounded-md px-2.5 text-xs font-medium capitalize transition-colors",
                status === s ? "bg-accent-soft text-accent" : "text-muted hover:text-fg",
              )}
            >
              {s === "waiting" ? (
                <span className="flex items-center gap-1">
                  <Hand className="size-3" /> waiting
                </span>
              ) : (
                s || "All"
              )}
            </button>
          ))}
        </div>
        <NativeSelect value={workflow} onChange={(e) => set("workflow", e.target.value)} className="w-56">
          <option value="">All workflows</option>
          {(workflows.data ?? []).map((w) => (
            <option key={w.metadata.name}>{w.metadata.name}</option>
          ))}
        </NativeSelect>
      </div>
      {runs.error && <ErrorNotice error={runs.error} className="mb-4" />}
      <Card>
        {runs.isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : !items.length ? (
          <EmptyState
            icon={<Activity className="size-5" />}
            title={status || workflow ? "No runs match" : "No runs yet"}
            description={status || workflow ? "Try another filter." : "Start a run from a workflow, or let a trigger or campaign start them."}
            action={!status && !workflow && <Link to={nsPath(ns, "workflows")}><Button variant="primary">Go to workflows</Button></Link>}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Status</Th>
                <Th>Workflow</Th>
                <Th>Run</Th>
                <Th>Started by</Th>
                <Th>Source</Th>
                <Th>Started</Th>
                <Th className="text-right">Duration</Th>
                <Th className="text-right">Cost</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <Tr key={r.id} onClick={() => navigate(nsPath(ns, "runs", r.id))}>
                  <Td>
                    <StatusBadge status={r.status} />
                  </Td>
                  <Td>
                    <span className="font-mono text-[12.5px] font-medium">{r.workflow_name}</span>
                    <span className="ml-1.5 text-[11px] text-faint">v{r.workflow_version}</span>
                  </Td>
                  <Td className="font-mono text-[11.5px] text-faint">{r.id.slice(-10)}</Td>
                  <Td className="max-w-48 truncate text-muted">{(r.initiator as { display_name?: string })?.display_name ?? actorName(r.started_by, "—")}</Td>
                  <Td className="text-xs text-muted">
                    {r.campaign_id ? "campaign" : r.trigger_name ? `trigger · ${r.trigger_name}` : r.parent_run_id ? "sub-workflow" : (r.labels as Record<string, string>)?.rerun_of ? "rerun" : "manual"}
                  </Td>
                  <Td className="text-muted" title={dateTime(r.created_at)}>
                    {relTime(r.created_at)}
                  </Td>
                  <Td className="text-right text-muted tabular-nums">{between(r.started_at ?? r.created_at, r.finished_at)}</Td>
                  <Td className="text-right text-muted tabular-nums">{usd((r.usage as { cost_usd?: number }).cost_usd ?? null)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      {runs.hasNextPage && (
        <div className="mt-4 flex justify-center">
          <Button loading={runs.isFetchingNextPage} onClick={() => void runs.fetchNextPage()}>
            Load older runs
          </Button>
        </div>
      )}
    </Page>
  );
}
