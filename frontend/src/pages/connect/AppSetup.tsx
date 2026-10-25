import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, Circle, Copy, Download, ExternalLink, KeyRound, PlugZap, XCircle } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { ApiError, rawJson } from "@/api/client";
import { usePutResource, useResource } from "@/api/hooks";
import { Badge, Button, Card, CardHeader, CodeEditor, Field, Input, Page, PageHeader, Skeleton, Switch } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import { Crumb, IntegrationMark, usePutSecret } from "./shared";

type Permission = { name: string; why: string };
type Feature = {
  key: string;
  title: string;
  description: string;
  scopes: Permission[];
  events: Permission[];
  required: boolean;
  enabled: boolean;
  used_by: string[];
};
type Guide = {
  integration: string;
  url: string;
  instructions: string;
  features: Feature[];
  custom: boolean;
  scopes: string[];
  events: string[];
  secrets: Record<string, { secret: string | null; set: boolean }>;
  config: Record<string, unknown>;
  install: boolean;
  manifest?: Record<string, unknown>;
  manifest_yaml?: string;
  create_url?: string;
};
type TestResult = { ok?: boolean; error?: string; warning?: string; team?: string; bot?: string; missing_scopes?: string[] };

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

/** Guided app setup (Slack): pick what the app does, see the permissions that needs and why, create the app from the
 * generated manifest, add its credentials, install it and check it. */
