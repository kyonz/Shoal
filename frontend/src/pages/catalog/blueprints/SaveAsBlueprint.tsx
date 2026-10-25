import { AlertTriangle, ArrowRight, Check, ExternalLink, Lock, PackagePlus, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { stringify } from "yaml";
import { useResources } from "@/api/hooks";
import { CodeEditor } from "@/components/ui/code";
import { Badge, Button, Dialog, ErrorNotice, Field, Input, NativeSelect, Segmented, Spinner, Switch, Textarea } from "@/components/ui";
import { cn } from "@/lib/cn";
import { nsPath } from "@/lib/ns";
import {
  spec as blueprintSpec,
  usePackagePreview,
  useSaveAsBlueprint,
  type Finding,
  type FindingChoice,
  type SaveAsBlueprintBody,
  type SaveAsBlueprintResult,
} from "./api";

const CATEGORY_LABEL: Record<Finding["category"], string> = {
  credential: "Credential",
  connection: "Connection",
  secret: "Secret",
  repository: "Repository",
  organization: "Organisation",
  jira_project: "Jira project",
  model: "Model",
  sandbox: "Sandbox profile",
  cloud_identity: "Cloud identity",
};

const STEPS = ["Contents", "Parameters", "Details", "Review"] as const;
type Step = (typeof STEPS)[number] | "Done";

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const refKey = (r: { kind: string; name: string }) => `${r.kind}/${r.name}`;

/** Package a workflow, with the agents, skills and triggers it uses, as a blueprint other teams adopt. */
export function SaveAsBlueprintWizard({ open, onOpenChange, ns, workflow }: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  ns: string;
  workflow: string;
}) {
  const blueprints = useResources("blueprints", ns, { enabled: open });
  const save = useSaveAsBlueprint(ns, workflow);
  const [step, setStep] = useState<Step>("Contents");
  const [target, setTarget] = useState<"new" | "existing">("new");
  const [name, setName] = useState(workflow);
  const [existing, setExisting] = useState("");
  const [version, setVersion] = useState("");
  const [include, setInclude] = useState<Set<string> | null>(null);
  const [choices, setChoices] = useState<Record<string, FindingChoice>>({});
  const [details, setDetails] = useState({ title: "", summary: "", tags: "", categories: "", namespaces: "" });
  const [readme, setReadme] = useState<string | null>(null);
  const [publish, setPublish] = useState(false);
  const [done, setDone] = useState<SaveAsBlueprintResult | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep("Contents");
    setTarget("new");
    setName(workflow);
    setExisting("");
    setVersion("");
    setInclude(null);
    setChoices({});
    setDetails({ title: "", summary: "", tags: "", categories: "", namespaces: "" });
    setReadme(null);
    setPublish(false);
    setDone(null);
    save.reset();
  }, [open, workflow]); // eslint-disable-line react-hooks/exhaustive-deps

  const body = useMemo((): Omit<SaveAsBlueprintBody, "dry_run"> => {
    const b: Omit<SaveAsBlueprintBody, "dry_run"> = { target, blueprint: target === "new" ? name || undefined : existing || undefined, params: choices };
    if (version.trim()) b.version = version.trim();
    if (include) b.include = [...include].map((k) => ({ kind: k.split("/")[0], name: k.slice(k.indexOf("/") + 1) }));
    if (details.title.trim()) b.title = details.title.trim();
    if (details.summary.trim()) b.summary = details.summary.trim();
    if (details.tags.trim()) b.tags = list(details.tags);
    if (details.categories.trim()) b.categories = list(details.categories);
    if (details.namespaces.trim()) b.namespaces = list(details.namespaces);
    if (readme !== null) b.readme = readme;
    if (publish) b.status = "published";
    return b;
  }, [target, name, existing, version, include, choices, details, readme, publish]);

  const ready = open && (target === "new" ? /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(name) : !!existing);
  const preview = usePackagePreview(ns, workflow, body, ready && step !== "Done");
  const data = preview.data;
  const bp = data?.blueprint.spec;
  const existingBlueprints = (blueprints.data ?? []).map((r) => ({ name: r.metadata.name, spec: blueprintSpec(r) }));
  const current = existingBlueprints.find((b) => b.name === (target === "new" ? name : existing));
  const clash = target === "new" && !!data?.exists;
  const choose = (id: string, patch: FindingChoice) => setChoices((c) => ({ ...c, [id]: { ...c[id], ...patch } }));
  const index = STEPS.indexOf(step as (typeof STEPS)[number]);

  const submit = async () => {
    try {
      const res = await save.mutateAsync(body);
      setDone(res);
      setStep("Done");
      toast.success(`${res.outcome === "created" ? "Created" : "Saved"} ${res.blueprint.metadata.name} ${res.blueprint.spec.version}`);
    } catch {
      /* shown below */
    }
  };

  const next = () => setStep(STEPS[Math.min(index + 1, STEPS.length - 1)]);
  const footer =
    step === "Done" ? (
      <>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>Close</Button>
        <Link to={nsPath(ns, "blueprints", done?.blueprint.metadata.name ?? "")} onClick={() => onOpenChange(false)}>
          <Button variant="primary" icon={<ExternalLink className="size-4" />}>Open blueprint</Button>
        </Link>
      </>
    ) : (
      <>
        {index > 0 ? <Button variant="ghost" onClick={() => setStep(STEPS[index - 1])}>Back</Button> : <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>}
        {step === "Review" ? (
          <Button variant="primary" loading={save.isPending} disabled={!data || clash || preview.isError} onClick={() => void submit()} icon={<Check className="size-4" />}>
            {target === "existing" ? `Publish v${bp?.version ?? ""}` : publish ? "Publish blueprint" : "Save draft"}
          </Button>
        ) : (
          <Button variant="primary" disabled={!data || preview.isError || (step === "Contents" && clash)} onClick={next} icon={<ArrowRight className="size-4" />}>
            Next
          </Button>
        )}
      </>
    );

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="xl"
      title={`Save ${workflow} as a blueprint`}
      description="Package this workflow so other teams in your organisation can adopt it and wire it to their own systems."
      footer={footer}
    >
      <ol className="mb-4 flex flex-wrap items-center gap-2 text-xs">
        {STEPS.map((st, i) => {
          const reached = step === "Done" || index >= i;
          return (
            <li key={st} className="flex items-center gap-2">
              <button
                type="button"
                disabled={!data || step === "Done"}
                onClick={() => setStep(st)}
                className={cn("flex size-5 items-center justify-center rounded-full border text-[10px] font-semibold", reached ? "border-accent bg-accent text-accent-fg" : "border-border text-faint")}
              >
                {step === "Done" || index > i ? <Check className="size-3" /> : i + 1}
              </button>
              <span className={reached ? "text-fg" : "text-faint"}>{st}</span>
              {i < STEPS.length - 1 && <span className="h-px w-8 bg-border" />}
            </li>
          );
        })}
        {preview.isFetching && <Spinner className="ml-auto size-3.5" />}
      </ol>

      {preview.isError && step !== "Done" && <ErrorNotice error={preview.error} className="mb-3" />}

      {step === "Contents" && (
        <div className="space-y-4">
          <Segmented<"new" | "existing">
            value={target}
            onChange={setTarget}
            options={[
              { value: "new", label: "New blueprint" },
              { value: "existing", label: "New version of a blueprint" },
            ]}
          />
          <div className="grid gap-3 sm:grid-cols-2">
            {target === "new" ? (
              <Field label="Blueprint name" required description="Lowercase letters, digits and dashes." error={clash ? `${name} already exists. Publish a new version of it instead.` : undefined}>
                <Input mono value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} />
              </Field>
            ) : (
              <Field label="Blueprint" required>
                <NativeSelect value={existing} onChange={(e) => setExisting(e.target.value)}>
                  <option value="">{existingBlueprints.length ? "Choose a blueprint…" : "No blueprints yet"}</option>
                  {existingBlueprints.map((b) => (
                    <option key={b.name} value={b.name}>{b.spec.title} ({b.name}, v{b.spec.version})</option>
                  ))}
                </NativeSelect>
              </Field>
            )}
            <Field label="Version" hint="semver" description={current && target === "existing" ? `Current version is ${current.spec.version}.` : "Defaults to 0.1.0, or the next minor version."}>
              <Input mono value={version} placeholder={bp?.version ?? "0.1.0"} onChange={(e) => setVersion(e.target.value)} />
            </Field>
          </div>
          <div>
            <div className="mb-1.5 text-xs font-medium">What goes in the package</div>
            {!data ? (
              <div className="flex items-center gap-2 text-[13px] text-muted"><Spinner className="size-3.5" /> Looking at what {workflow} uses…</div>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border">
                {data.resources.map((r) => {
                  const key = refKey(r);
                  const shared = r.namespace === "shared";
                  return (
                    <li key={key} className="flex items-center gap-3 px-3 py-2 text-[13px]">
                      <Switch
                        checked={r.included}
                        disabled={r.root}
                        onCheckedChange={(v) => {
                          const set = new Set(include ?? data.resources.filter((x) => x.included && !x.root).map(refKey));
                          if (v) set.add(key);
                          else set.delete(key);
                          setInclude(set);
                        }}
                      />
                      <Badge className="w-24 justify-center">{r.kind}</Badge>
                      <span className="font-mono text-xs">{r.name}</span>
                      <span className="truncate text-xs text-faint">{r.reason}</span>
                      {shared && <Badge tone="info" className="ml-auto" title="Visible to every namespace, so it is referenced rather than copied">shared</Badge>}
                    </li>
                  );
                })}
              </ul>
            )}
            <p className="mt-1.5 text-xs text-faint">Included resources are copied into each adopting namespace, renamed with the adopter's prefix. Left out, they stay references by name.</p>
          </div>
        </div>
      )}

      {step === "Parameters" && data && (
        <div className="space-y-3">
          <p className="text-[13px] text-muted">
            These values tie {workflow} to your team. Turn each into a parameter the adopting team fills in, or keep it as is.
          </p>
          {data.findings.length === 0 && <p className="rounded-lg border border-border px-3 py-4 text-center text-[13px] text-faint">Nothing hard-coded was found.</p>}
          <ul className="space-y-2">
            {data.findings.map((f) => (
              <FindingRow key={f.id} finding={f} onChange={(patch) => choose(f.id, patch)} />
            ))}
          </ul>
          {data.stripped.length > 0 && (
            <div className="flex gap-2 rounded-lg border border-border bg-ok-soft px-3 py-2 text-[13px]">
              <ShieldCheck className="mt-0.5 size-4 shrink-0 text-ok" />
              <div>
                Credentials found in text were redacted:
                <ul className="mt-0.5 list-disc pl-4 text-xs text-muted">{data.stripped.map((s) => <li key={s}>{s}</li>)}</ul>
              </div>
            </div>
          )}
          <Warnings warnings={data.warnings} />
        </div>
      )}

      {step === "Details" && data && bp && (
        <div className="space-y-3.5">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Title">
              <Input value={details.title} placeholder={bp.title} onChange={(e) => setDetails({ ...details, title: e.target.value })} />
            </Field>
            <Field label="Tags" hint="comma separated">
              <Input value={details.tags} placeholder={(bp.tags ?? []).join(", ") || "jira, github"} onChange={(e) => setDetails({ ...details, tags: e.target.value })} />
            </Field>
          </div>
          <Field label="Summary" description="One line shown in the catalogue.">
            <Input value={details.summary} placeholder={bp.summary} onChange={(e) => setDetails({ ...details, summary: e.target.value })} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Categories" hint="comma separated">
              <Input value={details.categories} placeholder={(bp.categories ?? []).join(", ") || "engineering"} onChange={(e) => setDetails({ ...details, categories: e.target.value })} />
            </Field>
            <Field label="Only these namespaces may adopt" hint="optional" description="Leave empty for every namespace in the organisation.">
              <Input mono value={details.namespaces} placeholder={(bp.namespaces ?? []).join(", ") || "all namespaces"} onChange={(e) => setDetails({ ...details, namespaces: e.target.value })} />
            </Field>
          </div>
          <Field label="README" hint="markdown" description={readme === null ? "Generated from the package; edit it to explain setup to adopting teams." : undefined}>
            <Textarea className="min-h-36" mono value={readme ?? bp.readme ?? ""} onChange={(e) => setReadme(e.target.value)} />
          </Field>
          {target === "new" || bp.status === "draft" || publish ? (
            <Switch
              checked={publish}
              onCheckedChange={setPublish}
              label={<span>{publish ? "Publish now: other namespaces can adopt it" : "Save as a draft: only blueprint publishers can adopt it to try it out"}</span>}
            />
          ) : (
            <p className="text-[13px] text-muted">The new version keeps the blueprint's status, <b>{bp.status}</b>.</p>
          )}
        </div>
      )}

      {step === "Review" && data && bp && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <PackagePlus className="size-4 text-accent" />
            <span className="font-semibold">{bp.title}</span>
            <Badge className="font-mono">{data.blueprint.metadata.name}</Badge>
            <Badge className="font-mono">v{bp.version}</Badge>
            {data.previous_version && <span className="text-xs text-faint">replaces v{data.previous_version} as current</span>}
            <Badge tone={bp.status === "draft" ? "neutral" : "info"}>{bp.status}</Badge>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <div className="mb-1.5 text-xs font-medium">Adopters provide</div>
              <ul className="divide-y divide-border rounded-lg border border-border">
                {Object.entries(bp.params ?? {}).map(([k, p]) => (
                  <li key={k} className="flex items-center gap-2 px-3 py-1.5 text-xs">
                    <span className="font-mono font-medium">{k}</span>
                    <Badge tone={p.type === "connection" ? "accent" : p.type === "secret" ? "warn" : "neutral"}>{p.type === "connection" ? `${p.integration ?? ""} connection` : p.type}</Badge>
                    {p.default != null && <span className="truncate font-mono text-faint">= {String(p.default)}</span>}
                  </li>
                ))}
                {!Object.keys(bp.params ?? {}).length && <li className="px-3 py-2 text-xs text-faint">No parameters.</li>}
              </ul>
            </div>
            <div>
              <div className="mb-1.5 text-xs font-medium">Resources created on adoption</div>
              <ul className="divide-y divide-border rounded-lg border border-border">
                {bp.resources.map((r, i) => (
                  <li key={i} className="flex items-center gap-2 px-3 py-1.5 text-xs">
                    <Badge className="w-24 justify-center">{r.kind}</Badge>
                    <span className="font-mono">
                      <span className="text-faint">prefix-</span>
                      {(r.metadata?.name ?? "").replace(/^\{\{\s*prefix\s*\}\}/, "")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <Warnings warnings={data.warnings} />
          <div>
            <div className="mb-1.5 text-xs font-medium">Blueprint</div>
            <div className="overflow-hidden rounded-lg border border-border">
              <CodeEditor value={stringify(data.blueprint)} readOnly maxHeight="320px" />
            </div>
          </div>
          {save.error ? <ErrorNotice error={save.error} /> : null}
        </div>
      )}

      {step === "Done" && done && (
        <div className="flex flex-col items-center gap-2 py-6 text-center">
          <div className="flex size-11 items-center justify-center rounded-full bg-ok-soft text-ok"><Check className="size-5" /></div>
          <div className="text-[15px] font-semibold">{done.blueprint.spec.title} v{done.blueprint.spec.version} is {done.blueprint.spec.status === "draft" ? "saved as a draft" : "in the catalogue"}</div>
          <p className="max-w-md text-[13px] text-muted">
            {done.blueprint.spec.status === "draft"
              ? "Try adopting it into a test namespace, then publish it from the blueprint page so other teams can adopt it."
              : "Namespace owners can now find it under Blueprints and adopt it with their own connections."}
          </p>
        </div>
      )}
    </Dialog>
  );
}

function FindingRow({ finding: f, onChange }: { finding: Finding; onChange: (patch: FindingChoice) => void }) {
  const [showWhere, setShowWhere] = useState(false);
  // edited locally and sent on blur, so typing does not wait on a preview per keystroke
  const [draft, setDraft] = useState({ name: f.param, description: f.description, default: f.default == null ? "" : String(f.default) });
  useEffect(() => {
    setDraft({ name: f.param, description: f.description, default: f.default == null ? "" : String(f.default) });
  }, [f.param, f.description, f.default]);
  const takesDefault = !f.locked && f.type !== "connection" && f.type !== "secret";
  const commit = () => {
    const patch: FindingChoice = {};
    if (draft.name && draft.name !== f.param) patch.name = draft.name;
    if (draft.description !== f.description) patch.description = draft.description;
    if (takesDefault && draft.default !== (f.default == null ? "" : String(f.default))) patch.default = draft.default === "" ? null : draft.default;
    if (Object.keys(patch).length) onChange(patch);
  };
  return (
    <li className={cn("rounded-lg border px-3 py-2.5", f.enabled ? "border-border" : "border-dashed border-border opacity-80")}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={f.category === "credential" ? "bad" : f.type === "connection" ? "accent" : f.type === "secret" ? "warn" : "neutral"}>{CATEGORY_LABEL[f.category]}</Badge>
        {f.locked ? (
          <span className="flex items-center gap-1 text-xs text-muted"><Lock className="size-3" /> a clear-text value was removed</span>
        ) : (
          <span className="font-mono text-xs">{f.value}</span>
        )}
        <button type="button" className="text-xs text-faint hover:text-fg" onClick={() => setShowWhere((v) => !v)}>
          {f.locations.length} place{f.locations.length === 1 ? "" : "s"}
        </button>
        <span className="ml-auto">
          <Switch
            checked={f.enabled}
            disabled={f.locked}
            onCheckedChange={(v) => onChange({ enabled: v })}
            label={<span className="text-xs text-muted">{f.locked ? "Always a parameter" : f.enabled ? "Parameter" : "Keep as is"}</span>}
          />
        </span>
      </div>
      {showWhere && <ul className="mt-1.5 list-disc pl-5 text-[11px] text-faint">{f.locations.map((l) => <li key={l}>{l}</li>)}</ul>}
      {f.enabled && (
        <div className={cn("mt-2 grid gap-2", takesDefault ? "sm:grid-cols-[180px_minmax(0,1fr)_180px]" : "sm:grid-cols-[180px_minmax(0,1fr)]")}>
          <Input mono aria-label="Parameter name" value={draft.name} onBlur={commit} onChange={(e) => setDraft({ ...draft, name: e.target.value.replace(/[^A-Za-z0-9_]/g, "_") })} />
          <Input aria-label="Description" value={draft.description} onBlur={commit} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          {takesDefault && (
            <Input mono aria-label="Default" placeholder="no default (required)" value={draft.default} onBlur={commit} onChange={(e) => setDraft({ ...draft, default: e.target.value })} />
          )}
        </div>
      )}
    </li>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  return (
    <div className="flex gap-2 rounded-lg border border-border bg-warn-soft px-3 py-2 text-[13px] text-warn">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <ul className="space-y-0.5">{warnings.map((w) => <li key={w}>{w}</li>)}</ul>
    </div>
  );
}
