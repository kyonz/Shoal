/**
 * Eval platforms (Braintrust, Langfuse, … via `shoal.eval_sinks`): export eval runs as experiments,
 * and take cases from a platform dataset. Settings follow each sink's config schema, with secrets
 * picked by name (`*_secret`), so keys never appear in the Eval.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, CloudUpload, DatabaseZap, ExternalLink, Plus, SlidersHorizontal, Trash2, Upload } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ApiError, rawJson, type Resource } from "@/api/client";
import { keys, type JsonSchema } from "@/api/hooks";
import { humanize } from "@/components/forms/SchemaForm";
import { useCan } from "@/lib/auth";
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
  Switch,
} from "@/components/ui";
import { SecretPicker } from "@/pages/connect/shared";
import type { EditorContext } from "../ResourceEditorPage";

export interface Sink {
  name: string;
  description: string;
  config_schema: JsonSchema;
}

export interface SinkRef {
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

/** Fill in a sink's required fields with sensible secret names, so a new destination is one pick away. */
function starterConfig(sink: Sink | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of sink?.config_schema.required ?? []) if (k.endsWith("_secret")) out[k] = `${sink!.name}-${k.replace(/_secret$/, "").replace(/_/g, "-")}`;
  return out;
}

export function missingFields(sink: Sink | undefined, config: Record<string, unknown> | undefined): string[] {
  return (sink?.config_schema.required ?? []).filter((k) => !config?.[k]);
}

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
            field={k.replace(/_secret$/, "")}
            description={`Secret holding the ${humanize(k.replace(/_secret$/, "")).toLowerCase()}${required.has(k) ? " (required)" : ""}.`}
            value={value[k] as string | undefined}
            disabled={disabled}
            onChange={(v) => set(k, v)}
          />
        ) : (
          <Field key={k} label={humanize(k)} required={required.has(k)} description={s.description}>
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

// --------------------------------------------------------------------------- eval editor card

/** "Eval platform" card on the eval editor: export destinations and where the cases come from. */
export function PlatformCard({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite, ns, isNew, name, dirty, replaceFrom } = ctx;
  const sinks = useEvalSinks();
  const list = sinks.data ?? [];
  const exports = (spec.sinks as SinkRef[] | undefined) ?? [];
  const dataset = spec.dataset as DatasetRef | undefined;
  const [importing, setImporting] = useState(false);
  const byName = (n: string) => list.find((s) => s.name === n);

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
    const first = list.find((s) => !exports.some((e) => e.sink === s.name)) ?? list[0];
    if (first) setExports([...exports, { sink: first.name, config: starterConfig(first) }]);
  };

  if (sinks.isSuccess && !list.length) return null;

  return (
    <Card>
      <CardHeader
        title="Eval platform"
        description={`Send results to ${list.map((s) => sinkLabel(s.name)).join(" or ") || "an eval platform"} as experiments, and take cases from a dataset there. Keys come from secrets.`}
      />
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
            const sink = byName(e.sink);
            const missing = missingFields(sink, e.config);
            return (
              <div key={i} className="rounded-lg border border-border p-3">
                <div className="mb-3 flex items-end gap-2">
                  <Field label="Platform" className="w-64">
                    <SinkSelect sinks={list} value={e.sink} onChange={(v) => setExports(exports.map((x, j) => (j === i ? { sink: v, config: starterConfig(byName(v)) } : x)))} />
                  </Field>
                  {missing.length > 0 && <Badge tone="warn">Needs {missing.map((m) => humanize(m.replace(/_secret$/, "")).toLowerCase()).join(", ")}</Badge>}
                  <Button variant="ghost" size="icon-sm" className="mb-1 ml-auto" aria-label="Remove destination" onClick={() => setExports(exports.filter((_, j) => j !== i))}>
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
                <SinkConfigFields ns={ns} sink={sink} value={e.config ?? {}} onChange={(config) => setExports(exports.map((x, j) => (j === i ? { ...x, config } : x)))} />
              </div>
            );
          })}
          {exports.length > 0 && exports.length < list.length && (
            <Button size="sm" variant="ghost" icon={<Plus className="size-3.5" />} onClick={addExport}>
              Add another platform
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
                onCheckedChange={(on) => setDataset(on ? { sink: list[0]?.name ?? "braintrust", name: "", config: starterConfig(list[0]) } : undefined)}
                disabled={!canWrite || !list.length}
              />
            </div>
          </div>
          {dataset && (
            <div className="rounded-lg border border-border p-3">
              <div className="mb-3 grid gap-3 sm:grid-cols-[16rem_minmax(0,1fr)_8rem]">
                <Field label="Platform">
                  <SinkSelect sinks={list} value={dataset.sink} onChange={(v) => setDataset({ ...dataset, sink: v, config: starterConfig(byName(v)) })} />
                </Field>
                <Field label="Dataset" required>
                  <Input mono value={dataset.name} placeholder="support-tickets-golden" onChange={(e) => setDataset({ ...dataset, name: e.target.value })} />
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
              <SinkConfigFields ns={ns} sink={byName(dataset.sink)} value={dataset.config ?? {}} onChange={(config) => setDataset({ ...dataset, config })} />
            </div>
          )}
        </section>
      </fieldset>
      {importing && name && list.length > 0 && <ImportDialog open onOpenChange={setImporting} ns={ns} evalName={name} sinks={list} initial={exports[0]} onImported={replaceFrom} />}
    </Card>
  );
}

