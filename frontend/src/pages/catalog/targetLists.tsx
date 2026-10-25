/** Target lists: the targets as one-per-line text, an optional lookup through a connection, and a preview. */
import { useMutation } from "@tanstack/react-query";
import { Eye } from "lucide-react";
import { useState } from "react";
import { useIntegrations, useResources } from "@/api/hooks";
import { SchemaForm } from "@/components/forms/SchemaForm";
import { Button, Card, CardHeader, CodeEditor, Field, NativeSelect, Textarea } from "@/components/ui";
import { useCan } from "@/lib/auth";
import { ErrorList, useConnectionCapabilities } from "../connect/shared";
import { TargetListResult, previewTargetList } from "../connect/TargetListPreview";
import type { EditorContext, EditorExtension } from "./ResourceEditorPage";

type Item = Record<string, unknown>;

/** The field a simple key expression reads (`{{ target.full_name }}` → full_name). */
export function keyField(key: unknown): string | undefined {
  return /^\{\{\s*target\.([A-Za-z_]\w*)\s*\}\}$/.exec(String(key ?? "{{ target.id }}").trim())?.[1];
}

/** Items that are just their key can be edited one per line. */
function asLines(items: Item[] | undefined, field: string | undefined): string | undefined {
  if (!field) return undefined;
  if (!items) return "";
  if (!items.every((i) => Object.keys(i).length === 1 && typeof i[field] === "string")) return undefined;
  return items.map((i) => i[field] as string).join("\n");
}

function ItemsEditor({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite } = ctx;
  const items = spec.items as Item[] | undefined;
  const field = keyField(spec.key);
  const lines = asLines(items, field);
  const [json, setJson] = useState(() => JSON.stringify(items ?? [], null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const set = (next: Item[] | undefined) => setSpec({ ...spec, items: next });
  if (lines !== undefined && field) {
    return (
      <Field
        label="Targets"
        description={<>One per line, matched on <code className="font-mono">{field}</code>. With a lookup below, leave this empty to take everything the query returns.</>}
      >
        <Textarea
          rows={8}
          className="font-mono text-xs"
          readOnly={!canWrite}
          value={lines}
          placeholder={field === "full_name" ? "acme/api\nacme/web" : `one ${field} per line`}
          onChange={(e) => {
            const values = e.target.value.split("\n").map((l) => l.trim());
            const kept = e.target.value.trim() ? values.filter((v, i) => v || i === values.length - 1) : [];
            set(kept.length ? kept.map((v) => ({ [field]: v })) : spec.connection ? undefined : []);
          }}
        />
      </Field>
    );
  }
  return (
    <Field label="Targets" description="A JSON array of objects; the target key picks each one's id." error={jsonError ?? undefined}>
      <div className="rounded-lg border border-border bg-panel">
        <CodeEditor
          value={json}
          language="json"
          minHeight="140px"
          maxHeight="360px"
          readOnly={!canWrite}
          onChange={(t) => {
            setJson(t);
            try {
              const v = JSON.parse(t);
              if (!Array.isArray(v)) throw new Error("Must be a JSON array");
              set(v);
              setJsonError(null);
            } catch (e) {
              setJsonError((e as Error).message);
            }
          }}
        />
      </div>
    </Field>
  );
}

function Lookup({ ctx }: { ctx: EditorContext }) {
  const { ns, spec, setSpec, canWrite } = ctx;
  const can = useCan("connections:read", ns);
  const connections = useResources("connections", ns, { enabled: can });
  const integrations = useIntegrations();
  const connection = spec.connection as string | undefined;
  const conn = connections.data?.find((c) => c.metadata.name === connection);
  const caps = useConnectionCapabilities(ns, connection);
  const resources = caps.data?.resources ?? integrations.data?.find((i) => i.key === conn?.spec.integration)?.resources;
  const resourceDef = resources?.find((r) => r.name === spec.resource);
  return (
    <div className="space-y-3.5">
      <div className="grid gap-3.5 sm:grid-cols-2">
        <Field label="Connection" hint="optional">
          <NativeSelect
            value={connection ?? ""}
            disabled={!canWrite}
            onChange={(e) => {
              const next: Item = { ...spec, connection: e.target.value || undefined, resource: undefined, filter: undefined };
              if (!e.target.value && next.items === undefined) next.items = [];
              setSpec(next);
            }}
          >
            <option value="">None: use the targets as written</option>
            {(connections.data ?? []).map((c) => (
              <option key={c.metadata.name} value={c.metadata.name}>
                {c.metadata.name} ({String(c.spec.integration)})
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Resource" required={!!connection} description={resourceDef?.description}>
          <NativeSelect
            value={(spec.resource as string) ?? ""}
            disabled={!canWrite || !resources}
            onChange={(e) => setSpec({ ...spec, resource: e.target.value || undefined, filter: undefined })}
          >
            <option value="">{resources ? "Choose a resource…" : "Pick a connection first"}</option>
            {(resources ?? []).map((r) => (
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
          <SchemaForm schema={resourceDef.filter_schema} value={spec.filter as Item | undefined} onChange={(f) => setSpec({ ...spec, filter: f })} />
        </div>
      )}
    </div>
  );
}

function Preview({ ctx }: { ctx: EditorContext }) {
  const { ns, spec } = ctx;
  const canUse = useCan("connections:use", ns);
  const preview = useMutation({ mutationFn: () => previewTargetList(ns, spec) });
  const needsUse = !!spec.connection && !canUse;
  return (
    <Card>
      <CardHeader
        title="Preview"
        description={spec.connection ? "Runs the lookup now, so you can see which targets resolve and which are missing." : "The targets as campaigns will see them."}
        actions={
          <Button size="sm" icon={<Eye className="size-4" />} loading={preview.isPending} disabled={needsUse} onClick={() => preview.mutate()}>
            Preview
          </Button>
        }
      />
      <div className="space-y-3 p-4">
        {needsUse && <p className="text-xs text-muted">Previewing a lookup needs permission to use the connection.</p>}
        <ErrorList error={preview.error} />
        {preview.data ? <TargetListResult result={preview.data} /> : !preview.error && <p className="text-xs text-faint">Not run yet.</p>}
      </div>
    </Card>
  );
}

export const targetListExtension: EditorExtension = {
  sections: (ctx) => (
    <>
      <Card>
        <CardHeader
          title="Targets"
          description="List targets by name, look them up through a connection so each carries its details, or both. Campaigns use a list with targets: {list: name}; trigger filters read its keys as lists['name']."
        />
        <div className="space-y-4 p-4">
          <ItemsEditor key={keyField(ctx.spec.key) ?? "json"} ctx={ctx} />
          <Lookup ctx={ctx} />
        </div>
      </Card>
      <Preview ctx={ctx} />
    </>
  ),
};
