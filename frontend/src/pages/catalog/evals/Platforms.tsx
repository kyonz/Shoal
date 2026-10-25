/**
 * Eval platforms (Braintrust, Langfuse, … via `shoal.eval_sinks`): export eval runs as experiments,
 * and take cases from a platform dataset. Settings follow each sink's config schema, with secrets
 * picked by name (`*_secret`), so keys never appear in the Eval.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, CircleHelp, CloudUpload, DatabaseZap, ExternalLink, PlugZap, Plus, SlidersHorizontal, Trash2, Upload, XCircle } from "lucide-react";
import { Link } from "react-router";
import { useId, useState } from "react";
import { toast } from "sonner";
import { ApiError, rawJson, type Resource } from "@/api/client";
import { keys, useResources, type JsonSchema } from "@/api/hooks";
import { humanize } from "@/components/forms/SchemaForm";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  Field,
  Input,
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
  NativeSelect,
  Segmented,
  Skeleton,
  Switch,
} from "@/components/ui";
import { SecretPicker } from "@/pages/connect/shared";
import type { EditorContext, EditorExtension } from "../ResourceEditorPage";
import { SHARED_NS } from "../extensions";

export interface Sink {
  name: string;
  description: string;
  config_schema: JsonSchema;
}

/** Where results go or cases come from: a connected platform (`platform`) or a sink with its own settings (`sink`). */
export interface SinkRef {
  sink?: string;
  platform?: string;
  config?: Record<string, unknown>;
}

/** An EvalPlatform: one sink's settings, connected once by an admin, with secrets from the shared namespace. */
export interface PlatformSpec {
  description?: string;
  sink: string;
  config?: Record<string, unknown>;
}

export interface DatasetRef extends SinkRef {
  name: string;
  limit?: number | null;
}

const SINK_LABEL: Record<string, string> = { braintrust: "Braintrust", langfuse: "Langfuse" };
export const sinkLabel = (name: string) => SINK_LABEL[name] ?? humanize(name);

export function useEvalSinks() {
  return useQuery({ queryKey: ["catalog", "eval-sinks"], queryFn: () => rawJson<Sink[]>("/v1/eval-sinks"), staleTime: 300_000 });
}

/** Platforms connected in the admin console. Empty for people who can't see them. */
export function useEvalPlatforms() {
  const canRead = useCan("eval-platforms:read");
  return useResources("eval-platforms", "_", { enabled: canRead, retry: false });
}

/** Mirrors the engine: settings that choose where requests (and credentials) go stay on the platform. */
export const isEndpointField = (k: string) => k === "host" || k === "url" || k.endsWith("_url") || k.endsWith("_host");

const platformSpec = (r: Resource | undefined) => r?.spec as unknown as PlatformSpec | undefined;

/** The sink a reference ends up using: its own, or its platform's. */
export function refSink(ref: SinkRef, platforms: Resource[]): string | undefined {
  return ref.sink ?? platformSpec(platforms.find((p) => p.metadata.name === ref.platform))?.sink;
}

/** "triage (Braintrust)" for a platform, "Braintrust" for custom settings. */
export function refLabel(ref: SinkRef, platforms: Resource[]): string {
  if (!ref.platform) return sinkLabel(ref.sink ?? "");
  const sink = refSink(ref, platforms);
  return sink ? `${ref.platform} (${sinkLabel(sink)})` : ref.platform;
}

/** A new destination: the first connected platform, else custom settings for the first sink. */
function defaultRef(platforms: Resource[], sinks: Sink[]): SinkRef {
  if (platforms.length) return { platform: platforms[0].metadata.name, config: {} };
  return { sink: sinks[0]?.name ?? "braintrust", config: starterConfig(sinks[0]) };
}

/** Fill in a sink's required fields with sensible secret names, so a new destination is one pick away. */
function starterConfig(sink: Sink | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of sink?.config_schema.required ?? []) if (k.endsWith("_secret")) out[k] = `${sink!.name}-${k.replace(/_secret$/, "").replace(/_/g, "-")}`;
  return out;
}

/** "API key", "App URL": settings named after the schema key. */
export function fieldLabel(key: string) {
  return humanize(key).replace(/\b(api|url|id)\b/gi, (w) => w.toUpperCase());
}

export function missingFields(sink: Sink | undefined, config: Record<string, unknown> | undefined): string[] {
  return (sink?.config_schema.required ?? []).filter((k) => !config?.[k]);
}

