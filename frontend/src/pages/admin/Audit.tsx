import { useInfiniteQuery } from "@tanstack/react-query";
import { ChevronRight, Download, FilterX, ScrollText } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { toast } from "sonner";
import { client, raw, unwrap, type Schemas } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import { Badge, Button, Card, EmptyState, Input, JsonBlock, NativeSelect, Page, PageHeader, Skeleton, Td, Th } from "@/components/ui";

type Entry = Schemas["AuditOut"];

interface Filters {
  action: string;
  actor: string;
  target: string;
  namespace: string;
  outcome: string;
  since: string;
  until: string;
}

const EMPTY: Filters = { action: "", actor: "", target: "", namespace: "", outcome: "", since: "", until: "" };

const RANGES = [
  { value: "", label: "All time" },
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
];

function query(f: Filters) {
  const q: Record<string, string> = {};
  for (const [k, v] of Object.entries(f)) if (v) q[k] = k === "since" || k === "until" ? new Date(v).toISOString() : v;
  return q;
}

function useDebounced<T>(value: T, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

const OUTCOME_TONE: Record<string, "ok" | "bad" | "warn"> = { success: "ok", denied: "warn", error: "bad" };

function actionTone(action: string): "ok" | "bad" | "info" | "neutral" | "accent" {
  if (/deleted|revoked|removed|cancel/.test(action)) return "bad";
  if (/created|launched|granted/.test(action)) return "ok";
  if (/updated|changed|approval/.test(action)) return "info";
  if (/sign|login/.test(action)) return "accent";
  return "neutral";
}

export default function Audit() {
  const [f, setF] = useState<Filters>(EMPTY);
  const [range, setRange] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const namespaces = useNamespaces();
  const filters = useDebounced(f);

  const audit = useInfiniteQuery({
    queryKey: ["admin", "audit", filters],
    initialPageParam: undefined as number | undefined,
    queryFn: async ({ pageParam }) => {
      const data = await unwrap(client.GET("/v1/audit", { params: { query: { ...query(filters), before: pageParam, limit: 100 } as never } }));
      // Older servers returned a bare list.
      return Array.isArray(data) ? { items: data as Entry[], next_cursor: null } : (data as { items: Entry[]; next_cursor?: string | null });
    },
    getNextPageParam: (last) => (last.next_cursor ? Number(last.next_cursor) : undefined),
  });

  const entries = audit.data?.pages.flatMap((p) => p.items) ?? [];
  const set = (k: keyof Filters, v: string) => setF((x) => ({ ...x, [k]: v }));
  const active = Object.values(f).some(Boolean);

  const exportJsonl = async () => {
    try {
      const res = await raw(`/v1/audit/export?${new URLSearchParams(query(f))}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `shoal-audit-${new Date().toISOString().slice(0, 10)}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Page wide>
      <PageHeader
        title="Audit log"
        description="Every mutating API call, sign-in, approval and agent tool call, with who did it and from where."
        actions={
          <Button icon={<Download className="size-4" />} onClick={() => void exportJsonl()}>
            Export JSONL
          </Button>
        }
      />
      <Card>
        <div className="grid gap-2 border-b border-border p-3 sm:grid-cols-3 lg:grid-cols-6">
          <Input placeholder="Action prefix" title="e.g. workflow." value={f.action} onChange={(e) => set("action", e.target.value)} mono />
          <Input placeholder="Actor" title="e.g. user:dana@acme.com" value={f.actor} onChange={(e) => set("actor", e.target.value)} mono />
          <Input placeholder="Target prefix" value={f.target} onChange={(e) => set("target", e.target.value)} mono />
          <NativeSelect value={f.namespace} onChange={(e) => set("namespace", e.target.value)}>
            <option value="">All namespaces</option>
            {(namespaces.data ?? []).map((n) => (
              <option key={n.name} value={n.name}>
                {n.display_name}
              </option>
            ))}
          </NativeSelect>
          <NativeSelect value={f.outcome} onChange={(e) => set("outcome", e.target.value)}>
            <option value="">Any outcome</option>
            <option value="success">Success</option>
            <option value="denied">Denied</option>
            <option value="error">Error</option>
          </NativeSelect>
          <div className="flex gap-2">
            <NativeSelect
              value={range}
              onChange={(e) => {
                setRange(e.target.value);
                set("since", e.target.value ? new Date(Date.now() - Number(e.target.value) * 86400_000).toISOString() : "");
              }}
            >
              {RANGES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </NativeSelect>
            {active && (
              <Button
                variant="ghost"
                size="icon"
                title="Clear filters"
                aria-label="Clear filters"
                onClick={() => {
                  setF(EMPTY);
                  setRange("");
                }}
              >
                <FilterX className="size-4" />
              </Button>
            )}
          </div>
        </div>
        {audit.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-8" />
            ))}
          </div>
        ) : !entries.length ? (
          <EmptyState icon={<ScrollText className="size-5" />} title={active ? "No entries match these filters" : "No audit entries yet"} />
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
                  <Th>Namespace</Th>
                  <Th>Outcome</Th>
                  <Th>Source</Th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => {
                  const expanded = open === e.id;
                  return (
                    <Fragment key={e.id}>
                      <tr
                        onClick={() => setOpen(expanded ? null : e.id)}
                        className={cn("cursor-pointer transition-colors hover:bg-subtle/70", expanded && "bg-subtle/50")}
                      >
                        <Td className="pr-0">
                          <ChevronRight className={cn("size-3.5 text-faint transition-transform", expanded && "rotate-90")} />
                        </Td>
                        <Td className="whitespace-nowrap text-muted" title={dateTime(e.created_at)}>
                          {relTime(e.created_at)}
                        </Td>
                        <Td className="font-mono text-xs">{e.actor ?? "—"}</Td>
                        <Td>
                          <Badge tone={actionTone(e.action)} className="font-mono">
                            {e.action}
                          </Badge>
                        </Td>
                        <Td className="max-w-72 truncate font-mono text-xs text-muted">{e.target ?? "—"}</Td>
                        <Td className="text-xs text-muted">{e.namespace ?? "—"}</Td>
                        <Td>{e.outcome ? <Badge tone={OUTCOME_TONE[e.outcome] ?? "neutral"}>{e.outcome}</Badge> : "—"}</Td>
                        <Td className="text-xs text-muted">{e.source ?? "—"}</Td>
                      </tr>
                      {expanded && (
                        <tr className="bg-subtle/30">
                          <td colSpan={8} className="border-b border-border px-4 py-3">
                            <div className="grid gap-4 md:grid-cols-[260px_1fr]">
                              <dl className="space-y-1.5 text-xs">
                                {(
                                  [
                                    ["Time", dateTime(e.created_at)],
                                    ["Request id", e.request_id],
                                    ["IP", e.ip],
                                    ["User agent", e.user_agent],
                                    ["Entry", `#${e.id}`],
                                  ] as const
                                ).map(([k, v]) => (
                                  <div key={k}>
                                    <dt className="text-faint">{k}</dt>
                                    <dd className="font-mono break-all">{v ?? "—"}</dd>
                                  </div>
                                ))}
                              </dl>
                              <JsonBlock value={e.data} maxHeight="280px" />
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
        {!audit.hasNextPage && entries.length > 0 && (
          <div className="border-t border-border p-3 text-center text-xs text-faint">Beginning of the log · {entries.length} entries</div>
        )}
      </Card>
    </Page>
  );
}
