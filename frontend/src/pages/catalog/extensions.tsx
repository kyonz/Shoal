/** Kind-specific widgets and sections for the generic editor. */
import { useQuery } from "@tanstack/react-query";
import { FilePlus2, Trash2 } from "lucide-react";
import { useState } from "react";
import { rawJson, type Schemas } from "@/api/client";
import { useCan } from "@/lib/auth";
import { Button, Card, CardHeader, CodeEditor, EmptyState, Input, NativeSelect } from "@/components/ui";
import { JsonField, SchemaForm } from "@/components/forms/SchemaForm";
import type { EditorExtension } from "./ResourceEditorPage";
import { Combo, MarkdownEditor, ModelCombo, ResourceMultiPicker, ResourcePicker, useProviders, useSandboxBackends } from "./widgets";

export function useSecrets(ns: string) {
  return useQuery({
    queryKey: ["secrets", ns],
    queryFn: () => rawJson<Schemas["SecretOut"][]>(`/v1/namespaces/${ns}/secrets`),
    retry: false,
  });
}

export function SecretPicker({ ns, value, onChange }: { ns: string; value: unknown; onChange: (v: unknown) => void }) {
  const canRead = useCan("secrets:read", ns);
  const { data } = useSecrets(ns);
  if (!canRead) return <Input mono value={(value as string) ?? ""} placeholder="secret name" onChange={(e) => onChange(e.target.value || undefined)} />;
  return (
    <Combo
      mono
      value={value as string | undefined}
      onChange={onChange}
      placeholder="secret name"
      options={(data ?? []).map((s) => ({ value: s.name }))}
    />
  );
}

// --------------------------------------------------------------------------- agents

export const agentExtension: EditorExtension = {
  widgets: ({ ns, name }) => ({
    model: ({ value, onChange }) => <ModelCombo value={value} onChange={onChange} />,
    provider: ({ value, onChange }) => <ProviderSelect value={value} onChange={onChange} />,
    runtime: ({ value, onChange }) => (
      <NativeSelect value={(value as string) ?? "tool-loop"} onChange={(e) => onChange(e.target.value === "tool-loop" ? undefined : e.target.value)}>
        <option value="tool-loop">tool-loop — Shoal's own model ↔ tools loop</option>
        <option value="cli">cli — run a coding agent CLI in the sandbox</option>
        <option value="a2a">a2a — delegate to a remote A2A agent</option>
      </NativeSelect>
    ),
    instructions: ({ value, onChange }) => <MarkdownEditor value={value} onChange={onChange} minHeight="300px" />,
    skills: ({ value, onChange }) => <ResourceMultiPicker plural="skills" ns={ns} value={value} onChange={onChange} />,
    subagents: ({ value, onChange }) => <ResourceMultiPicker plural="agents" ns={ns} value={value} onChange={onChange} exclude={name} />,
    sandbox: ({ value, onChange }) => <ResourcePicker plural="sandbox-profiles" ns={ns} value={value} onChange={onChange} placeholder="Workflow default" />,
  }),
};

function ProviderSelect({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const { data } = useProviders();
  const v = (value as string) ?? "anthropic";
  return (
    <NativeSelect value={v} onChange={(e) => onChange(e.target.value)}>
      {!(data ?? []).some((p) => p.name === v) && <option value={v}>{v}</option>}
      {(data ?? []).map((p) => (
        <option key={p.name} value={p.name}>
          {p.name}
          {p.description ? ` — ${p.description}` : ""}
        </option>
      ))}
    </NativeSelect>
  );
}

// --------------------------------------------------------------------------- skills

export const skillExtension: EditorExtension = {
  widgets: () => ({
    instructions: ({ value, onChange }) => <MarkdownEditor value={value} onChange={onChange} minHeight="340px" />,
  }),
  sections: ({ spec, setSpec, canWrite }) => (
    <SkillFiles files={(spec.files as Record<string, string>) ?? {}} readOnly={!canWrite} onChange={(files) => {
      const next = { ...spec };
      if (Object.keys(files).length) next.files = files;
      else delete next.files;
      setSpec(next);
    }} />
  ),
};

function SkillFiles({ files, onChange, readOnly }: { files: Record<string, string>; onChange: (f: Record<string, string>) => void; readOnly: boolean }) {
  const paths = Object.keys(files).sort();
  const [active, setActive] = useState<string | null>(paths[0] ?? null);
  const [newPath, setNewPath] = useState("");
  const current = active && active in files ? active : paths[0] ?? null;
  const add = () => {
    const p = newPath.trim().replace(/^\/+/, "");
    if (!p || p in files) return;
    onChange({ ...files, [p]: "" });
    setActive(p);
    setNewPath("");
  };
  return (
    <Card>
      <CardHeader title="Files" description="Scripts and resources mounted next to SKILL.md in the sandbox." />
      <div className="grid min-h-48 grid-cols-[220px_minmax(0,1fr)]">
        <div className="border-r border-border p-2">
          {paths.map((p) => (
            <div key={p} className={`group flex items-center rounded-md ${p === current ? "bg-accent-soft text-accent" : "hover:bg-subtle"}`}>
              <button className="min-w-0 flex-1 truncate px-2 py-1.5 text-left font-mono text-xs" onClick={() => setActive(p)}>
                {p}
              </button>
              {!readOnly && (
                <button
                  aria-label={`Delete ${p}`}
                  className="mr-1 rounded p-1 text-faint opacity-0 group-hover:opacity-100 hover:text-bad"
                  onClick={() => {
                    const next = { ...files };
                    delete next[p];
                    onChange(next);
                  }}
                >
                  <Trash2 className="size-3" />
                </button>
              )}
            </div>
          ))}
          {!readOnly && (
            <div className="mt-2 flex gap-1">
              <Input mono className="h-7 text-[11px]" value={newPath} placeholder="scripts/run.sh" onChange={(e) => setNewPath(e.target.value)} onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), add())} />
              <Button size="icon-sm" variant="secondary" onClick={add} aria-label="Add file">
                <FilePlus2 className="size-3.5" />
              </Button>
            </div>
          )}
        </div>
        <div className="min-w-0">
          {current ? (
            <CodeEditor key={current} value={files[current] ?? ""} language={current.endsWith(".json") ? "json" : current.endsWith(".yaml") || current.endsWith(".yml") ? "yaml" : "text"} readOnly={readOnly} minHeight="220px" maxHeight="480px" onChange={(t) => onChange({ ...files, [current]: t })} />
          ) : (
            <EmptyState className="py-10" title="No files" description="Add helper scripts or reference material the agent can use with this skill." />
          )}
        </div>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------- sandbox profiles

