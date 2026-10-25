import { useMutation } from "@tanstack/react-query";
import { Database, Eye, List, Rocket, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { useDeleteResource, useIntegrations, usePutResource, useResource, useResources } from "@/api/hooks";
import { SchemaForm } from "@/components/forms/SchemaForm";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import {
  Button,
  Card,
  CardHeader,
  CodeEditor,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Skeleton,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  useConfirm,
} from "@/components/ui";
import { relTime } from "@/lib/format";
import { useCampaignRuns, useLaunchCampaign } from "./Campaigns";
import { ChoiceCard, Crumb, ErrorList, ExprInput, NAME_RE, VersionsPanel, YamlPanel, useConnectionCapabilities, useYamlSync } from "./shared";

interface Targets {
  connection?: string;
  resource?: string;
  filter?: Record<string, unknown>;
  items?: Record<string, unknown>[];
  key?: string;
  limit?: number;
}
interface Policy {
  concurrency?: number;
  runs_per_hour?: number;
  max_failures?: number;
  budget_usd?: number;
}
interface Spec {
  description?: string;
  workflow: string;
  targets: Targets;
  inputs?: Record<string, unknown>;
  policy?: Policy;
}

function clean(spec: Spec): Record<string, unknown> {
  const t: Targets = { ...spec.targets };
  if (t.items) {
    delete t.connection;
    delete t.resource;
    delete t.filter;
  } else delete t.items;
  if (t.filter && !Object.keys(t.filter).length) delete t.filter;
  if (!t.key) delete t.key;
  if (!t.limit) delete t.limit;
  const policy = Object.fromEntries(Object.entries(spec.policy ?? {}).filter(([, v]) => v != null && v !== ""));
  const out: Record<string, unknown> = { workflow: spec.workflow, targets: t };
  if (spec.inputs && Object.keys(spec.inputs).length) out.inputs = spec.inputs;
  if (Object.keys(policy).length) out.policy = policy;
  return out;
}

export default function CampaignEditor() {
  const ns = useNs();
  const { name: routeName } = useParams();
  const isNew = !routeName;
  const navigate = useNavigate();
  const confirm = useConfirm();
  const { can } = useAuth();
  const canWrite = can("campaigns:write", ns);

  const existing = useResource("campaigns", ns, routeName);
  const workflows = useResources("workflows", ns, { enabled: can("workflows:read", ns) });
  const connections = useResources("connections", ns, { enabled: can("connections:read", ns) });
  const integrations = useIntegrations();
  const put = usePutResource("campaigns", ns);
  const del = useDeleteResource("campaigns", ns);
  const launch = useLaunchCampaign(ns);
  const runs = useCampaignRuns(ns, !isNew);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [spec, setSpec] = useState<Spec>({ workflow: "", targets: { key: "{{ target.id }}" }, policy: { concurrency: 5 } });
  const [itemsText, setItemsText] = useState('[\n  {"id": "one"},\n  {"id": "two"}\n]');
  const [itemsError, setItemsError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState("settings");

  useEffect(() => {
    if (existing.data && !loaded) {
      const s = existing.data.spec as unknown as Spec;
      setName(existing.data.metadata.name);
      setDescription(existing.data.metadata.description ?? "");
      setSpec({ ...s, targets: s.targets ?? {} });
      if (s.targets?.items) setItemsText(JSON.stringify(s.targets.items, null, 2));
      setLoaded(true);
    }
  }, [existing.data, loaded]);

  const setTargets = (patch: Partial<Targets>) => setSpec((s) => ({ ...s, targets: { ...s.targets, ...patch } }));
  const setPolicy = (patch: Partial<Policy>) => setSpec((s) => ({ ...s, policy: { ...s.policy, ...patch } }));
  const mode: "query" | "items" = spec.targets.items ? "items" : "query";

  const conn = connections.data?.find((c) => c.metadata.name === spec.targets.connection);
  const catalog = integrations.data?.find((i) => i.key === conn?.spec.integration);
  const caps = useConnectionCapabilities(ns, spec.targets.connection || undefined);
  // Prefer the connection's own resources (remote integrations), else the catalogue.
  const resourceList = caps.data?.resources ?? catalog?.resources;
  const resourceDef = resourceList?.find((r) => r.name === spec.targets.resource);
  const workflow = useResource("workflows", ns, spec.workflow || undefined);
  const wfInputs = (workflow.data?.spec.inputs ?? {}) as Record<string, { type?: string; description?: string; default?: unknown }>;

  const cleaned = useMemo(() => clean(spec), [spec]);
  const yaml = useYamlSync("Campaign", name, description, cleaned, (p) => {
    setDescription(p.description ?? "");
    const s = p.spec as unknown as Spec;
    setSpec({ ...s, targets: s.targets ?? {} });
    if (s.targets?.items) setItemsText(JSON.stringify(s.targets.items, null, 2));
  });

  const nameError = isNew && name && !NAME_RE.test(name) ? "Lowercase letters, digits and dashes." : undefined;
  const lastRun = runs.data?.find((r) => r.name === name);

  const save = async () => {
    try {
      const res = await put.mutateAsync({ name, body: { metadata: { description: description || null }, spec: cleaned } });
      toast.success(isNew ? `Campaign ${name} created` : `Saved ${name} (v${res.metadata.version})`);
      if (isNew) navigate(nsPath(ns, "campaigns", name), { replace: true });
    } catch {
      /* inline */
    }
  };

  const doLaunch = async () => {
    const ok = await confirm({
      title: `Launch ${name}?`,
      description: "This resolves the target set now and starts runs within the policy limits. You can pause or cancel it from its dashboard.",
      confirmLabel: "Launch",
    });
    if (ok) launch.mutate(name);
  };

  const remove = async () => {
    if (!(await confirm({ title: `Delete campaign ${name}?`, description: "Past launches are kept.", confirmLabel: "Delete", danger: true }))) return;
    await del.mutateAsync(name);
    navigate(nsPath(ns, "campaigns") + "?tab=definitions");
  };

  if (!isNew && existing.isLoading)
    return (
      <Page>
        <Skeleton className="h-14" />
        <Skeleton className="mt-4 h-96" />
      </Page>
    );
  if (!isNew && existing.error)
    return (
      <Page>
        <EmptyState title="Campaign not found" description={existing.error.message} action={<Link to={nsPath(ns, "campaigns")}><Button>Back to campaigns</Button></Link>} />
      </Page>
    );

  return (
    <Page wide>
      <PageHeader
        breadcrumb={<Crumb to={`${nsPath(ns, "campaigns")}?tab=definitions`}>Campaigns</Crumb>}
        icon={<Rocket className="size-4" />}
        title={isNew ? "New campaign" : name}
        description={isNew ? "A workflow × a target set × policies." : description || undefined}
        actions={
          <>
            {!isNew && canWrite && (
              <Button variant="ghost" icon={<Trash2 className="size-4" />} onClick={() => void remove()}>
                Delete
              </Button>
            )}
            {canWrite && (
              <Button variant={isNew ? "primary" : "secondary"} icon={<Save className="size-4" />} loading={put.isPending} disabled={!name || !!nameError || !!yaml.error || !!itemsError} onClick={() => void save()}>
                {isNew ? "Create campaign" : "Save"}
              </Button>
            )}
            {!isNew && can("campaigns:launch", ns) && (
              <Button variant="primary" icon={<Rocket className="size-4" />} loading={launch.isPending} onClick={() => void doLaunch()}>
                Launch
              </Button>
            )}
          </>
        }
      />
      <div className="grid gap-5 xl:grid-cols-[1fr_360px]">
        <div className="min-w-0">
          <Tabs
            value={tab}
            onValueChange={(t) => {
              if (t === "yaml") yaml.open();
              setTab(t);
            }}
          >
            <TabsList className="mb-4">
              <TabsTrigger value="settings">Settings</TabsTrigger>
              <TabsTrigger value="yaml">YAML</TabsTrigger>
              {!isNew && <TabsTrigger value="history">History</TabsTrigger>}
            </TabsList>
            <TabsContent value="settings">
              <fieldset disabled={!canWrite} className="space-y-5">
                <ErrorList error={put.error} />
                <Card className="p-4">
                  <div className="grid gap-3.5 sm:grid-cols-2">
                    <Field label="Name" required error={nameError}>
                      <Input mono value={name} disabled={!isNew} autoFocus={isNew} onChange={(e) => setName(e.target.value)} placeholder="coverage-uplift" />
                    </Field>
                    <Field label="Description">
                      <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Every active repo to 100% coverage" />
                    </Field>
                    <Field label="Workflow" required description="Runs once per target." className="sm:col-span-2">
                      <NativeSelect value={spec.workflow} onChange={(e) => setSpec((s) => ({ ...s, workflow: e.target.value }))}>
                        <option value="">Choose a workflow…</option>
                        {(workflows.data ?? []).map((w) => (
                          <option key={w.metadata.name} value={w.metadata.name}>
                            {w.metadata.name}
                            {w.metadata.description ? ` — ${w.metadata.description}` : ""}
                          </option>
                        ))}
                      </NativeSelect>
                    </Field>
                  </div>
                </Card>

                <Card>
                  <CardHeader title="Targets" description="What the workflow fans out over. Targets are snapshotted at launch, so the set is reproducible." />
                  <div className="space-y-3.5 p-4">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <ChoiceCard selected={mode === "query"} onClick={() => setTargets({ items: undefined })} icon={<Database />} title="Query an integration" description="e.g. every GitHub repository matching a filter." />
                      <ChoiceCard
                        selected={mode === "items"}
                        onClick={() => {
                          try {
                            setTargets({ items: JSON.parse(itemsText) });
                          } catch {
                            setTargets({ items: [] });
                          }
                        }}
                        icon={<List />}
                        title="A fixed list"
                        description="Paste the targets as a JSON array of objects."
                      />
                    </div>
                    {mode === "query" ? (
                      <>
                        <div className="grid gap-3.5 sm:grid-cols-2">
                          <Field label="Connection" required>
                            <NativeSelect value={spec.targets.connection ?? ""} onChange={(e) => setTargets({ connection: e.target.value, resource: undefined, filter: undefined })}>
                              <option value="">Choose a connection…</option>
                              {(connections.data ?? []).map((c) => (
                                <option key={c.metadata.name} value={c.metadata.name}>
                                  {c.metadata.name} ({String(c.spec.integration)})
                                </option>
                              ))}
                            </NativeSelect>
                          </Field>
                          <Field label="Resource" required description={resourceDef?.description}>
                            <NativeSelect value={spec.targets.resource ?? ""} disabled={!resourceList} onChange={(e) => setTargets({ resource: e.target.value, filter: undefined })}>
                              <option value="">{resourceList ? "Choose a resource…" : "Pick a connection first"}</option>
                              {(resourceList ?? []).map((r) => (
                                <option key={r.name} value={r.name}>
                                  {r.name}
                                </option>
                              ))}
                            </NativeSelect>
                          </Field>
                        </div>
                        {resourceDef && (
                          <div className="rounded-lg border border-border bg-subtle/30 p-3">
                            <div className="mb-2 text-[11px] font-semibold tracking-wider text-faint uppercase">Filter</div>
                            <SchemaForm schema={resourceDef.filter_schema} value={spec.targets.filter} onChange={(f) => setTargets({ filter: f })} />
                          </div>
                        )}
                      </>
                    ) : (
                      <Field label="Items" error={itemsError ?? undefined}>
                        <div className={cn("rounded-lg border bg-panel", itemsError ? "border-bad" : "border-border")}>
                          <CodeEditor
                            value={itemsText}
                            language="json"
                            minHeight="140px"
                            maxHeight="360px"
                            onChange={(t) => {
                              setItemsText(t);
                              try {
                                const v = JSON.parse(t);
                                if (!Array.isArray(v)) throw new Error("Must be a JSON array");
                                setTargets({ items: v });
                                setItemsError(null);
                              } catch (e) {
                                setItemsError((e as Error).message);
                              }
                            }}
                          />
                        </div>
                      </Field>
                    )}
                    <div className="grid gap-3.5 sm:grid-cols-[1fr_160px]">
                      <Field label="Target key" description="A stable id per target, used for dedupe and the dashboard.">
                        <ExprInput value={spec.targets.key ?? ""} onChange={(v) => setTargets({ key: v || undefined })} placeholder="{{ target.id }}" />
                      </Field>
                      <Field label="Limit" hint="optional">
                        <Input type="number" min={1} value={spec.targets.limit ?? ""} onChange={(e) => setTargets({ limit: e.target.value ? Number(e.target.value) : undefined })} placeholder="All" />
                      </Field>
                    </div>
                  </div>
                </Card>

                <Card>
                  <CardHeader title="Workflow inputs" description={<>Expressions over <code className="font-mono">target</code>, e.g. <code className="font-mono">{"{{ target.full_name }}"}</code>.</>} />
                  <div className="space-y-3 p-4">
                    {!spec.workflow ? (
                      <p className="text-[13px] text-muted">Choose a workflow first.</p>
                    ) : !Object.keys(wfInputs).length && !Object.keys(spec.inputs ?? {}).length ? (
                      <p className="text-[13px] text-muted">This workflow takes no inputs.</p>
                    ) : (
                      [...new Set([...Object.keys(wfInputs), ...Object.keys(spec.inputs ?? {})])].map((k) => {
                        const p = wfInputs[k];
                        const v = spec.inputs?.[k];
                        return (
                          <div key={k} className="grid gap-2 sm:grid-cols-[180px_1fr] sm:items-start">
                            <div className="pt-1.5">
                              <div className="font-mono text-[12.5px] font-medium">
                                {k}
                                {p && p.default == null && <span className="ml-0.5 text-bad">*</span>}
                              </div>
                              <div className="text-[11px] text-faint">{p ? `${p.type ?? "string"}${p.default != null ? ` · default ${JSON.stringify(p.default)}` : ""}` : "not a workflow input"}</div>
                            </div>
                            <ExprInput
                              value={typeof v === "string" ? v : v == null ? "" : JSON.stringify(v)}
                              placeholder={p?.description ?? "{{ target.… }}"}
                              onChange={(nv) =>
                                setSpec((s) => {
                                  const inputs = { ...(s.inputs ?? {}) };
                                  if (nv) inputs[k] = nv;
                                  else delete inputs[k];
                                  return { ...s, inputs };
                                })
                              }
                            />
                          </div>
                        );
                      })
                    )}
                  </div>
                </Card>

                <Card>
                  <CardHeader title="Policy" description="Limits the controller respects while it starts runs." />
                  <div className="grid gap-3.5 p-4 sm:grid-cols-2 lg:grid-cols-4">
                    <NumberField label="Concurrency" description="Max active runs." value={spec.policy?.concurrency} placeholder="5" onChange={(v) => setPolicy({ concurrency: v })} />
                    <NumberField label="Runs per hour" description="Rate limit." value={spec.policy?.runs_per_hour} placeholder="Unlimited" onChange={(v) => setPolicy({ runs_per_hour: v })} />
                    <NumberField label="Max failures" description="Pause after this many." value={spec.policy?.max_failures} placeholder="Never pause" onChange={(v) => setPolicy({ max_failures: v })} />
                    <NumberField label="Budget (USD)" description="Stop starting runs past this." value={spec.policy?.budget_usd} placeholder="No budget" step="any" onChange={(v) => setPolicy({ budget_usd: v })} />
                  </div>
                </Card>
              </fieldset>
            </TabsContent>
            <TabsContent value="yaml">
              <YamlPanel value={yaml.text} onChange={canWrite ? yaml.change : undefined} readOnly={!canWrite} error={yaml.error} />
              <ErrorList error={put.error} className="mt-3" />
            </TabsContent>
            {!isNew && (
              <TabsContent value="history">
                <VersionsPanel plural="campaigns" ns={ns} name={name} current={existing.data} />
              </TabsContent>
            )}
          </Tabs>
        </div>
        <aside className="space-y-4">
          {mode === "query" && <TargetPreview ns={ns} connection={spec.targets.connection} resource={spec.targets.resource} filter={spec.targets.filter} />}
          {mode === "items" && (
            <Card className="p-4 text-[13px] text-muted">
              <b className="text-fg">{spec.targets.items?.length ?? 0}</b> targets. Each item is available to inputs as <code className="font-mono">target</code>.
            </Card>
          )}
          {lastRun && (
            <Card>
              <CardHeader title="Last launch" />
              <Link to={nsPath(ns, "campaign-runs", lastRun.id)} className="flex items-center justify-between gap-2 px-4 py-3 hover:bg-subtle/60">
                <StatusBadge status={lastRun.status} />
                <span className="text-xs text-muted">{relTime(lastRun.created_at)}</span>
              </Link>
            </Card>
          )}
        </aside>
      </div>
    </Page>
  );
}

function NumberField({ label, description, value, onChange, placeholder, step }: { label: string; description: string; value?: number; onChange: (v?: number) => void; placeholder: string; step?: string }) {
  return (
    <Field label={label} description={description}>
      <Input type="number" min={0} step={step ?? 1} value={value ?? ""} placeholder={placeholder} onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />
    </Field>
  );
}

function TargetPreview({ ns, connection, resource, filter }: { ns: string; connection?: string; resource?: string; filter?: Record<string, unknown> }) {
  const { can } = useAuth();
  const query = useMutation({
    mutationFn: () =>
      rawJson<{ items: Record<string, unknown>[]; next_cursor?: string | null; total?: number | null }>(`/v1/namespaces/${ns}/connections/${connection}/resources/${resource}/query`, {
        method: "POST",
        body: JSON.stringify({ filter: filter ?? {}, limit: 10 }),
      }),
  });
  const ready = !!connection && !!resource && can("connections:use", ns);
  const items = query.data?.items ?? [];
  const label = (it: Record<string, unknown>) => String(it.full_name ?? it.name ?? it.key ?? it.title ?? it.id ?? JSON.stringify(it).slice(0, 60));
  return (
    <Card className="sticky top-4">
      <CardHeader title="Preview targets" description="Runs the query now (first 10) so you can check the filter." />
      <div className="space-y-3 p-4">
        <Button className="w-full justify-center" icon={<Eye className="size-4" />} disabled={!ready} loading={query.isPending} onClick={() => query.mutate()}>
          Preview
        </Button>
        <ErrorList error={query.error} />
        {query.data && (
          <div className="animate-fade-in">
            <div className="mb-1.5 text-xs text-muted">
              {query.data.total != null ? `${query.data.total} matching` : `${items.length}${query.data.next_cursor ? "+" : ""} matching`}
            </div>
            <ul className="divide-y divide-border rounded-lg border border-border">
              {items.map((it, i) => (
                <li key={i} className="truncate px-3 py-1.5 font-mono text-xs">
                  {label(it)}
                </li>
              ))}
              {!items.length && <li className="px-3 py-3 text-center text-xs text-faint">No targets match this filter.</li>}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}