/** Required settings a reference still lacks (a platform carries its own). */
function refMissing(ref: SinkRef, sinks: Sink[]): string[] {
  return ref.platform ? [] : missingFields(sinks.find((s) => s.name === ref.sink), ref.config);
}

const missingText = (missing: string[]) => missing.map((m) => fieldLabel(m.replace(/_secret$/, ""))).join(", ");

// --------------------------------------------------------------------------- settings form

export function SinkConfigFields({ ns, sink, value, onChange, disabled }: {
  ns: string;
  sink: Sink | undefined;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  if (!sink) return null;
  const props = Object.entries(sink.config_schema.properties ?? {});
  const required = new Set(sink.config_schema.required ?? []);
  const set = (k: string, v: unknown) => {
    const next = { ...value };
    if (v === undefined || v === "") delete next[k];
    else next[k] = v;
    onChange(next);
  };
  // Required first, then the rest (URLs and overrides) in schema order.
  props.sort(([a], [b]) => Number(required.has(b)) - Number(required.has(a)));
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {props.map(([k, s]) =>
        k.endsWith("_secret") ? (
          <SecretPicker
            key={k}
            ns={ns}
            field={fieldLabel(k.replace(/_secret$/, ""))}
            description={`Secret holding the ${fieldLabel(k.replace(/_secret$/, ""))}${required.has(k) ? " (required)" : ""}.`}
            value={value[k] as string | undefined}
            disabled={disabled}
            onChange={(v) => set(k, v)}
          />
        ) : (
          <Field key={k} label={fieldLabel(k)} required={required.has(k)} description={s.description}>
            <Input
              mono
              disabled={disabled}
              value={(value[k] as string) ?? ""}
              placeholder={s.default != null ? String(s.default) : undefined}
              onChange={(e) => set(k, e.target.value)}
            />
          </Field>
        ),
      )}
    </div>
  );
}

function SinkSelect({ sinks, value, onChange, disabled }: { sinks: Sink[]; value: string; onChange: (v: string) => void; disabled?: boolean }) {
  return (
    <NativeSelect value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {!sinks.some((s) => s.name === value) && <option value={value}>{value}</option>}
      {sinks.map((s) => (
        <option key={s.name} value={s.name}>
          {sinkLabel(s.name)}
          {s.description ? ` — ${s.description}` : ""}
        </option>
      ))}
    </NativeSelect>
  );
}

/** Pick a connected platform, or a sink to configure here. */
function TargetSelect({ value, onChange, sinks, platforms, disabled }: {
  value: SinkRef;
  onChange: (v: SinkRef) => void;
  sinks: Sink[];
  platforms: Resource[];
  disabled?: boolean;
}) {
  const current = value.platform ? `p:${value.platform}` : `s:${value.sink ?? ""}`;
  const known = value.platform ? platforms.some((p) => p.metadata.name === value.platform) : sinks.some((x) => x.name === value.sink);
  return (
    <NativeSelect
      value={current}
      disabled={disabled}
      onChange={(e) => {
        const [kind, name] = [e.target.value.slice(0, 1), e.target.value.slice(2)];
        onChange(kind === "p" ? { platform: name, config: {} } : { sink: name, config: starterConfig(sinks.find((x) => x.name === name)) });
      }}
    >
      {!known && <option value={current}>{value.platform ?? value.sink} (not found)</option>}
      {platforms.length > 0 && (
        <optgroup label="Connected platforms">
          {platforms.map((p) => (
            <option key={p.metadata.name} value={`p:${p.metadata.name}`}>
              {p.metadata.name} · {sinkLabel(platformSpec(p)?.sink ?? "")}
            </option>
          ))}
        </optgroup>
      )}
      <optgroup label={platforms.length ? "Set up here instead" : "Platform"}>
        {sinks.map((x) => (
          <option key={x.name} value={`s:${x.name}`}>
            {sinkLabel(x.name)}
            {platforms.length ? " (own settings)" : x.description ? ` — ${x.description}` : ""}
          </option>
        ))}
      </optgroup>
    </NativeSelect>
  );
}

/**
 * Settings for a reference: the full form for custom settings, or for a connected platform only the
 * plain settings this eval may override (endpoints and keys stay on the platform).
 */
