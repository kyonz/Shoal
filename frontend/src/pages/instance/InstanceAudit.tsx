/** /instance/audit: instance-level events (tenants created, suspended or deleted, instance roles, tenant switches). */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Activity, ChevronRight } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { client, unwrap } from "@/api/client";
import { Actor } from "@/lib/actor";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import { Badge, Button, Card, EmptyState, Input, JsonBlock, Page, PageHeader, Segmented, Skeleton, Td, Th } from "@/components/ui";
import { instanceKeys, type AuditEntry } from "./api";

const PAGE = 100;

const FILTERS = [
  { value: "", label: "Everything" },
  { value: "tenant.", label: "Tenants" },
  { value: "account.", label: "Accounts" },
  { value: "auth.", label: "Sign-ins" },
];

function tone(action: string): "ok" | "bad" | "warn" | "info" | "accent" | "neutral" {
  if (/deleted|revoked/.test(action)) return "bad";
  if (/suspended/.test(action)) return "warn";
  if (/created|resumed/.test(action)) return "ok";
  if (/auth\./.test(action)) return "accent";
  if (/updated/.test(action)) return "info";
  return "neutral";
}

export default function InstanceAudit() {
  const [preset, setPreset] = useState("");
  const [custom, setCustom] = useState("");
  const [action, setAction] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => {
    const t = setTimeout(() => setAction(custom || preset), 250);
    return () => clearTimeout(t);
  }, [custom, preset]);

  const audit = useInfiniteQuery({
    queryKey: instanceKeys.audit(action),
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(client.GET("/v1/instance/audit", { params: { query: { action: action || undefined, before: pageParam, limit: PAGE } } })),
    getNextPageParam: (last: AuditEntry[]) => (last.length === PAGE ? last[last.length - 1].id : undefined),
  });
  const entries = audit.data?.pages.flat() ?? [];

  return (
    <Page wide>
      <PageHeader
        icon={<Activity className="size-4" />}
        title="Instance audit"
        description="Changes to the instance itself: tenants created, suspended, resumed or deleted, instance roles granted, and people switching tenants. What happens inside a tenant is in that tenant's own audit log."
      />
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <Segmented
            value={custom ? "__custom" : preset}
            onChange={(v) => {
              setCustom("");
              setPreset(v);
            }}
            options={FILTERS}
          />
          <Input className="w-56" mono placeholder="Action prefix" value={custom} onChange={(e) => setCustom(e.target.value)} />
        </div>
        {audit.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-8" />
            ))}
          </div>
        ) : !entries.length ? (
          <EmptyState icon={<Activity className="size-5" />} title={action ? "No entries match" : "No instance events yet"} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr>
                  <Th className="w-6" />
                  <Th>When</Th>
                  <Th>Actor</Th>
                  <Th>Action</Th>
                  <Th>Target</Th>
                  <Th>Source</Th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => {
                  const expanded = open === e.id;
                  return (
                    <Fragment key={e.id}>
                      <tr onClick={() => setOpen(expanded ? null : e.id)} className={cn("cursor-pointer transition-colors hover:bg-subtle/70", expanded && "bg-subtle/50")}>
                        <Td className="pr-0">
                          <ChevronRight className={cn("size-3.5 text-faint transition-transform", expanded && "rotate-90")} />
                        </Td>
                        <Td className="whitespace-nowrap text-muted" title={dateTime(e.created_at)}>
                          {relTime(e.created_at)}
                        </Td>
                        <Td className="max-w-56">
                          <Actor actor={e.actor} />
                        </Td>
                        <Td>
                          <Badge tone={tone(e.action)} className="font-mono">
                            {e.action}
                          </Badge>
                        </Td>
                        <Td className="max-w-72 truncate font-mono text-xs text-muted">{e.target ?? "—"}</Td>
                        <Td className="text-xs text-muted">{e.source ?? "—"}</Td>
                      </tr>
                      {expanded && (
                        <tr className="bg-subtle/30">
                          <td colSpan={6} className="border-b border-border px-4 py-3">
                            <div className="grid gap-4 md:grid-cols-[260px_1fr]">
                              <dl className="space-y-1.5 text-xs">
                                {(
                                  [
                                    ["Time", dateTime(e.created_at)],
                                    ["Actor", e.actor],
                                    ["Request id", e.request_id],
                                    ["IP", e.ip],
                                    ["User agent", e.user_agent],
                                  ] as const
                                ).map(([k, v]) => (
                                  <div key={k}>
                                    <dt className="text-faint">{k}</dt>
                                    <dd className="font-mono break-all">{v ?? "—"}</dd>
                                  </div>
                                ))}
                              </dl>
                              <JsonBlock value={e.data} maxHeight="240px" />
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {audit.hasNextPage && (
          <div className="flex justify-center border-t border-border p-3">
            <Button loading={audit.isFetchingNextPage} onClick={() => void audit.fetchNextPage()}>
              Load older entries
            </Button>
          </div>
        )}
      </Card>
    </Page>
  );
}
