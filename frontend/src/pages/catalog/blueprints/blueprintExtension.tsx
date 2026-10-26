import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import YAML from "yaml";
import { isRecord, list, record, scalar, string } from "@/lib/shape";
import { useNamespaces } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { nsPath } from "@/lib/ns";
import { Button, Card, CardHeader, CodeEditor, Field, Input, NativeSelect, Switch } from "@/components/ui";
import { TagInput } from "@/components/forms/SchemaForm";
import type { EditorContext, EditorExtension } from "../ResourceEditorPage";
import { MarkdownEditor } from "../widgets";
import type { BlueprintParam } from "./api";

const STATUSES = ["draft", "published", "featured", "deprecated"];

function StatusSelect({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const canPublish = useCan("blueprints:publish");
  const canFeature = useCan("blueprints:feature");
  const v = value === "promoted" ? "featured" : ((value as string) ?? "draft");
  return (
    <NativeSelect value={v} disabled={!canPublish} onChange={(e) => onChange(e.target.value)}>
      {STATUSES.map((s) => (
        <option key={s} value={s} disabled={s === "featured" && !canFeature && v !== "featured"}>
          {s}
          {s === "featured" && !canFeature ? " (needs blueprints:feature)" : ""}
        </option>
      ))}
    </NativeSelect>
  );
}

function NamespacesPicker({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const { data } = useNamespaces();
  return (
    <TagInput
      value={(value as string[]) ?? []}
      onChange={(l) => onChange(l.length ? l : undefined)}
      options={(data ?? []).map((n) => n.name)}
    />
  );
}

function ParamsEditor({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite } = ctx;
  const params = (spec.params as Record<string, BlueprintParam>) ?? {};
  const entries = Object.entries(params);
  const [newName, setNewName] = useState("");
  const write = (next: [string, BlueprintParam][]) => setSpec({ ...spec, params: Object.fromEntries(next) });
  const update = (i: number, key: string, p: BlueprintParam) => write(entries.map((e, j) => (j === i ? [key, p] : e)));
  return (
    <Card>
      <CardHeader title={`Parameters (${entries.length})`} description="What adopters supply. Use them in resources as {{ params.name }}; {{ prefix }} is the adopter's name prefix." />
      <fieldset disabled={!canWrite} className="divide-y divide-border">
        {entries.map(([k, p], i) => (
          <div key={i} className="grid gap-3 px-4 py-3 md:grid-cols-[160px_150px_minmax(0,1fr)_32px]">
            <Field label="Name">
              <Input mono value={k} onChange={(e) => update(i, e.target.value.replace(/[^A-Za-z0-9_]/g, "_"), p)} />
            </Field>
            <Field label="Type">
              <NativeSelect value={p.type ?? "string"} onChange={(e) => update(i, k, { ...p, type: e.target.value as BlueprintParam["type"] })}>
                {["string", "number", "boolean", "choice", "connection", "secret"].map((t) => <option key={t}>{t}</option>)}
              </NativeSelect>
            </Field>
            <div className="space-y-2">
              <Field label="Description">
                <Input value={p.description ?? ""} onChange={(e) => update(i, k, { ...p, description: e.target.value || undefined })} />
              </Field>
              <div className="flex flex-wrap items-end gap-3">
                {p.type === "connection" && (
                  <Field label="Integration" className="w-40">
                    <Input mono value={p.integration ?? ""} placeholder="github" onChange={(e) => update(i, k, { ...p, integration: e.target.value || undefined })} />
                  </Field>
                )}
                {p.type === "choice" && (
                  <Field label="Options" className="min-w-60 flex-1">
                    <TagInput value={p.options ?? []} onChange={(options) => update(i, k, { ...p, options })} />
                  </Field>
                )}
                {p.type !== "connection" && p.type !== "secret" && (
                  <Field label="Default" className="w-40">
                    <Input value={p.default == null ? "" : String(p.default)} onChange={(e) => update(i, k, { ...p, default: e.target.value === "" ? undefined : p.type === "number" ? Number(e.target.value) : p.type === "boolean" ? e.target.value === "true" : e.target.value })} />
                  </Field>
                )}
                <Switch checked={p.required !== false} onCheckedChange={(c) => update(i, k, { ...p, required: c ? undefined : false })} label={<span className="text-xs">Required</span>} />
              </div>
            </div>
            <Button variant="ghost" size="icon-sm" className="mt-5" aria-label="Remove parameter" onClick={() => write(entries.filter((_, j) => j !== i))}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        ))}
        <div className="flex gap-2 px-4 py-3">
          <Input mono className="w-60" value={newName} placeholder="new_parameter" onChange={(e) => setNewName(e.target.value.replace(/[^A-Za-z0-9_]/g, "_"))} />
          <Button
            size="md"
            icon={<Plus className="size-3.5" />}
            disabled={!newName || newName in params}
            onClick={() => {
              write([...entries, [newName, { type: "string" }]]);
              setNewName("");
            }}
          >
            Add parameter
          </Button>
        </div>
      </fieldset>
    </Card>
  );
}

function ResourcesEditor({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite } = ctx;
  const [text, setText] = useState(() => ((spec.resources as unknown[]) ?? []).map((r) => YAML.stringify(r, { lineWidth: 0 })).join("---\n"));
  const [err, setErr] = useState<string | null>(null);
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Resources" description="Multi-document YAML: the workflow, agents, triggers… this blueprint creates. Templates may use {{ params.* }} and {{ prefix }}." />
      <CodeEditor
        value={text}
        readOnly={!canWrite}
        language="yaml"
        minHeight="280px"
        maxHeight="640px"
        onChange={(t) => {
          setText(t);
          try {
            const docs = YAML.parseAllDocuments(t).map((d) => {
              if (d.errors.length) throw new Error(d.errors[0].message);
              return d.toJS() as unknown;
            }).filter((d) => d != null);
            docs.forEach((d, i) => {
              const at = `Document ${i + 1}`;
              if (!isRecord(d)) throw new Error(`${at} must be a mapping with kind, metadata and spec`);
              string(d.kind, `${at}: kind`);
              const md = record(d.metadata ?? undefined, `${at}: metadata`, true);
              scalar(md?.name, `${at}: metadata.name`, true);
              record(d.spec ?? undefined, `${at}: spec`, true);
              if (d.kind === "Workflow" && isRecord(d.spec)) list(d.spec.steps ?? undefined, `${at}: spec.steps`, true);
            });
            setSpec({ ...spec, resources: docs });
            setErr(null);
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
          }
        }}
      />
      {err && <div className="border-t border-border bg-bad-soft px-4 py-2 font-mono text-xs text-bad">{err}</div>}
    </Card>
  );
}

export const blueprintExtension: EditorExtension = {
  widgets: () => ({
    readme: ({ value, onChange }) => <MarkdownEditor value={value} onChange={onChange} minHeight="320px" />,
    status: ({ value, onChange }) => <StatusSelect value={value} onChange={onChange} />,
    namespaces: ({ value, onChange }) => <NamespacesPicker value={value} onChange={onChange} />,
  }),
  sections: (ctx) => (
    <>
      <ParamsEditor ctx={ctx} />
      <ResourcesEditor ctx={ctx} />
    </>
  ),
  listPath: (ns) => nsPath(ns, "blueprints"),
  afterCreatePath: (ns, name) => nsPath(ns, "blueprints", name),
};