function TargetSettings({ ns, value, onChange, sinks, platforms }: {
  ns: string;
  value: SinkRef;
  onChange: (config: Record<string, unknown>) => void;
  sinks: Sink[];
  platforms: Resource[];
}) {
  if (!value.platform) return <SinkConfigFields ns={ns} sink={sinks.find((x) => x.name === value.sink)} value={value.config ?? {}} onChange={onChange} />;
  const platform = platformSpec(platforms.find((p) => p.metadata.name === value.platform));
  const sink = sinks.find((x) => x.name === platform?.sink);
  if (!platform || !sink) return null;
  const overridable = Object.entries(sink.config_schema.properties ?? {}).filter(([k]) => !k.endsWith("_secret") && !isEndpointField(k));
  const config = value.config ?? {};
  const set = (k: string, v: string) => {
    const next = { ...config };
    if (v) next[k] = v;
    else delete next[k];
    onChange(next);
  };
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">
        Keys and endpoints come from the <span className="font-medium text-fg">{value.platform}</span> platform your admin connected. Leave a field empty to use the platform's value.
      </p>
      {overridable.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {overridable.map(([k, sch]) => (
            <Field key={k} label={fieldLabel(k)} hint="optional" description={sch.description}>
              <Input
                mono
                value={(config[k] as string) ?? ""}
                placeholder={String(platform.config?.[k] ?? sch.default ?? "")}
                onChange={(e) => set(k, e.target.value)}
              />
            </Field>
          ))}
        </div>
      )}
    </div>
  );
}

/** Dataset names on a connected platform, for the dataset field's suggestions. */
function usePlatformDatasets(platform: string | undefined) {
  return useQuery({
    queryKey: ["eval-platforms", platform, "datasets"],
    queryFn: () => rawJson<string[]>(`/v1/eval-platforms/${encodeURIComponent(platform!)}/datasets`),
    enabled: !!platform,
    retry: false,
    staleTime: 60_000,
  });
}

function DatasetInput({ platform, value, onChange, autoFocus }: { platform?: string; value: string; onChange: (v: string) => void; autoFocus?: boolean }) {
  const datasets = usePlatformDatasets(platform);
  const id = useId();
  return (
    <>
      <Input mono autoFocus={autoFocus} list={id} value={value} placeholder={datasets.data?.[0] ?? "support-tickets-golden"} onChange={(e) => onChange(e.target.value)} />
      <datalist id={id}>
        {(datasets.data ?? []).map((d) => (
          <option key={d} value={d} />
        ))}
      </datalist>
    </>
  );
}

// --------------------------------------------------------------------------- eval editor card