export default function AppSetup() {
  const ns = useNs();
  const { name = "" } = useParams();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const resource = useResource("connections", ns, name);
  const put = usePutResource("connections", ns);
  const [draft, setDraft] = useState<string[] | null>(null);
  const preview = draft ? `?features=${encodeURIComponent(draft.join(",") || ",")}` : "";
  const guide = useQuery({
    queryKey: ["connection-setup", ns, name, preview],
    queryFn: () => rawJson<Guide>(`/v1/namespaces/${ns}/connections/${name}/setup${preview}`),
    placeholderData: keepPreviousData,
  });
  const saved = useQuery({
    queryKey: ["connection-setup", ns, name, ""],
    queryFn: () => rawJson<Guide>(`/v1/namespaces/${ns}/connections/${name}/setup`),
  });
  const g = guide.data;
  const savedKeys = useMemo(() => (saved.data?.features ?? []).filter((f) => f.enabled).map((f) => f.key), [saved.data]);
  const enabled = draft ?? savedKeys;
  const dirty = draft !== null && !sameSet(draft, savedKeys);
  const writable = can("connections:write", ns);

  const refresh = () => qc.invalidateQueries({ queryKey: ["connection-setup", ns, name] });
  const saveSpec = async (change: (spec: Record<string, unknown>) => Record<string, unknown>, message: string) => {
    const res = resource.data;
    if (!res) return;
    await put.mutateAsync({ name, body: { metadata: { description: res.metadata.description ?? null }, spec: change({ ...res.spec }) } });
    await refresh();
    toast.success(message);
  };
  const saveFeatures = async () => {
    const all = (saved.data?.features ?? []).map((f) => f.key);
    const value = draft && sameSet(draft, all) ? null : draft;
    try {
      await saveSpec((spec) => {
        const config = { ...((spec.config as Record<string, unknown>) ?? {}) };
        if (value) config.features = value.filter((k) => !(saved.data?.features ?? []).find((f) => f.key === k)?.required);
        else delete config.features;
        return { ...spec, config };
      }, "Saved what the app does");
      setDraft(null);
    } catch (e) {
      toast.error(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const toggle = (key: string, on: boolean) => setDraft((enabled.includes(key) ? enabled.filter((k) => k !== key) : enabled).concat(on ? [key] : []));
  const onlyUsed = () => setDraft((g?.features ?? []).filter((f) => f.required || f.used_by.length).map((f) => f.key));
  const everything = () => setDraft((g?.features ?? []).map((f) => f.key));

  if (guide.isLoading || resource.isLoading) {
    return (
      <Page>
        <Skeleton className="h-96" />
      </Page>
    );
  }
  if (!g) {
    return (
      <Page>
        <p className="text-sm text-bad">{guide.error instanceof ApiError ? guide.error.detail : "Could not load the setup guide."}</p>
      </Page>
    );
  }
  const secrets = g.secrets;
  const credentialsDone = !!secrets.signing_secret?.set;
  const installed = !!secrets.bot_token?.set;

  return (
    <Page>
      <PageHeader
        breadcrumb={
          <>
            <Crumb to={nsPath(ns, "connections")}>Connections</Crumb>
            <Crumb to={nsPath(ns, "connections", name)}>{name}</Crumb>
          </>
        }
        icon={<IntegrationMark integrationKey={g.integration} />}
        title="Set up the Slack app"
        description="Choose what the app does, create it in Slack from the generated manifest, then install and check it."
      />
      {params.get("installed") && (
        <div role="status" className="mb-4 flex items-center gap-2 rounded-lg border border-ok/25 bg-ok-soft px-3 py-2 text-[13px] text-ok">
          <CheckCircle2 className="size-4 shrink-0" /> App installed; its bot token is stored as a secret. Check it below.
        </div>
      )}
      <div className="max-w-4xl space-y-4">
        <Step n={1} done={!dirty && savedKeys.length > 0} title="Choose what the app does" description="Each feature asks Slack for only the permissions it needs. Features your triggers and workflows use are marked.">
          <div className="space-y-2">
            {g.features.map((f) => (
              <FeatureRow key={f.key} feature={f} on={f.required || enabled.includes(f.key)} disabled={!writable || f.required} onChange={(on) => toggle(f.key, on)} />
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {writable && (
              <>
                <Button size="sm" onClick={onlyUsed}>Only what I use</Button>
                <Button size="sm" onClick={everything}>Everything</Button>
                <Button size="sm" variant="primary" disabled={!dirty} loading={put.isPending} onClick={saveFeatures}>Save</Button>
              </>
            )}
            <span className="text-xs text-muted">
              {g.scopes.length} permissions, {g.events.length} event subscriptions
              {dirty && " · unsaved: save so the install asks for the same permissions"}
            </span>
          </div>
        </Step>

        <Step n={2} title="Create the app in Slack" description="The manifest already points events, clicks and the slash command at this connection.">
          <ManifestPanel guide={g} />
          <p className="mt-3 text-xs leading-relaxed text-muted">
            Already have the app? Open it at api.slack.com/apps, paste this into <b>App Manifest</b>, save, and reinstall it when Slack asks for new permissions.
          </p>
        </Step>

        <Step n={3} done={credentialsDone} title="Add the app's credentials" description="From the app's Basic Information page. They are stored as secrets and never shown again.">
          <Credentials ns={ns} name={name} guide={g} writable={writable && can("secrets:write", ns)} saveSpec={saveSpec} />
        </Step>

        <Step n={4} done={installed} title="Install it to your workspace" description="Add to Slack stores the bot token for you. You can paste a bot token instead.">
          <Install ns={ns} name={name} guide={g} writable={writable && can("secrets:write", ns)} saveSpec={saveSpec} />
        </Step>

        <Step n={5} title="Check it works" description="Tests the token and that Slack granted every permission the chosen features need.">
          <Verify ns={ns} name={name} />
          <p className="mt-3 text-xs text-muted">
            Then invite the app to its channels (<code>/invite @{String(g.config.app_name ?? "Shoal")}</code>) and{" "}
            <Link className="text-accent hover:underline" to={`${nsPath(ns, "triggers", "new")}?connection=${encodeURIComponent(name)}&event=message`}>wire a channel to a workflow</Link>.
          </p>
        </Step>
      </div>
    </Page>
  );
}

function Step({ n, title, description, done, children }: { n: number; title: string; description: string; done?: boolean; children: ReactNode }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <span className={cn("flex size-6 items-center justify-center rounded-full text-xs font-semibold", done ? "bg-ok-soft text-ok" : "bg-accent-soft text-accent")}>
              {done ? <CheckCircle2 className="size-4" /> : n}
            </span>
            {title}
          </span>
        }
        description={description}
      />
      <div className="p-4">{children}</div>
    </Card>
  );
}

function FeatureRow({ feature: f, on, disabled, onChange }: { feature: Feature; on: boolean; disabled: boolean; onChange: (on: boolean) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={cn("rounded-lg border px-3 py-2.5", on ? "border-accent/40 bg-accent-soft/30" : "border-border")}>
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          <Switch checked={on} disabled={disabled} onCheckedChange={onChange} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5 text-[13px] font-medium">
            {f.title}
            {f.required && <Badge tone="neutral">Always on</Badge>}
            {f.used_by.map((u) => (
              <Badge key={u} tone="info">Used by {u}</Badge>
            ))}
            {!on && f.used_by.length > 0 && <Badge tone="warn">Needed</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted">{f.description}</p>
          <button type="button" className="mt-1 inline-flex items-center gap-1 text-xs text-accent hover:underline" onClick={() => setOpen(!open)} aria-expanded={open}>
            <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
            {f.scopes.length} permission{f.scopes.length === 1 ? "" : "s"}
            {f.events.length > 0 && `, ${f.events.length} event${f.events.length === 1 ? "" : "s"}`}
          </button>
          {open && (
            <table className="mt-2 w-full text-xs">
              <tbody>
                {[...f.scopes.map((p) => ({ ...p, kind: "Permission" })), ...f.events.map((p) => ({ ...p, kind: "Event" }))].map((p) => (
                  <tr key={p.kind + p.name} className="border-t border-border/60 align-top">
                    <td className="py-1.5 pr-3 whitespace-nowrap"><code className="font-mono">{p.name}</code></td>
                    <td className="py-1.5 pr-3 text-muted">{p.kind}</td>
                    <td className="py-1.5">{p.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

function ManifestPanel({ guide: g }: { guide: Guide }) {
  const [format, setFormat] = useState<"yaml" | "json">("yaml");
  const text = format === "yaml" ? g.manifest_yaml ?? "" : JSON.stringify(g.manifest, null, 2);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Manifest copied");
    } catch {
      toast.error("Could not copy; select the text instead");
    }
  };
  const download = () => {
    const blob = new Blob([text], { type: format === "yaml" ? "application/yaml" : "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `slack-app-manifest.${format === "yaml" ? "yaml" : "json"}`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-border p-0.5 text-xs" role="tablist">
          {(["yaml", "json"] as const).map((f) => (
            <button key={f} role="tab" aria-selected={format === f} className={cn("rounded-md px-2.5 py-1 font-medium uppercase", format === f ? "bg-accent-soft text-accent" : "text-muted")} onClick={() => setFormat(f)}>
              {f}
            </button>
          ))}
        </div>
        <Button size="sm" icon={<Copy className="size-3.5" />} onClick={copy}>Copy</Button>
        <Button size="sm" icon={<Download className="size-3.5" />} onClick={download}>Download</Button>
        {g.create_url && (
          <a href={g.create_url} target="_blank" rel="noreferrer" className="ml-auto">
            <Button size="sm" variant="primary" icon={<ExternalLink className="size-3.5" />}>Create app in Slack</Button>
          </a>
        )}
      </div>
      <CodeEditor value={text} language={format === "yaml" ? "yaml" : "json"} readOnly height="280px" />
    </div>
  );
}

type SaveSpec = (change: (spec: Record<string, unknown>) => Record<string, unknown>, message: string) => Promise<void>;

function SecretInput({ id, label, description, set, value, onChange }: { id: string; label: string; description: string; set: boolean; value: string; onChange: (v: string) => void }) {
  return (
    <Field label={<span className="flex items-center gap-1.5">{label} {set ? <Badge tone="ok">Saved</Badge> : <Badge tone="warn">Missing</Badge>}</span>} description={description}>
      <Input id={id} type="password" autoComplete="off" placeholder={set ? "Saved; paste to replace" : "Paste it here"} value={value} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

/** Store pasted values as secrets (reusing the names the connection already points at) and point the connection at them. */
function useStoreSecrets(ns: string, name: string, guide: Guide, saveSpec: SaveSpec) {
  const putSecret = usePutSecret(ns);
  return async (values: Record<string, string>, config: Record<string, unknown>, message: string) => {
    const names: Record<string, string> = {};
    for (const [field, value] of Object.entries(values)) {
      if (!value.trim()) continue;
      names[field] = guide.secrets[field]?.secret ?? `${name}-${field.replace(/_/g, "-")}`;
      await putSecret.mutateAsync({ name: names[field], value: value.trim() });
    }
    await saveSpec((spec) => ({
      ...spec,
      config: { ...((spec.config as Record<string, unknown>) ?? {}), ...config },
      secrets: { ...((spec.secrets as Record<string, string>) ?? {}), ...names },
    }), message);
  };
}

function Credentials({ ns, name, guide: g, writable, saveSpec }: { ns: string; name: string; guide: Guide; writable: boolean; saveSpec: SaveSpec }) {
  const [signing, setSigning] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [clientId, setClientId] = useState(String(g.config.client_id ?? ""));
  const [busy, setBusy] = useState(false);
  useEffect(() => setClientId(String(g.config.client_id ?? "")), [g.config.client_id]);
  const store = useStoreSecrets(ns, name, g, saveSpec);
  const save = async () => {
    setBusy(true);
    try {
      await store({ signing_secret: signing, client_secret: clientSecret }, clientId ? { client_id: clientId.trim() } : {}, "Credentials saved");
      setSigning("");
      setClientSecret("");
    } catch (e) {
      toast.error(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="sm:col-span-2">
        <SecretInput id="setup-signing" label="Signing secret" description="Lets Shoal check that clicks, events and commands really come from Slack." set={!!g.secrets.signing_secret?.set} value={signing} onChange={setSigning} />
      </div>
      <Field label="Client ID" description="Needed for Add to Slack.">
        <Input id="setup-client-id" value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="1234567890.1234567890" />
      </Field>
      <SecretInput id="setup-client-secret" label="Client secret" description="Needed for Add to Slack." set={!!g.secrets.client_secret?.set} value={clientSecret} onChange={setClientSecret} />
      {writable && (
        <div className="sm:col-span-2">
          <Button variant="primary" size="sm" icon={<KeyRound className="size-3.5" />} loading={busy} disabled={!signing && !clientSecret && clientId === String(g.config.client_id ?? "")} onClick={save}>
            Save credentials
          </Button>
        </div>
      )}
    </div>
  );
}

function Install({ ns, name, guide: g, writable, saveSpec }: { ns: string; name: string; guide: Guide; writable: boolean; saveSpec: SaveSpec }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const store = useStoreSecrets(ns, name, g, saveSpec);
  const install = useMutation({
    mutationFn: () => rawJson<{ authorize_url: string }>(`/v1/namespaces/${ns}/connections/${name}/install?then=setup`, { method: "POST" }),
    onSuccess: (r) => window.location.assign(r.authorize_url),
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
  const canInstall = g.install && !!g.config.client_id && !!g.secrets.client_secret?.set;
  const saveToken = async () => {
    setBusy(true);
    try {
      await store({ bot_token: token }, {}, "Bot token saved");
      setToken("");
    } catch (e) {
      toast.error(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {writable && (
          <Button variant="primary" size="sm" icon={<Download className="size-3.5" />} disabled={!canInstall} loading={install.isPending} onClick={() => install.mutate()}>
            Add to Slack
          </Button>
        )}
        <span className="flex items-center gap-1.5 text-xs text-muted">
          {g.secrets.bot_token?.set ? <CheckCircle2 className="size-3.5 text-ok" /> : <Circle className="size-3.5" />}
          {g.secrets.bot_token?.set ? "Installed: the bot token is saved" : canInstall ? "Not installed yet" : "Add the client ID and secret first, or paste a bot token"}
        </span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1">
          <Field label="Or paste a bot token" description="OAuth & Permissions → Bot User OAuth Token (xoxb-…).">
            <Input id="setup-bot-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="xoxb-…" />
          </Field>
        </div>
        {writable && (
          <Button size="sm" className="mb-5" disabled={!token.trim()} loading={busy} onClick={saveToken}>Save token</Button>
        )}
      </div>
    </div>
  );
}

function Verify({ ns, name }: { ns: string; name: string }) {
  const test = useMutation({
    mutationFn: () => rawJson<TestResult>(`/v1/namespaces/${ns}/connections/${name}/test`, { method: "POST" }),
  });
  const r = test.data;
  const failed = test.error || (r && (r.ok === false || r.error));
  return (
    <div className="space-y-3">
      <Button size="sm" icon={<PlugZap className="size-3.5" />} loading={test.isPending} onClick={() => test.mutate()}>Check the app</Button>
      {(r || test.error) && (
        <div className={cn("rounded-lg border p-3 text-xs", failed ? "border-bad/25 bg-bad-soft text-bad" : "border-ok/25 bg-ok-soft text-ok")}>
          <div className="flex items-center gap-1.5 font-medium">
            {failed ? <XCircle className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}
            {failed ? "Not ready yet" : `Connected to ${r?.team ?? "Slack"} as @${r?.bot ?? "bot"}`}
          </div>
          {test.error && <p className="mt-1">{test.error instanceof ApiError ? test.error.detail : String(test.error)}</p>}
          {r?.error && <p className="mt-1">{r.error}</p>}
          {r?.missing_scopes && r.missing_scopes.length > 0 && (
            <p className="mt-1">Missing: {r.missing_scopes.map((s) => <code key={s} className="mr-1 font-mono">{s}</code>)}</p>
          )}
          {r?.warning && <p className="mt-1 text-warn">{r.warning}</p>}
        </div>
      )}
    </div>
  );
}
