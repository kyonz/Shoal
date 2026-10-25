import { Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { useResources } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Page, PageHeader, Skeleton, Table, Td, Th, Tr } from "@/components/ui";
import { KINDS } from "./kinds";
import { BlueprintBadge } from "./ResourceEditorPage";
import type { Resource } from "@/api/client";

const desc = (r: Resource) => r.metadata.description ?? (r.spec?.description as string | undefined);

export default function ResourceListPage({ plural }: { plural: string }) {
  const cfg = KINDS[plural];
  const ns = useNs();
  const navigate = useNavigate();
  const canWrite = useCan(`${plural}:write`, ns);
  const { data, isLoading, error } = useResources(cfg.plural, ns);
  const [q, setQ] = useState("");

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data ?? [])
      .filter((r) => {
        if (!needle) return true;
        const hay = [r.metadata.name, desc(r), ...Object.entries(r.metadata.labels ?? {}).map(([k, v]) => `${k}=${v}`), cfg.summary?.(r.spec)]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return hay.includes(needle);
      })
      .sort((a, b) => a.metadata.name.localeCompare(b.metadata.name));
  }, [data, q, cfg]);

  const create = canWrite ? (
    <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, plural, "new"))}>
      New {cfg.singular}
    </Button>
  ) : null;

  return (
    <Page>
      <PageHeader title={cfg.label} description={cfg.blurb} icon={<cfg.icon className="size-4" />} actions={create} />
      <Card>
        {(data?.length ?? 0) > 0 && (
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            <div className="relative w-72">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={`Search ${cfg.label.toLowerCase()}…`} className="pl-8" />
            </div>
            <span className="ml-auto text-xs text-faint">
              {rows.length} of {data?.length}
            </span>
          </div>
        )}
        {error ? (
          <ErrorNotice className="m-4" error={error} />
        ) : isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : !data?.length ? (
          <EmptyState icon={<cfg.icon className="size-5" />} title={`No ${cfg.label.toLowerCase()} in ${ns} yet`} description={cfg.blurb} action={create} />
        ) : !rows.length ? (
          <EmptyState title="No matches" description={`Nothing matches “${q}”.`} />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Details</Th>
                <Th>Labels</Th>
                <Th className="text-right">Version</Th>
                <Th className="text-right">Updated</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const sub = cfg.summary?.(r.spec);
                return (
                  <Tr key={r.metadata.name} onClick={() => navigate(nsPath(ns, plural, r.metadata.name))}>
                    <Td className="max-w-[340px]">
                      <Link to={nsPath(ns, plural, r.metadata.name)} className="font-medium hover:text-accent" onClick={(e) => e.stopPropagation()}>
                        {r.metadata.name}
                      </Link>
                      <BlueprintBadge labels={r.metadata.labels} />
                      {desc(r) && <div className="truncate text-xs text-muted">{desc(r)}</div>}
                    </Td>
                    <Td className="font-mono text-xs text-muted">{sub ?? "—"}</Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(r.metadata.labels ?? {}).filter(([k]) => !k.startsWith("shoal.dev/")).slice(0, 3).map(([k, v]) => (
                          <Badge key={k} className="font-mono">
                            {k}={v}
                          </Badge>
                        ))}
                      </div>
                    </Td>
                    <Td className="text-right text-xs text-muted tabular-nums">v{r.metadata.version}</Td>
                    <Td className="text-right text-xs whitespace-nowrap text-muted" title={r.metadata.updated_at}>
                      {relTime(r.metadata.updated_at)}
                      {r.metadata.updated_by && <div className="text-[11px] text-faint">{r.metadata.updated_by}</div>}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </Page>
  );
}