/** "Eval platform" card on the eval editor: export destinations and where the cases come from. */
export function PlatformCard({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite, ns, isNew, name, dirty, replaceFrom } = ctx;
  const sinks = useEvalSinks();
  const list = sinks.data ?? [];
  const platforms = useEvalPlatforms().data ?? [];
  const canManage = useCan("eval-platforms:manage");
  const exports = (spec.sinks as SinkRef[] | undefined) ?? [];
  const dataset = spec.dataset as DatasetRef | undefined;
  const [importing, setImporting] = useState(false);

  const setExports = (next: SinkRef[]) => {
    const s = { ...spec };
    if (next.length) s.sinks = next;
    else delete s.sinks;
    setSpec(s);
  };
  const setDataset = (next: DatasetRef | undefined) => {
    const s = { ...spec };
    if (next) s.dataset = next;
    else delete s.dataset;
    setSpec(s);
  };
  const addExport = () => {
    const unused = platforms.find((p) => !exports.some((e) => e.platform === p.metadata.name));
    if (unused) return setExports([...exports, { platform: unused.metadata.name, config: {} }]);
    const sink = list.find((x) => !exports.some((e) => e.sink === x.name)) ?? list[0];
    if (sink) setExports([...exports, { sink: sink.name, config: starterConfig(sink) }]);
  };

  if (sinks.isSuccess && !list.length) return null;

  return (
    <Card>
      <CardHeader
        title="Eval platform"
        description={`Send results to ${list.map((x) => sinkLabel(x.name)).join(" or ") || "an eval platform"} as experiments, and take cases from a dataset there.`}
      />
      {platforms.length === 0 && sinks.isSuccess && (
        <div className="mx-4 mb-1 flex items-start gap-2 rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-xs text-info">
          <PlugZap className="mt-0.5 size-3.5 shrink-0" />
          <span>
            {canManage ? (
              <>
                Connect Braintrust or Langfuse once under{" "}
                <Link to="/admin/eval-platforms" className="font-medium underline-offset-2 hover:underline">
                  Admin console › Eval platforms
                </Link>{" "}
                and every eval can pick it, without repeating keys.
              </>
            ) : (
              "No eval platforms are connected for your organisation yet. You can set one up for this eval below, or ask an admin to connect one for everyone."
            )}
          </span>
        </div>
      )}
      <fieldset disabled={!canWrite} className="divide-y divide-border">
        <section className="space-y-3 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-1.5 text-[13px] font-medium">
                <CloudUpload className="size-4 text-accent" /> Export results
              </div>
              <p className="text-xs text-muted">Every finished run is pushed as an experiment: one trace per case, with each grader's score.</p>
            </div>
            <Switch checked={exports.length > 0} onCheckedChange={(on) => (on ? addExport() : setExports([]))} disabled={!canWrite || !list.length} />
          </div>
          {exports.map((e, i) => {
            const missing = refMissing(e, list);
            const update = (next: SinkRef) => setExports(exports.map((x, j) => (j === i ? next : x)));
            return (
              <div key={i} className="rounded-lg border border-border p-3">
                <div className="mb-3 flex items-end gap-2">
                  <Field label="Destination" className="w-72">
                    <TargetSelect value={e} onChange={update} sinks={list} platforms={platforms} />
                  </Field>
                  {missing.length > 0 && <Badge tone="warn">Needs {missingText(missing)}</Badge>}
                  <Button variant="ghost" size="icon-sm" className="mb-1 ml-auto" aria-label="Remove destination" onClick={() => setExports(exports.filter((_, j) => j !== i))}>
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
                <TargetSettings ns={ns} value={e} onChange={(config) => update({ ...e, config })} sinks={list} platforms={platforms} />
              </div>
            );
          })}
          {exports.length > 0 && (
            <Button size="sm" variant="ghost" icon={<Plus className="size-3.5" />} onClick={addExport}>
              Add another destination
            </Button>
          )}
        </section>

        <section className="space-y-3 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-1.5 text-[13px] font-medium">
                <DatabaseZap className="size-4 text-accent" /> Cases from a dataset
              </div>
              <p className="text-xs text-muted">
                {dataset
                  ? "Each run pulls the dataset's current items as cases, alongside any cases written here."
                  : "Keep the platform's dataset as the source of truth, or import its items once as cases you edit here."}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {!isNew && !dataset && (
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Upload className="size-3.5" />}
                  onClick={() => setImporting(true)}
                  disabled={!list.length || dirty}
                  title={dirty ? "Save your changes first: the import adds to the saved version" : undefined}
                >
                  Import once…
                </Button>
              )}
              <Switch
                checked={!!dataset}
                onCheckedChange={(on) => setDataset(on ? { ...defaultRef(platforms, list), name: "" } : undefined)}
                disabled={!canWrite || !list.length}
              />
            </div>
          </div>
          {dataset && (
            <div className="rounded-lg border border-border p-3">
              <div className="mb-3 grid gap-3 sm:grid-cols-[18rem_minmax(0,1fr)_8rem]">
                <Field label="Source">
                  <TargetSelect value={dataset} onChange={(t) => setDataset({ ...t, name: dataset.name, limit: dataset.limit })} sinks={list} platforms={platforms} />
                </Field>
                <Field label="Dataset" required>
                  <DatasetInput platform={dataset.platform} value={dataset.name} onChange={(v) => setDataset({ ...dataset, name: v })} />
                </Field>
                <Field label="Limit">
                  <Input
                    type="number"
                    min={1}
                    value={dataset.limit ?? ""}
                    placeholder="All"
                    onChange={(e) => setDataset({ ...dataset, limit: e.target.value ? Number(e.target.value) : undefined })}
                  />
                </Field>
              </div>
              <TargetSettings ns={ns} value={dataset} onChange={(config) => setDataset({ ...dataset, config })} sinks={list} platforms={platforms} />
            </div>
          )}
        </section>
      </fieldset>
      {importing && name && list.length > 0 && (
        <ImportDialog open onOpenChange={setImporting} ns={ns} evalName={name} sinks={list} platforms={platforms} initial={exports[0] ?? defaultRef(platforms, list)} onImported={replaceFrom} />
      )}
    </Card>
  );
}