export const sandboxExtension: EditorExtension = {
  widgets: () => ({
    backend: ({ value, onChange }) => <BackendSelect value={value} onChange={onChange} />,
  }),
  sections: ({ spec, setSpec, canWrite }) => <BackendConfig spec={spec} setSpec={setSpec} readOnly={!canWrite} />,
};

function BackendSelect({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const { data } = useSandboxBackends();
  const v = (value as string) ?? "local";
  const current = data?.find((b) => b.name === v);
  return (
    <div>
      <NativeSelect value={v} onChange={(e) => onChange(e.target.value)}>
        {!current && <option value={v}>{v}</option>}
        {(data ?? []).map((b) => (
          <option key={b.name} value={b.name}>
            {b.name}
          </option>
        ))}
      </NativeSelect>
      {current?.description && <p className="mt-1 text-xs text-faint">{current.description}</p>}
    </div>
  );
}

function BackendConfig({ spec, setSpec, readOnly }: { spec: Record<string, unknown>; setSpec: (s: Record<string, unknown>) => void; readOnly: boolean }) {
  const { data } = useSandboxBackends();
  const backend = (spec.backend as string) ?? "local";
  const schema = data?.find((b) => b.name === backend)?.config_schema;
  const config = (spec.config as Record<string, unknown>) ?? {};
  const set = (c: unknown) => {
    const next = { ...spec };
    if (c && typeof c === "object" && Object.keys(c).length) next.config = c;
    else delete next.config;
    setSpec(next);
  };
  return (
    <Card>
      <CardHeader title={`${backend} settings`} description="Backend-specific configuration." />
      <fieldset disabled={readOnly} className="p-4">
        {schema ? (
          <SchemaForm schema={schema} value={config} onChange={set} />
        ) : (
          <>
            <p className="mb-2 text-xs text-faint">This backend does not publish a settings schema; enter JSON.</p>
            <JsonField key={backend} value={spec.config} onChange={set} />
          </>
        )}
      </fieldset>
    </Card>
  );
}

// --------------------------------------------------------------------------- gateways

export const gatewayExtension: EditorExtension = {
  widgets: ({ ns }) => ({
    type: ({ value, onChange }) => <ProviderSelect value={value} onChange={onChange} />,
    api_key_secret: ({ value, onChange }) => <SecretPicker ns={ns} value={value} onChange={onChange} />,
  }),
};

// --------------------------------------------------------------------------- model policies

function useDecisionProviders() {
  return useQuery({
    queryKey: ["catalog", "decision-providers"],
    queryFn: () => rawJson<{ name: string; description: string; default: boolean }[]>("/v1/decision-providers"),
    staleTime: 300_000,
  });
}

export function DecisionProviderSelect({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const { data } = useDecisionProviders();
  return (
    <NativeSelect value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value || undefined)}>
      <option value="">Server default{data?.find((d) => d.default) ? ` (${data.find((d) => d.default)!.name})` : ""}</option>
      {(data ?? []).map((d) => (
        <option key={d.name} value={d.name}>
          {d.name}
        </option>
      ))}
    </NativeSelect>
  );
}

export const modelPolicyExtension: EditorExtension = {
  widgets: () => ({
    decision_provider: ({ value, onChange }) => <DecisionProviderSelect value={value} onChange={onChange} />,
  }),
};
