/**
 * Generic editor for declarative resources: a schema-driven form, a two-way
 * YAML view validated by the server (dry-run apply) and version history with
 * diffs. Kind-specific widgets, sections, tabs and actions plug in through
 * `EditorExtension`.
 */
import { useQueryClient } from "@tanstack/react-query";
import { diffLines } from "diff";
import { ChevronRight, Code2, FileText, History, MoreHorizontal, RotateCcw, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import YAML from "yaml";
import { ApiError, applyYaml, rawJson, type Resource, type Schemas } from "@/api/client";
import {
  TENANT_SCOPED,
  resourcePath,
  useDeleteResource,
  usePutResource,
  useResource,
  useResourceVersions,
  useSchemas,
  type JsonSchema,
} from "@/api/hooks";
import { useCan } from "@/lib/auth";
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
  ErrorNotice,
  Field,
  Input,
  Kbd,
  Menu,
  MenuContent,
  MenuItem,
  MenuTrigger,
  Page,
  PageHeader,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Textarea,
  useConfirm,
} from "@/components/ui";
import { KeyValueInput, SchemaForm, type Widget } from "@/components/forms/SchemaForm";
import { KINDS, NAME_RE } from "./kinds";

export interface Draft {
  name: string;
  description: string;
  labels: Record<string, string>;
  spec: Record<string, unknown>;
}

export interface EditorContext {
  ns: string;
  plural: string;
  name: string | undefined;
  isNew: boolean;
  draft: Draft;
  spec: Record<string, unknown>;
  setSpec: (spec: Record<string, unknown>) => void;
  resource: Resource | undefined;
  canWrite: boolean;
  dirty: boolean;
}

export interface EditorExtension {
  widgets?: (ctx: EditorContext) => Record<string, Widget>;
  /** Extra cards under the generic form (for fields listed in `hidden`). */
  sections?: (ctx: EditorContext) => ReactNode;
  /** Extra tabs after History. */
  tabs?: (ctx: EditorContext) => { value: string; label: ReactNode; content: ReactNode }[];
  /** Extra header actions (e.g. "Run eval"). */
  actions?: (ctx: EditorContext) => ReactNode;
  /** Shown above the tabs (e.g. adoption badge, notices). */
  banner?: (ctx: EditorContext) => ReactNode;
  /** Where "back" and the breadcrumb go (default: the list). */
  listPath?: (ns: string) => string;
  /** Where to go after saving a new resource (default: its editor). */
  afterCreatePath?: (ns: string, name: string) => string;
}

// --------------------------------------------------------------------------- helpers

/** The resource keeps its human description in spec.description when the kind has one. */
function specHasDescription(schema: JsonSchema | undefined) {
  return !!schema?.properties?.description;
}

function fromResource(r: Resource, schema: JsonSchema | undefined): Draft {
  const spec = { ...(r.spec ?? {}) };
  const useSpec = specHasDescription(schema);
  return {
    name: r.metadata.name,
    description: ((useSpec ? spec.description : r.metadata.description) as string | undefined) ?? r.metadata.description ?? "",
    labels: { ...(r.metadata.labels ?? {}) },
    spec,
  };
}

export function toDocument(kind: string, d: Draft, schema: JsonSchema | undefined) {
  const useSpec = specHasDescription(schema);
  const spec = { ...d.spec };
  if (useSpec) {
    if (d.description) spec.description = d.description;
    else delete spec.description;
  }
  const metadata: Record<string, unknown> = { name: d.name || "my-resource" };
  if (!useSpec && d.description) metadata.description = d.description;
  if (Object.keys(d.labels).length) metadata.labels = d.labels;
  return { apiVersion: "awm.dev/v1", kind, metadata, spec };
}

function toBody(d: Draft, schema: JsonSchema | undefined) {
  const doc = toDocument("", d, schema);
  return {
    metadata: { description: specHasDescription(schema) ? null : d.description || null, labels: d.labels },
    spec: doc.spec,
  };
}

