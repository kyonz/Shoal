import { Cable, Plus, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useIntegrations, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Page, PageHeader, Table, Td, Th, Tr } from "@/components/ui";
import { IntegrationMark, RowSkeleton, useSecrets } from "./shared";

export default function Connections() {
  const ns = useNs();
  const navigate = useNavigate();
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const filter = params.get("integration") ?? "";
  const [q, setQ] = useState("");
  const { data, isLoading, error } = useResources("connections", ns);
  const integrations = useIntegrations();
  const secrets = useSecrets(ns, can("secrets:read", ns));
  const known = new Set((secrets.data ?? []).map((s) => s.name));
  const titles = new Map((integrations.data ?? []).map((i) => [i.key, i.title]));

  const rows = (data ?? []).filter(
    (c) =>
      (!filter || c.spec.integration === filter) &&
      (!q || `${c.metadata.name} ${c.metadata.description ?? ""} ${c.spec.integration}`.toLowerCase().includes(q.toLowerCase())),
  );

  return (
    <Page>
      <PageHeader
        icon={<Cable className="size-4" />}
        title="Connections"
        description="Credentials and settings for an integration, used by actions, triggers and campaign targets in this namespace."
        actions={
          can("connections:write", ns) && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, "connections", "new"))}>
              New connection
            </Button>
          )
        }
      />
      <ErrorNotice error={error} className="mb-4" />
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter connections" className="w-64 pl-8" />
          </div>
          {filter && (
            <Badge tone="accent">
              {titles.get(filter) ?? filter}
              <button className="ml-1 hover:text-fg" onClick={() => setParams({})} aria-label="Clear filter">
                ×
              </button>
            </Badge>
          )}
        </div>
        {isLoading ? (
          <RowSkeleton />
        ) : rows.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Integration</Th>
                <Th>Secrets</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const integration = String(c.spec.integration);
                const refs = Object.values((c.spec.secrets as Record<string, string>) ?? {});
                const missing = secrets.data ? refs.filter((r) => !known.has(r)) : [];
                return (
                  <Tr key={c.metadata.name} onClick={() => navigate(nsPath(ns, "connections", c.metadata.name))}>
                    <Td>
                      <div className="font-medium">{c.metadata.name}</div>
                      {c.metadata.description && <div className="max-w-md truncate text-xs text-muted">{c.metadata.description}</div>}
                    </Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <IntegrationMark integrationKey={integration} size="sm" />
                        <span>{titles.get(integration) ?? integration}</span>
                      </div>
                    </Td>
                    <Td>
                      {missing.length ? (
                        <Badge tone="warn" title={`Not set: ${missing.join(", ")}`}>
                          {missing.length} missing
                        </Badge>
                      ) : refs.length ? (
                        <Badge tone="ok">{refs.length} set</Badge>
                      ) : (
                        <span className="text-xs text-faint">none</span>
                      )}
                    </Td>
                    <Td className="text-xs text-muted">
                      {relTime(c.metadata.updated_at)} · v{c.metadata.version}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            icon={<Cable className="size-5" />}
            title={data?.length ? "No connections match" : "No connections yet"}
            description="Connect GitHub, Jira or any installed integration so workflows can act on it and triggers can listen to it."
            action={
              <Link to={nsPath(ns, "integrations")}>
                <Button variant="primary">Browse integrations</Button>
              </Link>
            }
          />
        )}
      </Card>
    </Page>
  );
}