// --------------------------------------------------------------------------- import once

function ImportDialog({ open, onOpenChange, ns, evalName, sinks, initial, onImported }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  ns: string;
  evalName: string;
  sinks: Sink[];
  initial?: SinkRef;
  onImported: (r: Resource) => void;
}) {
  const qc = useQueryClient();
  const [sink, setSink] = useState(initial?.sink ?? sinks[0]?.name ?? "");
  const [config, setConfig] = useState<Record<string, unknown>>(initial?.config ?? starterConfig(sinks[0]));
  const [dataset, setDataset] = useState("");
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [limit, setLimit] = useState("");
  const current = sinks.find((s) => s.name === sink);
  const run = useMutation({
    mutationFn: () =>
      rawJson<Resource>(`/v1/namespaces/${ns}/evals/${encodeURIComponent(evalName)}/import`, {
        method: "POST",
        body: JSON.stringify({ sink, config, dataset, mode, limit: limit ? Number(limit) : null }),
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
          <Button variant="primary" icon={<Upload className="size-3.5" />} loading={run.isPending} disabled={!dataset || missingFields(current, config).length > 0} onClick={() => run.mutate()}>
            Import
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-[14rem_minmax(0,1fr)]">
          <Field label="Platform">
            <SinkSelect
              sinks={sinks}
              value={sink}
              onChange={(v) => {
                setSink(v);
                setConfig(starterConfig(sinks.find((s) => s.name === v)));
              }}
            />
          </Field>
          <Field label="Dataset" required>
            <Input mono autoFocus value={dataset} onChange={(e) => setDataset(e.target.value)} placeholder="support-tickets-golden" />
          </Field>
        </div>
        <SinkConfigFields ns={ns} sink={current} value={config} onChange={setConfig} />
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

type ExportResult = { sink: string; url: string | null };

function exportRun(runId: string, sink: string, config?: Record<string, unknown>) {
  // Without config the server uses the eval's own settings for that sink, so no secrets travel from here.
  return rawJson<ExportResult>(`/v1/eval-runs/${runId}/export`, { method: "POST", body: JSON.stringify(config ? { sink, config } : { sink }) });
}

/**
 * Export a finished run. Destinations already set up on the eval are one click (anyone who can run the
 * eval). Other settings name secrets, so only people who can edit the eval get them.
 */
export function ExportRunButton({ ns, runId, evalSpec }: { ns: string; runId: string; evalSpec?: Record<string, unknown> }) {
  const sinks = useEvalSinks();
  const canWrite = useCan("evals:write", ns);
  const [open, setOpen] = useState(false);
  const list = sinks.data ?? [];
  const configured = ((evalSpec?.sinks as SinkRef[] | undefined) ?? []).filter((c) => list.some((s) => s.name === c.sink));
  const quick = useMutation({
    mutationFn: (sink: string) => exportRun(runId, sink),
    onSuccess: (r) =>
      toast.success(`Exported to ${sinkLabel(r.sink)}`, r.url ? { action: { label: "Open experiment", onClick: () => window.open(r.url!, "_blank", "noopener") } } : undefined),
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
  if (!list.length || (!configured.length && !canWrite)) return null;

  const dialog = open && <ExportDialog ns={ns} runId={runId} sinks={list} configured={configured} onClose={() => setOpen(false)} />;
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
      <Button icon={<CloudUpload className="size-4" />} loading={quick.isPending} onClick={() => quick.mutate(configured[0].sink)}>
        Export to {sinkLabel(configured[0].sink)}
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
            const hint = destinationHint(c.config);
            return (
              <MenuItem key={`${c.sink}-${i}`} icon={<CloudUpload />} onSelect={() => quick.mutate(c.sink)}>
                <span className="flex min-w-0 flex-col">
                  <span>Export to {sinkLabel(c.sink)}</span>
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

function ExportDialog({ ns, runId, sinks, configured, onClose }: { ns: string; runId: string; sinks: Sink[]; configured: SinkRef[]; onClose: () => void }) {
  const first = configured[0];
  const [sink, setSink] = useState(first?.sink ?? sinks[0].name);
  const [config, setConfig] = useState<Record<string, unknown>>(first?.config ?? starterConfig(sinks[0]));
  const current = sinks.find((s) => s.name === sink);
  const run = useMutation({ mutationFn: () => exportRun(runId, sink, config) });
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
            <Button variant="primary" icon={<CloudUpload className="size-3.5" />} loading={run.isPending} disabled={missingFields(current, config).length > 0} onClick={() => run.mutate()}>
              Export
            </Button>
          </>
        )
      }
    >
      {run.data ? (
        <EmptyState
          icon={<CloudUpload className="size-5" />}
          title={`Exported to ${sinkLabel(run.data.sink)}`}
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
          <Field label="Platform" className="w-64">
            <SinkSelect
              sinks={sinks}
              value={sink}
              onChange={(v) => {
                setSink(v);
                const c = configured.find((x) => x.sink === v);
                setConfig(c?.config ?? starterConfig(sinks.find((s) => s.name === v)));
              }}
            />
          </Field>
          <SinkConfigFields ns={ns} sink={current} value={config} onChange={setConfig} />
          {run.error && <p className="text-xs text-bad">{run.error instanceof ApiError ? run.error.detail : String(run.error)}</p>}
        </div>
      )}
    </Dialog>
  );
}