/** Field errors keyed by top-level spec property, plus a flat list. */
function parseErrors(err: unknown): { byField: Record<string, string>; list: string[] } {
  const byField: Record<string, string> = {};
  const list: string[] = [];
  if (!(err instanceof ApiError)) return { byField, list };
  const body = err.body as { details?: { errors?: { loc: string | string[]; msg: string }[] }; errors?: { loc: string[]; msg: string }[] } | undefined;
  const errors = body?.details?.errors ?? body?.errors ?? [];
  for (const e of errors) {
    const loc = Array.isArray(e.loc) ? e.loc : String(e.loc).split(".");
    const i = loc.indexOf("spec");
    const path = (i >= 0 ? loc.slice(i + 1) : loc.filter((p) => p !== "body")).join(".");
    const key = i >= 0 ? loc[i + 1] : undefined;
    if (key && !byField[key]) byField[key] = loc.length > i + 2 ? `${loc.slice(i + 2).join(".")}: ${e.msg}` : e.msg;
    list.push(path ? `${path}: ${e.msg}` : e.msg);
  }
  return { byField, list };
}

export const BLUEPRINT_LABEL = "awm.dev/blueprint";

/** "From blueprint X" badge for resources created by an adoption. */
export function BlueprintBadge({ labels }: { labels?: Record<string, string> }) {
  const bp = labels?.[BLUEPRINT_LABEL];
  if (!bp) return null;
  const version = labels?.["awm.dev/blueprint-version"];
  const name = bp.split("@")[0];
  return (
    <Badge tone="accent" title={`Created by adopting blueprint ${bp}`}>
      from blueprint {name}
      {version ? `@${version}` : bp.includes("@") ? `@${bp.split("@")[1]}` : ""}
    </Badge>
  );
}

// --------------------------------------------------------------------------- page

