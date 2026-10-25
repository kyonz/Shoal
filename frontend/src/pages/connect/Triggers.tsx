import { useQueries } from "@tanstack/react-query";
import { CalendarClock, Plus, Rocket, Workflow, Zap } from "lucide-react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { rawJson, type Resource, type Schemas } from "@/api/client";
import { usePutResource, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Page, PageHeader, StatusBadge, Switch, Table, Td, Th, Tr } from "@/components/ui";
import { RowSkeleton } from "./shared";

interface TriggerSpec {
  workflow?: string;
  campaign?: string;
  connection?: string;
  event: string;
  schedule?: string;
  timezone?: string;
  enabled?: boolean;
  filter?: string;
}

export function describeTrigger(spec: TriggerSpec) {
  return spec.event === "schedule" ? `${spec.schedule} (${spec.timezone ?? "UTC"})` : `${spec.connection} · ${spec.event}`;
}

export default function Triggers() {
  const ns = useNs();
  const navigate = useNavigate();
  const { can } = useAuth();
  const canWrite = can("triggers:write", ns);
  const { data, isLoading, error } = useResources("triggers", ns);
  const put = usePutResource("triggers", ns);

  const deliveries = useQueries({
    queries: (data ?? []).map((t) => ({
      queryKey: ["deliveries", ns, t.metadata.name, 1],
      queryFn: () => rawJson<Schemas["DeliveryOut"][]>(`/v1/namespaces/${ns}/triggers/${t.metadata.name}/deliveries?limit=1`),
      staleTime: 15_000,
    })),
  });

  const toggle = async (t: Resource, enabled: boolean) => {
    try {
      await put.mutateAsync({
        name: t.metadata.name,
        body: { metadata: { description: t.metadata.description, labels: t.metadata.labels }, spec: { ...t.spec, enabled } },
      });
      toast.success(`${t.metadata.name} ${enabled ? "enabled" : "disabled"}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not update trigger");
    }
  };

  return (
    <Page>
      <PageHeader
        icon={<Zap className="size-4" />}
        title="Triggers"
        description="Start a workflow or relaunch a campaign when an integration event arrives or on a schedule."
        actions={
          canWrite && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, "triggers", "new"))}>
              New trigger
            </Button>
          )
        }
      />
      <ErrorNotice error={error} className="mb-4" />
      <Card>
        {isLoading ? (
          <RowSkeleton />
        ) : data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Trigger</Th>
                <Th>When</Th>
                <Th>Starts</Th>
                <Th>Last delivery</Th>
                <Th>Enabled</Th>
              </tr>
            </thead>
            <tbody>
              {data.map((t, i) => {
                const s = t.spec as unknown as TriggerSpec;
                const last = deliveries[i]?.data?.[0];
                const enabled = s.enabled !== false;
                return (
                  <Tr key={t.metadata.name} onClick={() => navigate(nsPath(ns, "triggers", t.metadata.name))}>
                    <Td>
                      <div className="font-medium">{t.metadata.name}</div>
                      {t.metadata.description && <div className="max-w-xs truncate text-xs text-muted">{t.metadata.description}</div>}
                    </Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        {s.event === "schedule" ? (
                          <Badge tone="info">
                            <CalendarClock className="size-3" /> Schedule
                          </Badge>
                        ) : (
                          <Badge tone="accent">
                            <Zap className="size-3" /> Event
                          </Badge>
                        )}
                        <span className="font-mono text-xs text-muted">{describeTrigger(s)}</span>
                      </div>
                    </Td>
                    <Td>
                      <span className="inline-flex items-center gap-1.5 text-[13px]">
                        {s.campaign ? <Rocket className="size-3.5 text-muted" /> : <Workflow className="size-3.5 text-muted" />}
                        {s.campaign ?? s.workflow}
                      </span>
                    </Td>
                    <Td>
                      {last ? (
                        <div className="flex items-center gap-2">
                          <StatusBadge status={deliveryStatus(last.status)} />
                          <span className="text-xs text-muted">{relTime(last.created_at)}</span>
                        </div>
                      ) : (
                        <span className="text-xs text-faint">Never</span>
                      )}
                    </Td>
                    <Td onClick={(e) => e.stopPropagation()}>
                      <Switch checked={enabled} disabled={!canWrite || put.isPending} onCheckedChange={(v) => void toggle(t, v)} />
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<Zap className="size-5" />}
            title="No triggers yet"
            description="A trigger turns events, like a Jira ticket moving to “Ready for AI” or a GitHub review, or a cron schedule into workflow runs."
            action={canWrite && <Button variant="primary" onClick={() => navigate(nsPath(ns, "triggers", "new"))}>Create a trigger</Button>}
          />
        )}
      </Card>
    </Page>
  );
}

/** Map delivery statuses onto the shared status palette. */
export function deliveryStatus(s: string): string {
  if (s === "started" || s === "fired" || s === "launched") return "succeeded";
  if (s === "filtered" || s === "triaged_out" || s === "duplicate" || s === "ignored" || s === "skipped") return "skipped";
  if (s === "error") return "failed";
  if (s === "received") return "running";
  return s;
}
