import { ChevronRight, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useIntegrations, useResources, type IntegrationCatalog, type JsonSchema } from "@/api/hooks";
import { resolve, unwrapNullable } from "@/components/forms/SchemaForm";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, JsonBlock, Page, PageHeader, Skeleton, Table, Tabs, TabsContent, TabsList, TabsTrigger, Td, Th, Tr } from "@/components/ui";
import { Crumb, IntegrationMark } from "./shared";

function typeLabel(s: JsonSchema, root: JsonSchema): string {
  const { schema, nullable } = unwrapNullable(s, root);
  let t: string;
  if (schema.enum) t = schema.enum.map((e) => JSON.stringify(e)).join(" | ");
  else if (schema.type === "array") t = `${typeLabel(schema.items ?? {}, root)}[]`;
  else if (Array.isArray(schema.type)) t = schema.type.join(" | ");
  else if (schema.anyOf) t = schema.anyOf.map((v) => typeLabel(v, root)).join(" | ");
  else t = (schema.type as string) ?? (schema.properties ? "object" : "any");
  return nullable ? `${t}?` : t;
}

/** A readable table of an object schema's fields. */
export function SchemaTable({ schema, empty = "No fields." }: { schema: JsonSchema | null | undefined; empty?: string }) {
  if (!schema) return <p className="text-xs text-faint">{empty}</p>;
  const s = resolve(schema, schema);
  const props = Object.entries(s.properties ?? {});
  if (!props.length) return <p className="text-xs text-faint">{empty}</p>;
  const required = new Set(s.required ?? []);
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className="w-full text-[12.5px]">
        <tbody>
          {props.map(([name, p]) => {
            const r = unwrapNullable(p, schema).schema;
            return (
              <tr key={name} className="border-b border-border align-top last:border-0">
                <td className="w-1/3 px-3 py-2">
                  <span className="font-mono font-medium">{name}</span>
                  {required.has(name) && <span className="ml-1 text-bad">*</span>}
                  <div className="mt-0.5 font-mono text-[11px] break-all text-faint">{typeLabel(p, schema)}</div>
                </td>
                <td className="px-3 py-2 text-muted">
                  {r.description ?? r.title ?? ""}
                  {r.default != null && (
                    <div className="mt-0.5 text-[11px] text-faint">
                      default <code className="font-mono">{JSON.stringify(r.default)}</code>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function Expandable({ title, badges, description, children }: { title: string; badges?: React.ReactNode; description?: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-border last:border-0">
      <button onClick={() => setOpen(!open)} className="flex w-full items-start gap-2 px-4 py-3 text-left hover:bg-subtle/60">
        <ChevronRight className={cn("mt-0.5 size-4 shrink-0 text-faint transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[13px] font-medium">{title}</span>
            {badges}
          </div>
          {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
        </div>
      </button>
      {open && <div className="px-4 pb-4 pl-10">{children}</div>}
    </div>
  );
}

export default function IntegrationDetail() {
  const ns = useNs();
  const { key = "" } = useParams();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { data, isLoading } = useIntegrations();
  const connections = useResources("connections", ns, { enabled: can("connections:read", ns) });
  const integration: IntegrationCatalog | undefined = data?.find((i) => i.key === key);
  const mine = (connections.data ?? []).filter((c) => c.spec.integration === key);

  if (isLoading)
    return (
      <Page>
        <Skeleton className="h-16" />
        <Skeleton className="mt-4 h-80" />
      </Page>
    );
  if (!integration)
    return (
      <Page>
        <EmptyState title="Integration not found" description={`No integration with key “${key}” is installed on this server.`} />
      </Page>
    );

  return (
    <Page>
      <PageHeader
        breadcrumb={<Crumb to={nsPath(ns, "integrations")}>Integrations</Crumb>}
        icon={<IntegrationMark integrationKey={key} size="sm" />}
        title={integration.title}
        description={integration.description}
        actions={
          can("connections:write", ns) && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(`${nsPath(ns, "connections", "new")}?integration=${key}`)}>
              New connection
            </Button>
          )
        }
      />
      <Tabs defaultValue="actions">
        <TabsList className="mb-4">
          <TabsTrigger value="actions" count={integration.actions.length}>
            Actions
          </TabsTrigger>
          <TabsTrigger value="events" count={integration.events.length}>
            Events
          </TabsTrigger>
          <TabsTrigger value="resources" count={integration.resources.length}>
            Resources
          </TabsTrigger>
          <TabsTrigger value="connections" count={mine.length}>
            Connections
          </TabsTrigger>
          <TabsTrigger value="setup">Connection settings</TabsTrigger>
        </TabsList>
        <TabsContent value="actions">
          <p className="mb-3 text-[13px] text-muted">
            Use these as <code className="font-mono">action</code> steps (<code className="font-mono">{key}.&lt;name&gt;</code>) or give them to agents as tools.
          </p>
          <Card>
            {integration.actions.map((a) => (
              <Expandable
                key={a.name}
                title={`${key}.${a.name}`}
                description={a.description}
                badges={a.mutating ? <Badge tone="warn">Mutating</Badge> : <Badge>Read-only</Badge>}
              >
                <div className="grid gap-4 lg:grid-cols-2">
                  <div>
                    <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Input</div>
                    <SchemaTable schema={a.input_schema} empty="Takes no input." />
                  </div>
                  <div>
                    <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Output</div>
                    <SchemaTable schema={a.output_schema} empty="Returns free-form JSON." />
                  </div>
                </div>
              </Expandable>
            ))}
            {!integration.actions.length && <EmptyState title="No actions" description="This integration only provides triggers." />}
          </Card>
        </TabsContent>
        <TabsContent value="events">
          <p className="mb-3 text-[13px] text-muted">Triggers listen for these events on a connection's webhook (or by polling). Wildcards like <code className="font-mono">pull_request.*</code> match every sub-type.</p>
          <Card>
            {integration.events.map((e) => (
              <Expandable key={e.name} title={e.name} description={e.description}>
                {e.example && Object.keys(e.example as object).length ? <JsonBlock value={e.example} /> : <p className="text-xs text-faint">No example payload published.</p>}
              </Expandable>
            ))}
            {!integration.events.length && <EmptyState title="No events" description="This integration does not start workflows." />}
          </Card>
        </TabsContent>
        <TabsContent value="resources">
          <p className="mb-3 text-[13px] text-muted">Resources can be queried through a connection and power campaign target selection.</p>
          <Card>
            {integration.resources.map((r) => (
              <Expandable key={r.name} title={r.name} description={r.description}>
                <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Filters</div>
                <SchemaTable schema={r.filter_schema} />
              </Expandable>
            ))}
            {!integration.resources.length && <EmptyState title="No resources" description="Nothing to query on this integration." />}
          </Card>
        </TabsContent>
        <TabsContent value="connections">
          <Card>
            {mine.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Description</Th>
                    <Th>Version</Th>
                  </tr>
                </thead>
                <tbody>
                  {mine.map((c) => (
                    <Tr key={c.metadata.name} onClick={() => navigate(nsPath(ns, "connections", c.metadata.name))}>
                      <Td className="font-medium">{c.metadata.name}</Td>
                      <Td className="text-muted">{c.metadata.description}</Td>
                      <Td>v{c.metadata.version}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                title={`No ${integration.title} connections in ${ns}`}
                description="A connection stores the credentials and settings this namespace uses for the integration."
                action={
                  can("connections:write", ns) && (
                    <Link to={`${nsPath(ns, "connections", "new")}?integration=${key}`}>
                      <Button variant="primary">Connect {integration.title}</Button>
                    </Link>
                  )
                }
              />
            )}
          </Card>
        </TabsContent>
        <TabsContent value="setup">
          <Card className="p-4">
            <p className="mb-3 text-[13px] text-muted">
              Fields a connection takes. Secret fields (<span className="font-mono">{integration.secret_fields.join(", ") || "none"}</span>) reference namespace secrets by name.
            </p>
            <SchemaTable schema={integration.connection_schema} />
          </Card>
        </TabsContent>
      </Tabs>
    </Page>
  );
}
