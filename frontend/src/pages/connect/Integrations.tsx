import { ArrowRight, BookOpen, Code2, Plug, Search, Server, Zap } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { useIntegrations, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { nsPath, useNs } from "@/lib/ns";
import { plural } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Page, PageHeader, Skeleton } from "@/components/ui";
import { IntegrationMark } from "./shared";

const BUILTIN = new Set(["github", "jira", "slack", "webhook", "schedule", "http"]);

export default function Integrations() {
  const ns = useNs();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { data, isLoading, error } = useIntegrations();
  const connections = useResources("connections", ns, { enabled: can("connections:read", ns) });
  const [q, setQ] = useState("");

  const counts = new Map<string, number>();
  for (const c of connections.data ?? []) {
    const key = String(c.spec.integration ?? "");
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const list = (data ?? []).filter(
    (i) => !q || `${i.key} ${i.title} ${i.description}`.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <Page>
      <PageHeader
        icon={<Plug className="size-4" />}
        title="Integrations"
        description="Integrations let workflows react to events (triggers), act in other systems (actions) and look things up (resources). Connect one to give this namespace credentials for it."
        actions={
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search integrations" className="w-60 pl-8" />
          </div>
        }
      />
      <ErrorNotice error={error} className="mb-4" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {isLoading && Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-48 rounded-xl" />)}
        {list.map((i) => {
          const n = counts.get(i.key) ?? 0;
          return (
            <Card key={i.key} className="group flex flex-col p-4 transition-shadow hover:shadow-pop">
              <Link to={nsPath(ns, "integrations", i.key)} className="flex flex-1 flex-col">
                <div className="flex items-start gap-3">
                  <IntegrationMark integrationKey={i.key} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-[14px] font-semibold">{i.title}</h3>
                      {BUILTIN.has(i.key) ? <Badge tone="accent">Built-in</Badge> : <Badge>Plugin</Badge>}
                    </div>
                    <div className="font-mono text-[11px] text-faint">{i.key}</div>
                  </div>
                </div>
                <p className="mt-3 line-clamp-3 flex-1 text-[13px] text-muted">{i.description || "No description."}</p>
                <div className="mt-3 flex flex-wrap gap-1.5 text-[11px] text-muted">
                  <span className="rounded-md bg-subtle px-1.5 py-0.5">{plural(i.actions.length, "action")}</span>
                  <span className="rounded-md bg-subtle px-1.5 py-0.5">{plural(i.events.length, "event")}</span>
                  <span className="rounded-md bg-subtle px-1.5 py-0.5">{plural(i.resources.length, "resource")}</span>
                </div>
              </Link>
              <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                <span className="text-xs text-muted">
                  {n ? (
                    <Link className="text-accent hover:underline" to={`${nsPath(ns, "connections")}?integration=${i.key}`}>
                      {plural(n, "connection")}
                    </Link>
                  ) : (
                    "Not connected"
                  )}
                </span>
                {can("connections:write", ns) && (
                  <Button size="sm" variant={n ? "secondary" : "primary"} onClick={() => navigate(`${nsPath(ns, "connections", "new")}?integration=${i.key}`)}>
                    {n ? "Add another" : "Connect"}
                  </Button>
                )}
              </div>
            </Card>
          );
        })}
        {!isLoading && <BuildYourOwn />}
      </div>
      {!isLoading && !list.length && q && <EmptyState title="No integrations match" description={`Nothing matches “${q}”.`} />}
    </Page>
  );
}

function BuildYourOwn() {
  return (
    <Card className="relative flex flex-col overflow-hidden border-dashed bg-gradient-to-br from-accent-soft/70 via-panel to-panel p-4 sm:col-span-2 lg:col-span-3">
      <div className="flex flex-col gap-5 md:flex-row md:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Code2 className="size-4 text-accent" />
            <h3 className="text-[14px] font-semibold">Build your own integration</h3>
            <Badge tone="accent">Open platform</Badge>
          </div>
          <p className="mt-1.5 max-w-2xl text-[13px] text-muted">
            The built-ins use exactly the same public contract you do. Anything you build shows up here, its actions become workflow steps and agent tools, and its forms are rendered from its schemas, with no UI changes.
          </p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-border bg-panel/80 p-3">
              <div className="flex items-center gap-1.5 text-[13px] font-medium">
                <Zap className="size-3.5 text-accent" /> Python SDK
              </div>
              <p className="mt-1 text-xs text-muted">
                Subclass <code className="font-mono">shoal.sdk.Integration</code>, decorate actions with <code className="font-mono">@action</code>, test with <code className="font-mono">shoal.sdk.testing</code>, publish under the <code className="font-mono">shoal.integrations</code> entry point.
              </p>
              <pre className="mt-2 overflow-x-auto rounded-md bg-subtle px-2 py-1.5 font-mono text-[11px] text-muted">shoal integrations new linear</pre>
            </div>
            <div className="rounded-lg border border-border bg-panel/80 p-3">
              <div className="flex items-center gap-1.5 text-[13px] font-medium">
                <Server className="size-3.5 text-accent" /> Any language, over HTTP
              </div>
              <p className="mt-1 text-xs text-muted">
                Serve <code className="font-mono">GET /manifest</code> plus <code className="font-mono">/actions/&#123;name&#125;</code>, <code className="font-mono">/resources/&#123;name&#125;/query</code> and webhook endpoints, then register it as a remote integration connection.
              </p>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 flex-col gap-2">
          <Link to="/docs">
            <Button variant="primary" icon={<BookOpen className="size-4" />}>
              Read the SDK guide
            </Button>
          </Link>
          <Link to="/docs" className="flex items-center justify-center gap-1 text-xs text-muted hover:text-fg">
            Remote protocol reference <ArrowRight className="size-3" />
          </Link>
        </div>
      </div>
    </Card>
  );
}