export default function ResourceEditorPage({ plural, ext = {} }: { plural: string; ext?: EditorExtension }) {
  const cfg = KINDS[plural];
  const ns = useNs();
  const params = useParams();
  const name = params.name;
  const isNew = !name;
  const navigate = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const tenantScoped = TENANT_SCOPED.includes(cfg.plural);
  const canPublish = useCan("blueprints:publish");
  const canWriteKind = useCan(`${plural}:write`, ns);
  const canDeleteKind = useCan(`${plural}:delete`, ns);
  const canWrite = plural === "blueprints" ? canPublish : canWriteKind;
  const canDelete = plural === "blueprints" ? canPublish : canDeleteKind;

  const schemas = useSchemas();
  const specSchema = schemas.data?.[plural]?.spec_schema;
  const resource = useResource(cfg.plural, ns, name);
  const put = usePutResource(cfg.plural, ns);
  const del = useDeleteResource(cfg.plural, ns);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState<string>("");
  const [tab, setTab] = useState("form");
  const [saveError, setSaveError] = useState<unknown>(null);

  // Initialise from the server (or the template for new resources).
  useEffect(() => {
    if (!specSchema) return;
    if (isNew) {
      if (draft) return;
      const d: Draft = { name: "", description: "", labels: {}, spec: structuredClone(cfg.template) };
      if (specHasDescription(specSchema)) d.description = (d.spec.description as string) ?? "";
      setDraft(d);
      setBaseline(JSON.stringify(d));
    } else if (resource.data && (!draft || draft.name !== resource.data.metadata.name)) {
      const d = fromResource(resource.data, specSchema);
      setDraft(d);
      setBaseline(JSON.stringify(d));
    }
  }, [specSchema, resource.data, isNew]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = !!draft && JSON.stringify(draft) !== baseline;
  const nameError = isNew && draft && draft.name && !NAME_RE.test(draft.name) ? "Lowercase letters, digits and dashes; must start and end with a letter or digit." : undefined;
  const errors = useMemo(() => parseErrors(saveError), [saveError]);

  const listPath = ext.listPath?.(ns) ?? nsPath(ns, plural);

  const save = useCallback(async () => {
    if (!draft || !canWrite) return;
    if (isNew && (!draft.name || nameError)) {
      toast.error("Give it a valid name first");
      return;
    }
    setSaveError(null);
    try {
      if (isNew) {
        // PUT creates or replaces; refuse to overwrite an existing resource from "New".
        const exists = await rawJson<Resource>(resourcePath(cfg.plural, ns, draft.name)).then(
          () => true,
          () => false,
        );
        if (exists) {
          toast.error(`A ${cfg.singular} named ${draft.name} already exists`);
          return;
        }
      }
      const res = await put.mutateAsync({ name: draft.name, body: toBody(draft, specSchema) });
      toast.success(isNew ? `Created ${draft.name}` : `Saved ${draft.name} (v${res.metadata.version})`);
      const d = fromResource(res, specSchema);
      setDraft(d);
      setBaseline(JSON.stringify(d));
      if (isNew) navigate(ext.afterCreatePath?.(ns, draft.name) ?? nsPath(ns, plural, draft.name), { replace: true });
    } catch (e) {
      setSaveError(e);
      toast.error(e instanceof Error ? e.message : "Save failed");
    }
  }, [draft, canWrite, isNew, nameError, put, specSchema, navigate, ns, plural, ext, cfg]);

  // Cmd/Ctrl+S
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void saveRef.current();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  // Guard unsaved changes.
  useEffect(() => {
    if (!dirty) return;
    const on = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", on);
    return () => window.removeEventListener("beforeunload", on);
  }, [dirty]);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && !put.isPending && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    void confirm({ title: "Discard unsaved changes?", description: "You have edits that have not been saved.", confirmLabel: "Discard", danger: true }).then((ok) =>
      ok ? blocker.proceed() : blocker.reset(),
    );
  }, [blocker, confirm]);

  const remove = async () => {
    if (!name) return;
    const ok = await confirm({
      title: `Delete ${cfg.singular} ${name}?`,
      description: "Workflows and runs that reference it by name will fail until it is recreated.",
      confirmLabel: "Delete",
      danger: true,
      typeToConfirm: name,
    });
    if (!ok) return;
    try {
      setBaseline(JSON.stringify(draft));
      await del.mutateAsync(name);
      qc.removeQueries({ queryKey: ["resources", cfg.plural, tenantScoped ? "_" : ns, name] });
      toast.success(`Deleted ${name}`);
      navigate(listPath, { replace: true });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Delete failed");
    }
  };

  if (!isNew && resource.error) {
    return (
      <Page>
        <EmptyState
          icon={<cfg.icon className="size-5" />}
          title={resource.error instanceof ApiError && resource.error.status === 404 ? `${cfg.singular[0].toUpperCase()}${cfg.singular.slice(1)} not found` : "Could not load"}
          description={resource.error.message}
          action={<Button onClick={() => navigate(listPath)}>Back to {cfg.label.toLowerCase()}</Button>}
        />
      </Page>
    );
  }

  if (!draft || !specSchema) {
    return (
      <Page>
        <Skeleton className="mb-3 h-6 w-48" />
        <Skeleton className="mb-6 h-4 w-80" />
        <Skeleton className="h-96 w-full" />
      </Page>
    );
  }

  const setSpec = (spec: Record<string, unknown>) => setDraft((d) => (d ? { ...d, spec } : d));
  const ctx: EditorContext = { ns, plural, name, isNew, draft, spec: draft.spec, setSpec, resource: resource.data, canWrite, dirty };
  const extraTabs = isNew ? [] : ext.tabs?.(ctx) ?? [];
  const meta = resource.data?.metadata;

  return (
    <Page>
      <PageHeader
        icon={<cfg.icon className="size-4" />}
        breadcrumb={
          <>
            <Link to={listPath} className="hover:text-fg">
              {cfg.label}
            </Link>
            <ChevronRight className="size-3" />
            <span className="text-fg">{isNew ? `New ${cfg.singular}` : name}</span>
          </>
        }
        title={
          <span className="flex items-center gap-2">
            {isNew ? `New ${cfg.singular}` : name}
            {meta && <Badge className="tabular-nums">v{meta.version}</Badge>}
            <BlueprintBadge labels={meta?.labels} />
            {dirty && (
              <Badge tone="warn" dot>
                Unsaved
              </Badge>
            )}
          </span>
        }
        description={
          meta ? (
            <span>
              Updated {relTime(meta.updated_at)}
              {meta.updated_by ? ` by ${meta.updated_by}` : ""}
            </span>
          ) : (
            cfg.blurb
          )
        }
        actions={
          <>
            {ext.actions?.(ctx)}
            {canWrite && (
              <Button variant="primary" icon={<Save className="size-4" />} loading={put.isPending} disabled={!dirty && !isNew} onClick={() => void save()} title="Save (⌘S)">
                {isNew ? "Create" : "Save"}
                <Kbd className="ml-1 border-white/20 bg-white/15 text-current">⌘S</Kbd>
              </Button>
            )}
            {!isNew && canDelete && (
              <Menu>
                <MenuTrigger asChild>
                  <Button variant="secondary" size="icon" aria-label="More actions">
                    <MoreHorizontal className="size-4" />
                  </Button>
                </MenuTrigger>
                <MenuContent>
                  {dirty && (
                    <MenuItem icon={<RotateCcw />} onSelect={() => setDraft(JSON.parse(baseline))}>
                      Discard changes
                    </MenuItem>
                  )}
                  <MenuItem icon={<Trash2 />} danger onSelect={() => void remove()}>
                    Delete {cfg.singular}
                  </MenuItem>
                </MenuContent>
              </Menu>
            )}
          </>
        }
      />
      {!canWrite && (
        <div className="mb-4 rounded-lg border border-border bg-subtle px-3 py-2 text-[13px] text-muted">
          You can view this {cfg.singular} but not change it (needs <code className="font-mono text-xs">{plural}:write</code>).
        </div>
      )}
      {ext.banner?.(ctx)}
      {saveError ? (
        <div className="mb-4 rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad">
          <b>{saveError instanceof Error ? saveError.message : "Save failed"}</b>
          {errors.list.length > 0 && (
            <ul className="mt-1 list-disc pl-5 font-mono text-xs">
              {errors.list.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-4">
          <TabsTrigger value="form">
            <FileText className="size-3.5" /> Form
          </TabsTrigger>
          <TabsTrigger value="yaml">
            <Code2 className="size-3.5" /> YAML
          </TabsTrigger>
          {!isNew && (
            <TabsTrigger value="history">
              <History className="size-3.5" /> History
            </TabsTrigger>
          )}
          {extraTabs.map((t) => (
            <TabsTrigger key={t.value} value={t.value}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="form" className="outline-none">
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px]">
            <div className="min-w-0 space-y-4">
              <Card className="p-4">
                <div className="grid gap-3.5 sm:grid-cols-2">
                  <Field label="Name" required error={nameError} description={isNew ? (tenantScoped ? "Unique in the organisation; used in links and adoptions." : "Unique in the namespace; other resources refer to it by name.") : "Names cannot be changed."}>
                    <Input
                      mono
                      autoFocus={isNew}
                      disabled={!isNew}
                      value={draft.name}
                      placeholder={`my-${cfg.singular.replace(/ /g, "-")}`}
                      onChange={(e) => setDraft({ ...draft, name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })}
                    />
                  </Field>
                  <Field label="Description" className="sm:col-span-2">
                    <Textarea
                      className="min-h-14"
                      disabled={!canWrite}
                      value={draft.description}
                      placeholder={`What this ${cfg.singular} is for`}
                      onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                    />
                  </Field>
                </div>
              </Card>
              <Card className="p-4">
                <fieldset disabled={!canWrite} className="min-w-0">
                  <SchemaForm
                    schema={specSchema}
                    value={draft.spec}
                    onChange={setSpec}
                    hidden={["description", ...(cfg.hidden ?? [])]}
                    order={cfg.order}
                    widgets={ext.widgets?.(ctx)}
                    errors={errors.byField}
                    collapseOptional
                  />
                </fieldset>
              </Card>
              {ext.sections?.(ctx)}
            </div>
            <div className="space-y-4">
              <Card>
                <CardHeader title="Labels" description="Key/value tags for search and selection." />
                <div className="p-3">
                  <fieldset disabled={!canWrite}>
                    <KeyValueInput key={`${draft.name}-${baseline.length}`} value={draft.labels} onChange={(labels) => setDraft({ ...draft, labels })} />
                  </fieldset>
                </div>
              </Card>
              {meta && (
                <Card className="p-4 text-xs">
                  <dl className="space-y-2">
                    <Row k="Namespace" v={meta.namespace} />
                    <Row k="Version" v={`v${meta.version}`} />
                    <Row k="Created" v={dateTime(meta.created_at)} />
                    <Row k="Updated" v={dateTime(meta.updated_at)} />
                    {meta.updated_by && <Row k="By" v={meta.updated_by} />}
                  </dl>
                </Card>
              )}
            </div>
          </div>
        </TabsContent>

        <TabsContent value="yaml" className="outline-none">
          {tab === "yaml" && <YamlTab kind={cfg.kind} draft={draft} schema={specSchema} ns={ns} isNew={isNew} readOnly={!canWrite} onChange={setDraft} />}
        </TabsContent>

        {!isNew && (
          <TabsContent value="history" className="outline-none">
            {tab === "history" && (
              <HistoryTab
                plural={plural}
                ns={ns}
                name={name!}
                canWrite={canWrite}
                onRestored={(r) => {
                  const d = fromResource(r, specSchema);
                  setDraft(d);
                  setBaseline(JSON.stringify(d));
                  setTab("form");
                }}
              />
            )}
          </TabsContent>
        )}
        {extraTabs.map((t) => (
          <TabsContent key={t.value} value={t.value} className="outline-none">
            {t.content}
          </TabsContent>
        ))}
      </Tabs>
    </Page>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-faint">{k}</dt>
      <dd className="truncate text-right text-muted">{v}</dd>
    </div>
  );
}

// --------------------------------------------------------------------------- YAML

function YamlTab({ kind, draft, schema, ns, isNew, readOnly, onChange }: {
  kind: string;
  draft: Draft;
  schema: JsonSchema;
  ns: string;
  isNew: boolean;
  readOnly: boolean;
  onChange: (d: Draft) => void;
}) {
  const [text, setText] = useState(() => YAML.stringify(toDocument(kind, draft, schema), { lineWidth: 0 }));
  const [parseError, setParseError] = useState<string | null>(null);
  const [check, setCheck] = useState<{ state: "idle" | "checking" | "ok" | "bad"; messages: string[] }>({ state: "idle", messages: [] });

  // Server-side validation (dry-run apply), debounced.
  useEffect(() => {
    if (parseError) return;
    const t = setTimeout(async () => {
      setCheck((c) => ({ ...c, state: "checking" }));
      try {
        await applyYaml(text, { namespace: ns, dryRun: true });
        setCheck({ state: "ok", messages: [] });
      } catch (e) {
        const results = e instanceof ApiError ? e.applyResults : [];
        const messages = results.filter((r) => r.error).map((r) => r.error!) ;
        setCheck({ state: "bad", messages: messages.length ? messages : [e instanceof Error ? e.message : String(e)] });
      }
    }, 600);
    return () => clearTimeout(t);
  }, [text, parseError, ns]);

  const edit = (t: string) => {
    setText(t);
    try {
      const doc = YAML.parse(t) as { kind?: string; metadata?: { name?: string; description?: string; labels?: Record<string, string> }; spec?: Record<string, unknown> };
      if (!doc || typeof doc !== "object") throw new Error("expected a mapping");
      if (doc.kind && doc.kind !== kind) throw new Error(`kind must be ${kind}`);
      if (!doc.spec || typeof doc.spec !== "object") throw new Error("missing spec");
      const name = isNew ? String(doc.metadata?.name ?? "") : draft.name;
      if (!isNew && doc.metadata?.name && doc.metadata.name !== draft.name) throw new Error("the name cannot be changed here");
      const spec = { ...doc.spec };
      const useSpec = specHasDescription(schema);
      const description = (useSpec ? (spec.description as string) : doc.metadata?.description) ?? "";
      onChange({ name, description, labels: doc.metadata?.labels ?? {}, spec });
      setParseError(null);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs">
        <span className="font-medium">{kind}.yaml</span>
        <span className="text-faint">Edits here update the form. Validated by the server as you type.</span>
        <span className="ml-auto">
          {parseError ? (
            <Badge tone="bad">YAML error</Badge>
          ) : check.state === "checking" ? (
            <Badge>Checking…</Badge>
          ) : check.state === "ok" ? (
            <Badge tone="ok" dot>
              Valid
            </Badge>
          ) : check.state === "bad" ? (
            <Badge tone="bad" dot>
              Invalid
            </Badge>
          ) : null}
        </span>
      </div>
      <CodeEditor value={text} onChange={readOnly ? undefined : edit} readOnly={readOnly} language="yaml" minHeight="420px" maxHeight="70vh" />
      {(parseError || (check.state === "bad" && check.messages.length > 0)) && (
        <div className="border-t border-border bg-bad-soft px-4 py-2 font-mono text-xs text-bad">
          {parseError ?? check.messages.map((m) => <div key={m}>{m}</div>)}
        </div>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------- history

function HistoryTab({ plural, ns, name, canWrite, onRestored }: {
  plural: string;
  ns: string;
  name: string;
  canWrite: boolean;
  onRestored: (r: Resource) => void;
}) {
  const cfg = KINDS[plural];
  const versions = useResourceVersions(cfg.plural, ns, name);
  const put = usePutResource(cfg.plural, ns);
  const confirm = useConfirm();
  const list = useMemo(() => [...(versions.data ?? [])].sort((a, b) => b.version - a.version), [versions.data]);
  const [selected, setSelected] = useState<number | null>(null);
  const current = list[0];
  const sel = list.find((v) => v.version === selected) ?? list[1] ?? list[0];

  if (versions.isLoading) return <Skeleton className="h-80 w-full" />;
  if (versions.error) return <ErrorNotice error={versions.error} />;
  if (!list.length) return <EmptyState title="No history yet" />;

  // Compare server-normalised documents: the selected version against the
  // current one (or, when the current one is selected, against its predecessor).
  const prune = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(prune);
    if (v && typeof v === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, x] of Object.entries(v)) {
        const p = prune(x);
        if (p == null || (typeof p === "object" && !Array.isArray(p) && !Object.keys(p).length) || (Array.isArray(p) && !p.length)) continue;
        out[k] = p;
      }
      return out;
    }
    return v;
  };
  const strip = (doc: Record<string, unknown>) => {
    const { metadata, ...rest } = doc as { metadata?: Record<string, unknown> };
    const m = { ...(metadata ?? {}) };
    delete m.namespace;
    return prune({ ...rest, metadata: m });
  };
  const older = sel === current ? list[1] : sel;
  const newer = current;
  const left = older ? YAML.stringify(strip(older.document), { lineWidth: 0 }) : "";
  const right = newer ? YAML.stringify(strip(newer.document), { lineWidth: 0 }) : "";
  const parts = diffLines(left, right);
  const changed = parts.some((p) => p.added || p.removed);

  const restore = async (v: Schemas["ResourceVersionOut"]) => {
    const ok = await confirm({
      title: `Restore version ${v.version}?`,
      description: `This saves v${v.version}'s spec as a new version. Unsaved edits are discarded.`,
      confirmLabel: "Restore",
    });
    if (!ok) return;
    const doc = v.document as { metadata?: { description?: string; labels?: Record<string, string> }; spec: Record<string, unknown> };
    try {
      const res = await put.mutateAsync({
        name,
        body: { metadata: { description: doc.metadata?.description ?? null, labels: doc.metadata?.labels ?? {} }, spec: doc.spec },
      });
      toast.success(`Restored v${v.version} as v${res.metadata.version}`);
      onRestored(res);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Restore failed");
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
      <Card className="h-fit overflow-hidden">
        <div className="border-b border-border px-3 py-2 text-[11px] font-semibold tracking-wide text-faint uppercase">Versions</div>
        <ul className="max-h-[560px] overflow-y-auto">
          {list.map((v) => (
            <li key={v.version}>
              <button
                onClick={() => setSelected(v.version)}
                className={cn(
                  "flex w-full items-start gap-2 border-l-2 px-3 py-2 text-left transition-colors hover:bg-subtle",
                  sel?.version === v.version ? "border-accent bg-accent-soft/50" : "border-transparent",
                )}
              >
                <span className="mt-0.5 font-mono text-xs font-semibold tabular-nums">v{v.version}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-xs">{relTime(v.created_at)}</span>
                  <span className="block truncate text-[11px] text-faint">{v.created_by ?? "unknown"}</span>
                </span>
                {v === current && <Badge tone="ok">current</Badge>}
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card className="min-w-0 overflow-hidden">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs">
          <span className="font-medium">
            {older ? <>v{older.version} <span className="text-faint">→</span> v{newer?.version}</> : `v${current?.version} (first version)`}
          </span>
          <span className="text-faint">{dateTime((older ?? sel)?.created_at)}</span>
          {canWrite && sel && sel !== current && (
            <Button size="sm" className="ml-auto" icon={<RotateCcw className="size-3.5" />} loading={put.isPending} onClick={() => void restore(sel)}>
              Restore this version
            </Button>
          )}
        </div>
        {!changed ? (
          <div className="px-4 py-10 text-center text-[13px] text-muted">{older ? "No differences." : "This is the first version; there is nothing to compare."}</div>
        ) : (
          <pre className="max-h-[560px] overflow-auto py-2 font-mono text-[12px] leading-[1.6]">
            {parts.map((p, i) =>
              p.value
                .replace(/\n$/, "")
                .split("\n")
                .map((line, j) => (
                  <div
                    key={`${i}-${j}`}
                    className={cn("px-4 whitespace-pre-wrap", p.added && "bg-ok-soft text-ok", p.removed && "bg-bad-soft text-bad", !p.added && !p.removed && "text-muted")}
                  >
                    <span className="mr-3 inline-block w-3 select-none opacity-60">{p.added ? "+" : p.removed ? "−" : " "}</span>
                    {line}
                  </div>
                )),
            )}
          </pre>
        )}
      </Card>
    </div>
  );
}