/** The request body for a reference: `{platform}` or `{sink}`, plus any settings. */
function targetBody(ref: SinkRef) {
  const config = ref.config && Object.keys(ref.config).length ? ref.config : undefined;
  return { ...(ref.platform ? { platform: ref.platform } : { sink: ref.sink }), ...(config ? { config } : {}) };
}

// --------------------------------------------------------------------------- import once

function ImportDialog({ open, onOpenChange, ns, evalName, sinks, platforms, initial, onImported }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  ns: string;
  evalName: string;
  sinks: Sink[];
  platforms: Resource[];
  initial: SinkRef;
  onImported: (r: Resource) => void;
}) {
  const qc = useQueryClient();
  const [target, setTarget] = useState<SinkRef>(initial);
  const [dataset, setDataset] = useState("");
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [limit, setLimit] = useState("");
  const run = useMutation({
    mutationFn: () =>
      rawJson<Resource>(`/v1/namespaces/${ns}/evals/${encodeURIComponent(evalName)}/import`, {
        method: "POST",
        body: JSON.stringify({ ...targetBody(target), dataset, mode, limit: limit ? Number(limit) : null }),
      }),
    onSuccess: (r) => {
      const n = ((r.spec?.cases as unknown[]) ?? []).length;
      toast.success(`Imported ${dataset}: the eval now has ${n} case${n === 1 ? "" : "s"} (v${r.metadata.version})`);
      qc.setQueryData(keys.resource("evals", ns, evalName), r);
      onImported(r);
      void qc.invalidateQueries({ queryKey: ["resources", "evals"] });
      onOpenChange(false);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="Import cases from a dataset"
      description="Copies the dataset's items into this eval as cases and saves a new version. Item input becomes the case inputs, expected output becomes expect."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" icon={<Upload className="size-3.5" />} loading={run.isPending} disabled={!dataset || refMissing(target, sinks).length > 0} onClick={() => run.mutate()}>
            Import
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-[16rem_minmax(0,1fr)]">
          <Field label="Source">
            <TargetSelect value={target} onChange={setTarget} sinks={sinks} platforms={platforms} />
          </Field>
          <Field label="Dataset" required>
            <DatasetInput autoFocus platform={target.platform} value={dataset} onChange={setDataset} />
          </Field>
        </div>
        <TargetSettings ns={ns} value={target} onChange={(config) => setTarget({ ...target, config })} sinks={sinks} platforms={platforms} />
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Existing cases">
            <Segmented<"append" | "replace">
              value={mode}
              onChange={setMode}
              options={[
                { value: "append", label: "Keep, add new" },
                { value: "replace", label: "Replace all" },
              ]}
            />
          </Field>
          <Field label="Limit" className="w-28">
            <Input type="number" min={1} value={limit} placeholder="All" onChange={(e) => setLimit(e.target.value)} />
          </Field>
        </div>
        {run.error && <p className="text-xs text-bad">{run.error instanceof ApiError ? run.error.detail : String(run.error)}</p>}
      </div>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- export a finished run

/** Where a configured destination sends results, e.g. "project triage" or a host. */
function destinationHint(config: Record<string, unknown> | undefined): string | undefined {
  const c = config ?? {};
  const named = c.project ?? c.dataset ?? c.workspace;
  if (typeof named === "string" && named) return named;
  const url = c.host ?? c.api_url ?? c.base_url;
  return typeof url === "string" && url ? url.replace(/^https?:\/\//, "") : undefined;
}

type ExportResult = { sink: string; platform?: string | null; url: string | null };

function exportRun(runId: string, body: object) {
  return rawJson<ExportResult>(`/v1/eval-runs/${runId}/export`, { method: "POST", body: JSON.stringify(body) });
}

const exportedTo = (r: ExportResult) => (r.platform ? `${r.platform} (${sinkLabel(r.sink)})` : sinkLabel(r.sink));

/**
 * Export a finished run. Destinations already set up on the eval are one click (anyone who can run the
 * eval): only the sink or platform name is sent and the server uses the eval's own settings. Other
 * settings can name secrets, so only people who can edit the eval get them.
 */
export function ExportRunButton({ ns, runId, evalSpec }: { ns: string; runId: string; evalSpec?: Record<string, unknown> }) {
  const sinks = useEvalSinks();
  const platforms = useEvalPlatforms().data ?? [];
  const canWrite = useCan("evals:write", ns);
  const [open, setOpen] = useState(false);
  const list = sinks.data ?? [];
  const configured = ((evalSpec?.sinks as SinkRef[] | undefined) ?? []).filter((c) => c.platform || list.some((x) => x.name === c.sink));
  const quick = useMutation({
    mutationFn: (ref: SinkRef) => exportRun(runId, ref.platform ? { platform: ref.platform } : { sink: ref.sink }),
    onSuccess: (r) =>
      toast.success(`Exported to ${exportedTo(r)}`, r.url ? { action: { label: "Open experiment", onClick: () => window.open(r.url!, "_blank", "noopener") } } : undefined),
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
  if (!list.length || (!configured.length && !canWrite)) return null;

  const dialog = open && <ExportDialog ns={ns} runId={runId} sinks={list} platforms={platforms} configured={configured} onClose={() => setOpen(false)} />;
  if (!configured.length)
    return (
      <>
        <Button icon={<CloudUpload className="size-4" />} onClick={() => setOpen(true)}>
          Export
        </Button>
        {dialog}
      </>
    );
  if (configured.length === 1 && !canWrite)
    return (
      <Button icon={<CloudUpload className="size-4" />} loading={quick.isPending} onClick={() => quick.mutate(configured[0])}>
        Export to {configured[0].platform ?? sinkLabel(configured[0].sink ?? "")}
      </Button>
    );
  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <Button icon={<CloudUpload className="size-4" />} loading={quick.isPending}>
            Export <ChevronDown className="size-3.5 opacity-60" />
          </Button>
        </MenuTrigger>
        <MenuContent className="min-w-64">
          <MenuLabel>This eval's destinations</MenuLabel>
          {configured.map((c, i) => {
            const sink = refSink(c, platforms);
            const hint = [c.platform && sink ? sinkLabel(sink) : null, destinationHint(c.config)].filter(Boolean).join(" · ");
            return (
              <MenuItem key={`${c.platform ?? c.sink}-${i}`} icon={<CloudUpload />} onSelect={() => quick.mutate(c)}>
                <span className="flex min-w-0 flex-col">
                  <span>Export to {c.platform ?? sinkLabel(c.sink ?? "")}</span>
                  {hint && <span className="truncate text-[11px] text-faint">{hint}</span>}
                </span>
              </MenuItem>
            );
          })}
          {canWrite && (
            <>
              <MenuSeparator />
              <MenuItem icon={<SlidersHorizontal />} onSelect={() => setOpen(true)}>
                Other settings…
              </MenuItem>
            </>
          )}
        </MenuContent>
      </Menu>
      {dialog}
    </>
  );
}

function ExportDialog({ ns, runId, sinks, platforms, configured, onClose }: {
  ns: string;
  runId: string;
  sinks: Sink[];
  platforms: Resource[];
  configured: SinkRef[];
  onClose: () => void;
}) {
  const [target, setTarget] = useState<SinkRef>(configured[0] ?? defaultRef(platforms, sinks));
  const run = useMutation({ mutationFn: () => exportRun(runId, targetBody(target)) });
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      size="lg"
      title="Export to an eval platform"
      description="Pushes this run as an experiment: one trace per case with its steps, and every grader's score."
      footer={
        run.data ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" icon={<CloudUpload className="size-3.5" />} loading={run.isPending} disabled={refMissing(target, sinks).length > 0} onClick={() => run.mutate()}>
              Export
            </Button>
          </>
        )
      }
    >
      {run.data ? (
        <EmptyState
          icon={<CloudUpload className="size-5" />}
          title={`Exported to ${exportedTo(run.data)}`}
          description="The experiment is ready on the platform."
          action={
            run.data.url ? (
              <a href={run.data.url} target="_blank" rel="noreferrer">
                <Button icon={<ExternalLink className="size-3.5" />}>Open experiment</Button>
              </a>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-4">
          {configured.length > 0 && <p className="text-xs text-muted">Starts from this eval's export settings. Changes apply to this export only and aren't saved to the eval.</p>}
          <Field label="Destination" className="w-72">
            <TargetSelect
              value={target}
              onChange={(t) => setTarget(configured.find((c) => (t.platform ? c.platform === t.platform : !c.platform && c.sink === t.sink)) ?? t)}
              sinks={sinks}
              platforms={platforms}
            />
          </Field>
          <TargetSettings ns={ns} value={target} onChange={(config) => setTarget({ ...target, config })} sinks={sinks} platforms={platforms} />
          {run.error && <p className="text-xs text-bad">{run.error instanceof ApiError ? run.error.detail : String(run.error)}</p>}
        </div>
      )}
    </Dialog>
  );
}

// --------------------------------------------------------------------------- admin console: eval platforms

type TestResult = { ok: boolean | null; sink?: string; project?: string | null; detail?: string | null };

/** Admin console editor for an EvalPlatform: the sink, its settings with shared secrets, and a connection check. */
export const evalPlatformExtension: EditorExtension = {
  sections: (ctx) => <PlatformConnection ctx={ctx} />,
};

function PlatformConnection({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite, errors, isNew, name, dirty } = ctx;
  const sinks = useEvalSinks();
  const list = sinks.data ?? [];
  const sinkName = (spec.sink as string | undefined) ?? list[0]?.name ?? "";
  const sink = list.find((x) => x.name === sinkName);
  const config = (spec.config as Record<string, unknown> | undefined) ?? {};
  const missing = missingFields(sink, config);
  return (
    <>
      <Card>
        <CardHeader
          title="Connection"
          description="Keys are picked from secrets in the shared namespace, so they never appear here or in an eval. Evals pick this platform by name and can only change plain settings such as the project."
        />
        <fieldset disabled={!canWrite} className="space-y-4 p-4">
          <Field label="Platform" required className="w-80" error={errors.sink}>
            <SinkSelect sinks={list} value={sinkName} onChange={(v) => setSpec({ ...spec, sink: v, config: starterConfig(list.find((x) => x.name === v)) })} />
          </Field>
          <SinkConfigFields ns={SHARED_NS} sink={sink} value={config} onChange={(c) => setSpec({ ...spec, sink: sinkName, config: c })} disabled={!canWrite} />
          {errors.config && <p className="text-xs text-bad">{errors.config}</p>}
          {missing.length > 0 && <Badge tone="warn">Needs {missingText(missing)}</Badge>}
        </fieldset>
      </Card>
      {!isNew && name && <PlatformCheck name={name} dirty={dirty} />}
    </>
  );
}

function PlatformCheck({ name, dirty }: { name: string; dirty: boolean }) {
  const test = useMutation({ mutationFn: () => rawJson<TestResult>(`/v1/eval-platforms/${encodeURIComponent(name)}/test`, { method: "POST" }) });
  const datasets = usePlatformDatasets(name);
  const r = test.data;
  return (
    <Card>
      <CardHeader
        title="Check the connection"
        description={dirty ? "Save first: the check uses the saved settings." : "Makes a cheap call with the saved settings and keys."}
        actions={
          <Button size="sm" icon={<PlugZap className="size-3.5" />} loading={test.isPending} disabled={dirty} onClick={() => test.mutate()}>
            Test connection
          </Button>
        }
      />
      <div className="space-y-3 px-4 pb-4">
        {r && (
          <div
            role="status"
            className={cn(
              "flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px]",
              r.ok === true ? "border-ok/25 bg-ok-soft text-ok" : r.ok === false ? "border-bad/25 bg-bad-soft text-bad" : "border-border bg-subtle text-muted",
            )}
          >
            {r.ok === true ? <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> : r.ok === false ? <XCircle className="mt-0.5 size-4 shrink-0" /> : <CircleHelp className="mt-0.5 size-4 shrink-0" />}
            <span>
              <b className="font-semibold">{r.ok === true ? "Connected." : r.ok === false ? "Couldn't connect." : "Not checked."}</b>{" "}
              {r.ok === true && r.project ? `Project ${r.project}. ` : ""}
              {r.detail}
            </span>
          </div>
        )}
        {test.error && <p className="text-xs text-bad">{test.error instanceof ApiError ? test.error.detail : String(test.error)}</p>}
        <div>
          <div className="mb-1.5 text-xs font-medium text-muted">Datasets</div>
          {datasets.isLoading ? (
            <Skeleton className="h-6 w-64" />
          ) : datasets.error ? (
            <p className="text-xs text-faint">{datasets.error instanceof ApiError ? datasets.error.detail : "Couldn't list datasets."}</p>
          ) : datasets.data?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {datasets.data.map((d) => (
                <Badge key={d} className="font-mono">
                  {d}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-xs text-faint">No datasets yet.</p>
          )}
        </div>
      </div>
    </Card>
  );
}
