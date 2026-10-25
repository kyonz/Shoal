import { Building2, Cable, Plus, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import type { Resource } from "@/api/client";
import { useIntegrations, useOrgConnections, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { SHARED_NS, nsPath, useNs, useOrgScope } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Page, PageHeader, Table, Td, Th, Tr } from "@/components/ui";
import { IntegrationMark, RowSkeleton, useSecrets } from "./shared";

export default function Connections() {
  const ns = useNs();
  const org = useOrgScope();
  const navigate = useNavigate();
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const filter = params.get("integration") ?? "";
  const [q, setQ] = useState("");
  const { data, isLoading, error } = useResources("connections", ns);
  const integrations = useIntegrations(org ? undefined : ns);
  const secrets = useSecrets(ns, can("secrets:read", ns));
  const fromOrg = useOrgConnections(ns, !org);
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
        icon={org ? <Building2 className="size-4" /> : <Cable className="size-4" />}
        title={org ? "Organisation connections" : "Connections"}
        description={
          org
            ? "Connections the whole organisation shares. Each one is available to every namespace, or only to the namespaces you pick; its secrets live in the shared namespace."
            : "Credentials and settings for an integration, used by actions, triggers and campaign targets in this namespace."
        }
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
                {org && <Th>Shared with</Th>}
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
                    {org && (
                      <Td>
                        <SharedWith spec={c.spec} />
                      </Td>
                    )}
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
            title={data?.length ? "No connections match" : org ? "No organisation connections yet" : "No connections yet"}
            description={
              org
                ? "Connect an integration once for the whole organisation, then choose which namespaces may use it."
                : "Connect GitHub, Jira or any installed integration so workflows can act on it and triggers can listen to it."
            }
            action={
              <Link to={org ? nsPath(SHARED_NS, "connections", "new") : nsPath(ns, "integrations")}>
                <Button variant="primary">{org ? "New organisation connection" : "Browse integrations"}</Button>
              </Link>
            }
          />
        )}
      </Card>
      {!org && !!fromOrg.data?.length && (
        <OrgConnections rows={fromOrg.data} titles={titles} filter={filter} canOpen={can("connections:read", SHARED_NS)} />
      )}
    </Page>
  );
}

function SharedWith({ spec }: { spec: Record<string, unknown> }) {
  const list = (spec.namespaces as string[] | undefined) ?? [];
  if (!list.length) return <span className="text-xs text-muted">All namespaces</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {list.map((n) => (
        <Badge key={n}>{n}</Badge>
      ))}
    </div>
  );
}

/** Organisation connections this namespace may use; admins manage them in the admin console. */
function OrgConnections({ rows, titles, filter, canOpen }: { rows: Resource[]; titles: Map<string, string>; filter: string; canOpen: boolean }) {
  const navigate = useNavigate();
  const shown = rows.filter((c) => !filter || c.spec.integration === filter);
  if (!shown.length) return null;
  return (
    <Card className="mt-5">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <Building2 className="size-4 text-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold">From your organisation</div>
          <div className="text-xs text-muted">
            Shared with this namespace by your admins. Use them by name like your own; a connection here with the same name takes precedence.
          </div>
        </div>
      </div>
      <Table>
        <tbody>
          {shown.map((c) => {
            const integration = String(c.spec.integration);
            return (
              <Tr key={c.metadata.name} onClick={canOpen ? () => navigate(nsPath(SHARED_NS, "connections", c.metadata.name)) : undefined}>
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
                <Td className="text-right">
                  <Badge tone="info">Organisation</Badge>
                </Td>
              </Tr>
            );
          })}
        </tbody>
      </Table>
    </Card>
  );
}
