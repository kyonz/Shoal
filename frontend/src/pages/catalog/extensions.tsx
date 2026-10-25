/** Kind-specific widgets and sections for the generic editor. */
import { useQuery } from "@tanstack/react-query";
import { FilePlus2, KeyRound, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { rawJson, type Schemas } from "@/api/client";
import { useResources } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { nsPath } from "@/lib/ns";
import { Button, Card, CardHeader, CodeEditor, EmptyState, Field, Input, NativeSelect } from "@/components/ui";
import { JsonField, KeyValueInput, SchemaForm } from "@/components/forms/SchemaForm";
import type { EditorContext, EditorExtension } from "./ResourceEditorPage";
import { looksLikeCredential } from "./kinds";
import { Combo, MarkdownEditor, ModelCombo, ResourceMultiPicker, ResourcePicker, ToolsPicker, useProviders, useSandboxBackends } from "./widgets";

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

/** Gateway credentials live in the tenant's shared namespace. */
export const SHARED_NS = "shared";

function SharedSecretPicker({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  return (
    <div>
      <SecretPicker ns={SHARED_NS} value={value} onChange={onChange} />
      <Link to={nsPath(SHARED_NS, "secrets")} className="mt-1 inline-block text-xs text-accent hover:underline">
        Manage shared secrets
      </Link>
    </div>
  );
}

// --------------------------------------------------------------------------- agents

export const agentExtension: EditorExtension = {
  widgets: ({ ns, name }) => ({
    model: ({ value, onChange }) => <ModelCombo value={value} onChange={onChange} />,
    provider: ({ value, onChange }) => <AgentProviderSelect ns={ns} value={value} onChange={onChange} />,
    runtime: ({ value, onChange }) => (
      <NativeSelect value={(value as string) ?? "tool-loop"} onChange={(e) => onChange(e.target.value === "tool-loop" ? undefined : e.target.value)}>
        <option value="tool-loop">tool-loop — Shoal's own model ↔ tools loop</option>
        <option value="cli">cli — run a coding agent CLI in the sandbox</option>
        <option value="a2a">a2a — delegate to a remote A2A agent</option>
      </NativeSelect>
    ),
    instructions: ({ value, onChange }) => <MarkdownEditor value={value} onChange={onChange} minHeight="300px" />,
    skills: ({ value, onChange }) => <ResourceMultiPicker plural="skills" ns={ns} value={value} onChange={onChange} />,
    tools: ({ value, onChange }) => <ToolsPicker ns={ns} value={value} onChange={onChange} />,
    subagents: ({ value, onChange }) => <ResourceMultiPicker plural="agents" ns={ns} value={value} onChange={onChange} exclude={name} />,
    sandbox: ({ value, onChange }) => <ResourcePicker plural="sandbox-profiles" ns={ns} value={value} onChange={onChange} placeholder="Workflow default" />,
  }),
};

/** An agent's provider: a tenant gateway, or a built-in provider plugin used directly. */
function AgentProviderSelect({ ns, value, onChange }: { ns: string; value: unknown; onChange: (v: unknown) => void }) {
  const providers = useProviders();
  const gateways = useResources("gateways", ns);
  const v = (value as string) ?? "anthropic";
  const gw = gateways.data ?? [];
  const gwNames = new Set(gw.map((g) => g.metadata.name));
  // A gateway with a provider's name shadows it, so the built-in is not offered separately.
  const builtins = (providers.data ?? []).filter((p) => !gwNames.has(p.name));
  const known = gwNames.has(v) || builtins.some((p) => p.name === v);
  const current = gw.find((g) => g.metadata.name === v);
  return (
    <div>
      <NativeSelect value={v} onChange={(e) => onChange(e.target.value)}>
        {!known && <option value={v}>{v} (not found)</option>}
        {gw.length > 0 && (
          <optgroup label="Gateways">
            {gw.map((g) => (
              <option key={g.metadata.name} value={g.metadata.name}>
                {g.metadata.name}
                {g.metadata.description ? ` — ${g.metadata.description}` : ""}
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label="Built-in providers">
          {builtins.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
              {p.description ? ` — ${p.description}` : ""}
            </option>
          ))}
        </optgroup>
      </NativeSelect>
      <p className="mt-1 text-xs text-faint">
        {current ? (
          <>
            Through the <span className="font-mono">{v}</span> gateway ({String(current.spec?.type ?? "anthropic")}).{" "}
          </>
        ) : known ? (
          "Calls the provider directly with the server's credentials. "
        ) : null}
        Gateways are managed in the{" "}
        <Link to="/admin/gateways" className="text-accent hover:underline">
          admin console
        </Link>
        .
      </p>
    </div>
  );
}

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
  sections: ({ spec, setSpec, canWrite }) => (
    <>
      <NetworkSupportWarning spec={spec} />
      <BackendConfig spec={spec} setSpec={setSpec} readOnly={!canWrite} />
    </>
  ),
};

/** Mirrors the engine: a backend that cannot enforce the network policy refuses to start the sandbox. */
function NetworkSupportWarning({ spec }: { spec: Record<string, unknown> }) {
  const { data } = useSandboxBackends();
  const backend = (spec.backend as string) ?? "local";
  const controls = data?.find((b) => b.name === backend)?.network_controls;
  const network = (spec.network as { egress?: string; allow_hosts?: string[] } | undefined) ?? {};
  if (!controls) return null;
  const hosts = (network.allow_hosts ?? []).length > 0;
  const missing = hosts && !controls.includes("allow_hosts") ? "restrict egress to allowed hosts" : !hosts && network.egress === "deny" && !controls.includes("deny") ? "block egress" : null;
  if (!missing) return null;
  return (
    <div className="flex items-center gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
      <TriangleAlert className="size-4 shrink-0" />
      <span>
        The <span className="font-mono">{backend}</span> backend can't {missing}, so steps using this profile will fail rather than run with open network
        access. Pick a backend that supports it, or change the network policy.
      </span>
    </div>
  );
}

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
  // A backend whose schema has no fields takes no settings: nothing to show unless some are set already.
  if (schema && !Object.keys(schema.properties ?? {}).length && !Object.keys(config).length) return null;
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

// --------------------------------------------------------------------------- cloud identities

interface TrustPolicy {
  identity: string;
  subject: string;
  trust: string;
  role_arn: string;
  policy: Record<string, unknown>;
  setup: string[];
  notes: string[];
}

function TrustPolicyTab({ ns, name, version }: { ns: string; name: string; version: number | undefined }) {
  const { data, error } = useQuery({
    queryKey: ["cloud-identities", "trust-policy", ns, name, version],
    queryFn: () => rawJson<TrustPolicy>(`/v1/namespaces/${ns}/cloud-identities/${name}/trust-policy`),
  });
  if (error) return <EmptyState className="py-10" title="No trust policy" description={String(error)} />;
  if (!data) return null;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Trust policy"
          description={`Set this on ${data.role_arn} so Shoal can assume it for ${data.identity}. Sessions are tagged with the run and its initiator.`}
        />
        <div className="p-4">
          <CodeEditor value={JSON.stringify(data.policy, null, 2)} language="json" readOnly minHeight="220px" maxHeight="480px" onChange={() => undefined} />
        </div>
      </Card>
      <Card>
        <CardHeader title="Setup" description={`Subject / ExternalId: ${data.subject}`} />
        <div className="space-y-2 p-4 text-sm">
          {data.setup.map((line) => (
            <pre key={line} className="overflow-x-auto rounded bg-muted p-2 font-mono text-xs">{line}</pre>
          ))}
          {data.notes.map((note) => (
            <p key={note} className="text-muted-foreground">{note}</p>
          ))}
        </div>
      </Card>
    </div>
  );
}

export const cloudIdentityExtension: EditorExtension = {
  tabs: (ctx) => [
    {
      value: "trust",
      label: "Trust policy",
      content: <TrustPolicyTab ns={ctx.ns} name={ctx.name!} version={ctx.resource?.metadata.version} />,
    },
  ],
};

// --------------------------------------------------------------------------- gateways

export const gatewayExtension: EditorExtension = {
  widgets: () => ({
    type: ({ value, onChange }) => <ProviderSelect value={value} onChange={onChange} />,
    api_key_secret: ({ value, onChange }) => <SharedSecretPicker value={value} onChange={onChange} />,
  }),
  sections: (ctx) => <GatewayHeaders ctx={ctx} />,
};

function GatewayHeaders({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite, errors } = ctx;
  const headers = (spec.headers as Record<string, string>) ?? {};
  const secretHeaders = (spec.secret_headers as Record<string, string>) ?? {};
  // KeyValueInput keeps its own rows; remount both lists when headers move between them.
  const [generation, setGeneration] = useState(0);
  const set = (field: "headers" | "secret_headers", v: Record<string, string>, next = { ...spec }) => {
    if (Object.keys(v).length) next[field] = v;
    else delete next[field];
    return next;
  };
  const flagged = Object.keys(headers).filter(looksLikeCredential);
  const move = () => {
    const plain = Object.fromEntries(Object.entries(headers).filter(([k]) => !looksLikeCredential(k)));
    const secret = { ...secretHeaders, ...Object.fromEntries(flagged.map((k) => [k, secretHeaders[k] ?? ""])) };
    setSpec(set("secret_headers", secret, set("headers", plain)));
    setGeneration((g) => g + 1);
  };
  return (
    <Card>
      <CardHeader title="Headers" description="Sent with every model request through this gateway." />
      <fieldset disabled={!canWrite} className="space-y-4 p-4">
        <Field label="Secret headers" description="Header name and the shared secret that holds its value, e.g. x-portkey-api-key → portkey-key." error={errors.secret_headers}>
          <KeyValueInput
            key={`s${generation}`}
            value={secretHeaders}
            onChange={(v) => setSpec(set("secret_headers", v))}
            keyPlaceholder="x-api-key"
            valueWidget={(v, setV) => <SecretPicker ns={SHARED_NS} value={v || undefined} onChange={(nv) => setV((nv as string) ?? "")} />}
          />
        </Field>
        <Field label="Plain headers" description="Non-secret values only, such as a team or routing tag." error={errors.headers}>
          <KeyValueInput key={`p${generation}`} value={headers} onChange={(v) => setSpec(set("headers", v))} keyPlaceholder="x-team" valuePlaceholder="platform" />
        </Field>
        {flagged.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
            <TriangleAlert className="size-4 shrink-0" />
            <span className="min-w-0 flex-1">
              <span className="font-mono">{flagged.join(", ")}</span> {flagged.length === 1 ? "looks" : "look"} like a credential. Plain headers can't carry
              credentials; keep the value in a shared secret instead.
            </span>
            <Button size="sm" variant="secondary" icon={<KeyRound className="size-3.5" />} onClick={move}>
              Move to secret headers
            </Button>
          </div>
        )}
      </fieldset>
    </Card>
  );
}

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
