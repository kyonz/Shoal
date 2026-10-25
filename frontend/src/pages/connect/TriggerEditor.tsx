import { useMutation, useQuery } from "@tanstack/react-query";
import { CalendarClock, CheckCircle2, FlaskConical, Play, Rocket, Save, Trash2, Workflow, XCircle, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { ApiError, rawJson, type Schemas } from "@/api/client";
import { useDeleteResource, useIntegrations, usePutResource, useResource, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CodeEditor,
  EmptyState,
  Field,
  Input,
  JsonBlock,
  NativeSelect,
  Page,
  PageHeader,
  Skeleton,
  StatusBadge,
  Switch,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
  Tr,
  useConfirm,
} from "@/components/ui";
import { ChoiceCard, Crumb, ErrorList, ExprInput, NAME_RE, VersionsPanel, YamlPanel, useYamlSync } from "./shared";
import { deliveryStatus } from "./Triggers";

interface Spec {
  description?: string;
  workflow?: string;
  campaign?: string;
  connection?: string;
  event: string;
  schedule?: string;
  timezone?: string;
  overlap?: "allow" | "skip";
  filter?: string;
  inputs?: Record<string, unknown>;
  dedupe_key?: string;
  enabled?: boolean;
  triage?: unknown;
}

const PRESETS = [
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Weekdays 9:00", cron: "0 9 * * MON-FRI" },
  { label: "Daily 2:00", cron: "0 2 * * *" },
  { label: "Mondays 2:00", cron: "0 2 * * MON" },
  { label: "1st of month", cron: "0 3 1 * *" },
];

function formatInZone(iso: string, tz: string): string {
  const opts: Intl.DateTimeFormatOptions = { weekday: "short", year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  try {
    return `${new Date(iso).toLocaleString(undefined, { ...opts, timeZone: tz })} ${tz}`;
  } catch {
    return new Date(iso).toLocaleString(undefined, opts);
  }
}

function timezones(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone");
  } catch {
    return ["UTC"];
  }
}

function clean(spec: Spec): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(spec)) {
    if (v === undefined || v === "" || (k === "inputs" && v && !Object.keys(v as object).length)) continue;
    out[k] = v;
  }
  if (spec.event === "schedule") delete out.connection;
  else {
    delete out.schedule;
    delete out.timezone;
  }
  if (out.enabled === true) delete out.enabled;
  if (out.overlap === "allow") delete out.overlap;
  return out;
}

export default function TriggerEditor() {
  const ns = useNs();
  const { name: routeName } = useParams();
  const isNew = !routeName;
  const navigate = useNavigate();
  const confirm = useConfirm();
  const { can } = useAuth();
  const canWrite = can("triggers:write", ns);

  const existing = useResource("triggers", ns, routeName);
  const workflows = useResources("workflows", ns, { enabled: can("workflows:read", ns) });
  const campaigns = useResources("campaigns", ns, { enabled: can("campaigns:read", ns) });
  const connections = useResources("connections", ns, { enabled: can("connections:read", ns) });
  const integrations = useIntegrations();
  const put = usePutResource("triggers", ns);
  const del = useDeleteResource("triggers", ns);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [spec, setSpec] = useState<Spec>({ event: "", enabled: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" });
  const [loaded, setLoaded] = useState(false);
  const [tab, setTab] = useState("settings");

  useEffect(() => {
    if (existing.data && !loaded) {
      setName(existing.data.metadata.name);
      setDescription(existing.data.metadata.description ?? "");
      setSpec({ enabled: true, ...(existing.data.spec as unknown as Spec) });
      setLoaded(true);
    }
  }, [existing.data, loaded]);

  const target: "workflow" | "campaign" = spec.campaign !== undefined ? "campaign" : "workflow";
  const source: "event" | "schedule" = spec.event === "schedule" ? "schedule" : "event";
  const set = (patch: Partial<Spec>) => setSpec((s) => ({ ...s, ...patch }));

  const connection = connections.data?.find((c) => c.metadata.name === spec.connection);
  const integrationKey = connection ? String(connection.spec.integration) : undefined;
  const catalog = integrations.data?.find((i) => i.key === integrationKey);
  const workflow = useResource("workflows", ns, target === "workflow" ? spec.workflow || undefined : undefined);
  const wfInputs = (workflow.data?.spec.inputs ?? {}) as Record<string, { type?: string; description?: string; default?: unknown }>;

  const cleaned = useMemo(() => clean(spec), [spec]);
  const yaml = useYamlSync("Trigger", name, description, cleaned, (p) => {
    setDescription(p.description ?? "");
    setSpec({ enabled: true, ...(p.spec as unknown as Spec) });
  });

  const nameError = isNew && name && !NAME_RE.test(name) ? "Lowercase letters, digits and dashes." : undefined;

  const save = async () => {
    try {
      const res = await put.mutateAsync({ name, body: { metadata: { description: description || null }, spec: cleaned } });
      toast.success(isNew ? `Trigger ${name} created` : `Saved ${name} (v${res.metadata.version})`);
      if (isNew) navigate(nsPath(ns, "triggers", name), { replace: true });
    } catch {
      /* inline */
    }
  };

  const remove = async () => {
    if (!(await confirm({ title: `Delete trigger ${name}?`, description: "Events and schedules will stop starting runs.", confirmLabel: "Delete", danger: true }))) return;
    await del.mutateAsync(name);
    toast.success(`Deleted ${name}`);
    navigate(nsPath(ns, "triggers"));
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
        <EmptyState title="Trigger not found" description={existing.error.message} action={<Link to={nsPath(ns, "triggers")}><Button>Back to triggers</Button></Link>} />
      </Page>
    );

  return (
    <Page wide>
      <PageHeader
        breadcrumb={<Crumb to={nsPath(ns, "triggers")}>Triggers</Crumb>}
        icon={source === "schedule" ? <CalendarClock className="size-4" /> : <Zap className="size-4" />}
        title={isNew ? "New trigger" : name}
        description={isNew ? "Choose what to start and when." : description || undefined}
        actions={
          <>
            {!isNew && (
              <Switch
                checked={spec.enabled !== false}
                disabled={!canWrite}
                onCheckedChange={(v) => set({ enabled: v })}
                label={<span className="text-xs text-muted">{spec.enabled !== false ? "Enabled" : "Disabled"}</span>}
              />
            )}
            {!isNew && canWrite && (
              <Button variant="ghost" icon={<Trash2 className="size-4" />} onClick={() => void remove()}>
                Delete
              </Button>
            )}
            {canWrite && (
              <Button variant="primary" icon={<Save className="size-4" />} loading={put.isPending} disabled={!name || !!nameError || !!yaml.error} onClick={() => void save()}>
                {isNew ? "Create trigger" : "Save"}
              </Button>
            )}
          </>
        }
      />
      <div className="grid gap-5 xl:grid-cols-[1fr_380px]">
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
              {!isNew && <TabsTrigger value="deliveries">Deliveries</TabsTrigger>}
              {!isNew && <TabsTrigger value="history">History</TabsTrigger>}
            </TabsList>
            <TabsContent value="settings">
              <fieldset disabled={!canWrite} className="space-y-5">
                <ErrorList error={put.error} />
                <Card className="p-4">
                  <div className="grid gap-3.5 sm:grid-cols-2">
                    <Field label="Name" required error={nameError}>
                      <Input mono value={name} disabled={!isNew} autoFocus={isNew} onChange={(e) => setName(e.target.value)} placeholder="jira-ready-for-ai" />
                    </Field>
                    <Field label="Description">
                      <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this trigger does" />
                    </Field>
                  </div>
                </Card>

                <Card>
                  <CardHeader title="1 · What it starts" />
                  <div className="space-y-3.5 p-4">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <ChoiceCard selected={target === "workflow"} onClick={() => set({ workflow: spec.workflow ?? "", campaign: undefined })} icon={<Workflow />} title="A workflow run" description="One run per matching event, with inputs mapped from it." />
                      <ChoiceCard selected={target === "campaign"} onClick={() => set({ campaign: spec.campaign ?? "", workflow: undefined, inputs: undefined })} icon={<Rocket />} title="A campaign launch" description="Relaunch a campaign over its whole target set." />
                    </div>
                    {target === "workflow" ? (
                      <Field label="Workflow" required>
                        <NativeSelect value={spec.workflow ?? ""} onChange={(e) => set({ workflow: e.target.value })}>
                          <option value="">Choose a workflow…</option>
                          {(workflows.data ?? []).map((w) => (
                            <option key={w.metadata.name} value={w.metadata.name}>
                              {w.metadata.name}
                            </option>
                          ))}
                        </NativeSelect>
                      </Field>
                    ) : (
                      <Field label="Campaign" required>
                        <NativeSelect value={spec.campaign ?? ""} onChange={(e) => set({ campaign: e.target.value })}>
                          <option value="">Choose a campaign…</option>
                          {(campaigns.data ?? []).map((c) => (
                            <option key={c.metadata.name} value={c.metadata.name}>
                              {c.metadata.name}
                            </option>
                          ))}
                        </NativeSelect>
                      </Field>
                    )}
                  </div>
                </Card>

                <Card>
                  <CardHeader title="2 · When it fires" />
                  <div className="space-y-3.5 p-4">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <ChoiceCard selected={source === "event"} onClick={() => set({ event: source === "event" ? spec.event : "" })} icon={<Zap />} title="On an event" description="A webhook or polled event from a connection." />
                      <ChoiceCard selected={source === "schedule"} onClick={() => set({ event: "schedule", schedule: spec.schedule ?? "0 9 * * MON-FRI" })} icon={<CalendarClock />} title="On a schedule" description="A cron expression in a time zone." />
                    </div>
                    {source === "event" ? (
                      <div className="grid gap-3.5 sm:grid-cols-2">
                        <Field label="Connection" required>
                          <NativeSelect value={spec.connection ?? ""} onChange={(e) => set({ connection: e.target.value })}>
                            <option value="">Choose a connection…</option>
                            {(connections.data ?? []).map((c) => (
                              <option key={c.metadata.name} value={c.metadata.name}>
                                {c.metadata.name} ({String(c.spec.integration)})
                              </option>
                            ))}
                          </NativeSelect>
                        </Field>
                        <Field label="Event" required description={catalog?.events.find((e) => e.name === spec.event)?.description}>
                          <Input list="trigger-events" mono value={spec.event} onChange={(e) => set({ event: e.target.value })} placeholder={catalog?.events[0]?.name ?? "issue_updated"} />
                          <datalist id="trigger-events">
                            {(catalog?.events ?? []).map((e) => (
                              <option key={e.name} value={e.name}>
                                {e.description}
                              </option>
                            ))}
                          </datalist>
                        </Field>
                      </div>
                    ) : (
                      <ScheduleFields spec={spec} set={set} />
                    )}
                  </div>
                </Card>

                <Card>
                  <CardHeader title="3 · Conditions" description={<>Expressions over <code className="font-mono">event</code>. Leave the filter empty to fire on every event.</>} />
                  <div className="space-y-3.5 p-4">
                    {source === "event" && (
                      <Field label="Filter" description={<>e.g. <code className="font-mono">{"{{ event.status_change.to == 'Ready for AI' }}"}</code></>}>
                        <ExprInput value={spec.filter ?? ""} onChange={(v) => set({ filter: v || undefined })} placeholder="{{ event... }}" />
                      </Field>
                    )}
                    <div className="grid gap-3.5 sm:grid-cols-2">
                      {source === "event" && (
                        <Field label="Dedupe key" description="Events with a key already seen are ignored.">
                          <ExprInput value={spec.dedupe_key ?? ""} onChange={(v) => set({ dedupe_key: v || undefined })} placeholder="{{ event.issue_key }}" />
                        </Field>
                      )}
                      <Field label="Overlap" description="What to do while a run it started is still active.">
                        <NativeSelect value={spec.overlap ?? "allow"} onChange={(e) => set({ overlap: e.target.value as Spec["overlap"] })}>
                          <option value="allow">Allow overlapping runs</option>
                          <option value="skip">Skip while a run is active</option>
                        </NativeSelect>
                      </Field>
                    </div>
                    {spec.triage != null && <Badge tone="info">Semantic triage configured (edit in YAML)</Badge>}
                  </div>
                </Card>

                {target === "workflow" && (
                  <Card>
                    <CardHeader title="4 · Workflow inputs" description="Map event fields onto the workflow's inputs." />
                    <div className="space-y-3 p-4">
                      <InputMapping inputs={wfInputs} value={spec.inputs ?? {}} onChange={(v) => set({ inputs: v })} hasWorkflow={!!spec.workflow} />
                    </div>
                  </Card>
                )}
              </fieldset>
            </TabsContent>
            <TabsContent value="yaml">
              <YamlPanel value={yaml.text} onChange={canWrite ? yaml.change : undefined} readOnly={!canWrite} error={yaml.error} />
              <ErrorList error={put.error} className="mt-3" />
            </TabsContent>
            {!isNew && (
              <TabsContent value="deliveries">
                <Deliveries ns={ns} name={name} />
              </TabsContent>
            )}
            {!isNew && (
              <TabsContent value="history">
                <VersionsPanel plural="triggers" ns={ns} name={name} current={existing.data} />
              </TabsContent>
            )}
          </Tabs>
        </div>
        <aside className="space-y-4">
          {!isNew ? (
            <TestPanel key={`${spec.event}-${catalog ? 1 : 0}`} ns={ns} name={name} spec={existing.data?.spec as unknown as Spec} example={catalog?.events.find((e) => e.name === spec.event)?.example} />
          ) : (
            <Card className="p-4 text-[13px] text-muted">
              <FlaskConical className="mb-2 size-4 text-accent" />
              Save the trigger to dry-run it against a sample event and fire it manually.
            </Card>
          )}
        </aside>
      </div>
    </Page>
  );
}

function ScheduleFields({ spec, set }: { spec: Spec; set: (p: Partial<Spec>) => void }) {
  const tz = spec.timezone ?? "UTC";
  const [cron, setCron] = useState(spec.schedule ?? "");
  useEffect(() => setCron(spec.schedule ?? ""), [spec.schedule]);
  const [debounced, setDebounced] = useState(cron);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(cron), 300);
    return () => clearTimeout(t);
  }, [cron]);
  const preview = useQuery({
    queryKey: ["schedule-preview", debounced, tz],
    queryFn: () => rawJson<{ next: string[] }>(`/v1/schedule-preview?${new URLSearchParams({ cron: debounced, timezone: tz, count: "5" })}`),
    enabled: !!debounced.trim(),
    retry: false,
  });
  const zones = useMemo(timezones, []);
  return (
    <div className="space-y-3.5">
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((p) => (
          <button
            type="button"
            key={p.cron}
            onClick={() => set({ schedule: p.cron })}
            className={cn("h-6 rounded-md border px-2 text-xs transition-colors", spec.schedule === p.cron ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:text-fg")}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="grid gap-3.5 sm:grid-cols-2">
        <Field label="Cron expression" required description="minute hour day-of-month month day-of-week" error={preview.error instanceof ApiError ? preview.error.detail : undefined}>
          <Input mono value={cron} onChange={(e) => {
            setCron(e.target.value);
            set({ schedule: e.target.value });
          }} placeholder="0 9 * * MON-FRI" />
        </Field>
        <Field label="Time zone">
          <Input list="tz-list" value={tz} onChange={(e) => set({ timezone: e.target.value })} />
          <datalist id="tz-list">
            {zones.map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
        </Field>
      </div>
      {preview.data && (
        <div className="rounded-lg border border-border bg-subtle/50 p-3">
          <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Next runs</div>
          <ul className="space-y-1 text-[13px]">
            {preview.data.next.map((n) => (
              <li key={n} className="flex items-center justify-between gap-3">
                <span className="tabular-nums">{formatInZone(n, tz)}</span>
                <span className="text-xs text-faint">{relTime(n)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function InputMapping({ inputs, value, onChange, hasWorkflow }: {
  inputs: Record<string, { type?: string; description?: string; default?: unknown }>;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  hasWorkflow: boolean;
}) {
  const names = Object.keys(inputs);
  const extra = Object.keys(value).filter((k) => !names.includes(k));
  if (!hasWorkflow) return <p className="text-[13px] text-muted">Choose a workflow first.</p>;
  if (!names.length && !extra.length) return <p className="text-[13px] text-muted">This workflow takes no inputs.</p>;
  const setKey = (k: string, v: string) => {
    const next = { ...value };
    if (v) next[k] = v;
    else delete next[k];
    onChange(next);
  };
  return (
    <>
      {[...names, ...extra].map((k) => {
        const p = inputs[k];
        const required = p && p.default == null;
        return (
          <div key={k} className="grid gap-2 sm:grid-cols-[180px_1fr] sm:items-start">
            <div className="pt-1.5">
              <div className="font-mono text-[12.5px] font-medium">
                {k}
                {required && <span className="ml-0.5 text-bad">*</span>}
              </div>
              <div className="text-[11px] text-faint">{p ? `${p.type ?? "string"}${p.default != null ? ` · default ${JSON.stringify(p.default)}` : ""}` : "not a workflow input"}</div>
            </div>
            <ExprInput value={typeof value[k] === "string" ? (value[k] as string) : value[k] == null ? "" : JSON.stringify(value[k])} onChange={(v) => setKey(k, v)} placeholder={p?.description ?? "{{ event.… }}"} />
          </div>
        );
      })}
    </>
  );
}

function TestPanel({ ns, name, spec, example }: { ns: string; name: string; spec?: Spec; example?: unknown }) {
  const { can } = useAuth();
  const navigate = useNavigate();
  const initial = example && Object.keys(example as object).length ? JSON.stringify(example, null, 2) : "{\n  \n}";
  const [data, setData] = useState(initial);
  const [eventType, setEventType] = useState("");
  const parsed = useMemo(() => {
    try {
      return { ok: true as const, value: JSON.parse(data || "{}") as Record<string, unknown> };
    } catch (e) {
      return { ok: false as const, error: (e as Error).message };
    }
  }, [data]);
  const test = useMutation({
    mutationFn: () =>
      rawJson<{ matched: boolean; reason?: string; inputs?: Record<string, unknown>; dedupe_key?: string | null; triage?: unknown }>(`/v1/namespaces/${ns}/triggers/${name}/test`, {
        method: "POST",
        body: JSON.stringify({ event_type: eventType || undefined, data: parsed.ok ? parsed.value : {} }),
      }),
  });
  const fire = useMutation({
    mutationFn: () =>
      rawJson<{ status: string; run_id?: string; campaign_id?: string; reason?: string }>(`/v1/namespaces/${ns}/triggers/${name}/fire`, {
        method: "POST",
        body: JSON.stringify({ data: parsed.ok ? parsed.value : {} }),
      }),
    onSuccess: (r) => {
      if (r.run_id) toast.success("Run started", { action: { label: "Open", onClick: () => navigate(nsPath(ns, "runs", r.run_id!)) } });
      else if (r.campaign_id) toast.success("Campaign launched", { action: { label: "Open", onClick: () => navigate(nsPath(ns, "campaign-runs", r.campaign_id!)) } });
      else toast.message(`Trigger ${r.status}${r.reason ? `: ${r.reason}` : ""}`);
    },
  });
  const r = test.data;
  return (
    <Card className="sticky top-4">
      <CardHeader title="Try it" description="Dry-run the saved trigger against a sample event. Nothing is started." />
      <div className="space-y-3 p-4">
        {spec?.event !== "schedule" && (
          <Field label="Event type" hint="optional">
            <Input mono value={eventType} onChange={(e) => setEventType(e.target.value)} placeholder={spec?.event.replace("*", "test") ?? ""} />
          </Field>
        )}
        <Field label="Event payload" hint="JSON, available as event">
          <div className={cn("rounded-lg border bg-panel", parsed.ok ? "border-border" : "border-bad")}>
            <CodeEditor value={data} onChange={setData} language="json" lineNumbers={false} minHeight="140px" maxHeight="320px" />
          </div>
          {!parsed.ok && <p className="mt-1 text-xs text-bad">{parsed.error}</p>}
        </Field>
        <div className="flex gap-2">
          <Button className="flex-1 justify-center" icon={<FlaskConical className="size-4" />} loading={test.isPending} disabled={!parsed.ok} onClick={() => test.mutate()}>
            Test
          </Button>
          {can("triggers:fire", ns) && (
            <Button className="flex-1 justify-center" variant="primary" icon={<Play className="size-4" />} loading={fire.isPending} disabled={!parsed.ok} onClick={() => fire.mutate()}>
              Fire now
            </Button>
          )}
        </div>
        <ErrorList error={test.error ?? fire.error} />
        {r && (
          <div className={cn("animate-fade-in rounded-lg border p-3", r.matched ? "border-ok/25 bg-ok-soft" : "border-border bg-subtle")}>
            <div className={cn("flex items-center gap-1.5 text-[13px] font-medium", r.matched ? "text-ok" : "text-muted")}>
              {r.matched ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
              {r.matched ? "Would fire" : "Would not fire"}
            </div>
            {r.reason && <p className="mt-1 text-xs text-muted">{r.reason}</p>}
            {r.dedupe_key && (
              <p className="mt-1 text-xs text-muted">
                Dedupe key <code className="font-mono text-fg">{r.dedupe_key}</code>
              </p>
            )}
            {r.inputs && (
              <>
                <div className="mt-2 mb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Inputs</div>
                <JsonBlock value={r.inputs} className="bg-panel" maxHeight="200px" />
              </>
            )}
            {r.triage != null && (
              <>
                <div className="mt-2 mb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Triage</div>
                <JsonBlock value={r.triage} className="bg-panel" maxHeight="200px" />
              </>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function Deliveries({ ns, name }: { ns: string; name: string }) {
  const navigate = useNavigate();
  const { data, isLoading } = useQuery({
    queryKey: ["deliveries", ns, name, 100],
    queryFn: () => rawJson<Schemas["DeliveryOut"][]>(`/v1/namespaces/${ns}/triggers/${name}/deliveries?limit=100`),
    refetchInterval: 10_000,
  });
  if (isLoading) return <Skeleton className="h-40" />;
  if (!data?.length) return <Card><EmptyState icon={<Zap className="size-5" />} title="No deliveries yet" description="Every event or schedule tick this trigger evaluates shows up here, with the run it started." /></Card>;
  return (
    <Card>
      <Table>
        <thead>
          <tr>
            <Th>Received</Th>
            <Th>Event</Th>
            <Th>Outcome</Th>
            <Th>Dedupe key</Th>
            <Th>Run</Th>
          </tr>
        </thead>
        <tbody>
          {data.map((d) => (
            <Tr key={d.id} onClick={d.run_id ? () => navigate(nsPath(ns, "runs", d.run_id!)) : undefined}>
              <Td className="text-xs whitespace-nowrap" title={dateTime(d.created_at)}>
                {relTime(d.created_at)}
              </Td>
              <Td className="font-mono text-xs">{d.event_type}</Td>
              <Td>
                <div className="flex items-center gap-2">
                  <StatusBadge status={deliveryStatus(d.status)} />
                  <span className="text-xs text-muted">{d.status}</span>
                </div>
                {typeof d.detail.reason === "string" && <div className="mt-0.5 max-w-xs truncate text-[11px] text-faint">{d.detail.reason}</div>}
              </Td>
              <Td className="max-w-40 truncate font-mono text-[11px] text-muted">{d.dedupe_key ?? "—"}</Td>
              <Td>
                {d.run_id ? (
                  <span className="font-mono text-xs text-accent">{d.run_id.slice(-10)}</span>
                ) : typeof d.detail.campaign_id === "string" ? (
                  <Link onClick={(e) => e.stopPropagation()} className="font-mono text-xs text-accent" to={nsPath(ns, "campaign-runs", d.detail.campaign_id)}>
                    campaign
                  </Link>
                ) : (
                  <span className="text-xs text-faint">—</span>
                )}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}
