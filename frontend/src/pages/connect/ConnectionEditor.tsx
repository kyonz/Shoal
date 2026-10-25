import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, PlugZap, UserRound, Save, Trash2, Webhook, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";
import { ApiError, rawJson } from "@/api/client";
import { useDeleteResource, useIntegrations, usePutResource, useResource, type IntegrationCatalog } from "@/api/hooks";
import { SchemaForm, humanize, unwrapNullable } from "@/components/forms/SchemaForm";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CopyField,
  EmptyState,
  Field,
  Input,
  JsonBlock,
  Page,
  PageHeader,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  useConfirm,
} from "@/components/ui";
import { Expandable, SchemaTable } from "./IntegrationDetail";
import { ConnectLink, MyCredentialCard, WhoConnects, pickPersonal, userFields, type PersonalSpec } from "./PersonalCredentials";
import { ConnectionWebhooks } from "./TriggerWebhook";
import { Crumb, ErrorList, IntegrationMark, NAME_RE, SecretPicker, VersionsPanel, YamlPanel, useConnectionCapabilities, useYamlSync } from "./shared";

export default function ConnectionEditor() {
  const ns = useNs();
  const { name: routeName } = useParams();
  const isNew = !routeName;
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const { can } = useAuth();
  const canWrite = can("connections:write", ns);

  const existing = useResource("connections", ns, routeName);
  const integrations = useIntegrations();
  const put = usePutResource("connections", ns);
  const del = useDeleteResource("connections", ns);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [integration, setIntegration] = useState(params.get("integration") ?? "");
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [personal, setPersonal] = useState<PersonalSpec>({});
  const [tab, setTab] = useState("settings");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (existing.data && !loaded) {
      const s = existing.data.spec as unknown as { integration: string; config?: Record<string, unknown>; secrets?: Record<string, string> } & PersonalSpec;
      setName(existing.data.metadata.name);
      setDescription(existing.data.metadata.description ?? "");
      setIntegration(s.integration);
      setConfig(s.config ?? {});
      setSecrets(s.secrets ?? {});
      setPersonal(pickPersonal(s));
      setLoaded(true);
    }
  }, [existing.data, loaded]);

  const catalog: IntegrationCatalog | undefined = integrations.data?.find((i) => i.key === integration);
  const spec = useMemo(
    () => ({ integration, ...(Object.keys(config).length ? { config } : {}), ...(Object.keys(secrets).length ? { secrets } : {}), ...personal }),
    [integration, config, secrets, personal],
  );
  const yaml = useYamlSync("Connection", name, description, spec, (p) => {
    setDescription(p.description ?? "");
    const s = p.spec as { integration?: string; config?: Record<string, unknown>; secrets?: Record<string, string> } & PersonalSpec;
    if (s.integration) setIntegration(s.integration);
    setConfig(s.config ?? {});
    setSecrets(s.secrets ?? {});
    setPersonal(pickPersonal(s));
  });

  const nameError = isNew && name && !NAME_RE.test(name) ? "Lowercase letters, digits and dashes; start and end with a letter or digit." : undefined;

  const save = async () => {
    try {
      const res = await put.mutateAsync({ name, body: { metadata: { description: description || null }, spec } });
      toast.success(isNew ? `Connection ${name} created` : `Saved ${name} (v${res.metadata.version})`);
      if (isNew) navigate(nsPath(ns, "connections", name), { replace: true });
    } catch {
      /* shown inline */
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: `Delete connection ${name}?`,
      description: "Workflows, triggers and campaigns that use it will fail until it is recreated. Secrets are kept.",
      confirmLabel: "Delete",
      danger: true,
      typeToConfirm: name,
    });
    if (!ok) return;
    await del.mutateAsync(name);
    toast.success(`Deleted ${name}`);
    navigate(nsPath(ns, "connections"));
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
        <EmptyState title="Connection not found" description={existing.error.message} action={<Link to={nsPath(ns, "connections")}><Button>Back to connections</Button></Link>} />
      </Page>
    );

  if (isNew && !integration) return <PickIntegration integrations={integrations.data ?? []} onPick={setIntegration} loading={integrations.isLoading} />;

  const secretFields = catalog?.secret_fields ?? Object.keys(secrets);
  const personalFields = userFields({ ...personal, secrets });

  return (
    <Page>
      <PageHeader
        breadcrumb={<Crumb to={nsPath(ns, "connections")}>Connections</Crumb>}
        icon={<IntegrationMark integrationKey={integration} size="sm" />}
        title={isNew ? `New ${catalog?.title ?? integration} connection` : name}
        description={isNew ? catalog?.description : description || undefined}
        actions={
          <>
            {!isNew && canWrite && (
              <Button variant="ghost" icon={<Trash2 className="size-4" />} onClick={() => void remove()}>
                Delete
              </Button>
            )}
            {canWrite && (
              <Button variant="primary" icon={<Save className="size-4" />} loading={put.isPending} disabled={!name || !!nameError || !!yaml.error} onClick={() => void save()}>
                {isNew ? "Create connection" : "Save"}
              </Button>
            )}
          </>
        }
      />
      <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
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
              {!isNew && <TabsTrigger value="actions">Actions</TabsTrigger>}
              {!isNew && <TabsTrigger value="history">History</TabsTrigger>}
            </TabsList>
            <TabsContent value="settings" className="space-y-5">
              <ErrorList error={put.error} />
              <Card className="p-4">
                <div className="grid gap-3.5 sm:grid-cols-2">
                  <Field label="Name" required error={nameError} description={isNew ? "Referenced by workflows, e.g. connections: {github: <name>}." : undefined}>
                    <Input mono value={name} disabled={!isNew} autoFocus={isNew} onChange={(e) => setName(e.target.value)} placeholder={`${integration}-main`} />
                  </Field>
                  <Field label="Description">
                    <Input value={description} disabled={!canWrite} onChange={(e) => setDescription(e.target.value)} placeholder="What this connection is for" />
                  </Field>
                </div>
              </Card>
              <Card>
                <CardHeader
                  title={personal.credentials === "per_user" ? "Shared settings" : personal.credentials === "per_user_or_shared" ? "Shared credential (fallback)" : "Credentials"}
                  description={
                    personal.credentials === "per_user"
                      ? "Each person supplies their own credential. Secret fields that are not theirs still come from namespace secrets."
                      : "Secret fields point at namespace secrets, so values never appear in the resource."
                  }
                />
                <div className="space-y-3.5 p-4">
                  {secretFields.length ? (
                    secretFields.map((f) =>
                      personal.credentials === "per_user" && personalFields.includes(f) ? (
                        <Field key={f} label={humanize(f)}>
                          <div className="flex h-8 items-center gap-2 rounded-lg border border-dashed border-border px-2.5 text-xs text-muted">
                            <UserRound className="size-3.5" /> Each person supplies their own
                          </div>
                        </Field>
                      ) : (
                      <SecretPicker
                        key={f}
                        ns={ns}
                        field={f}
                        description={catalog ? unwrapNullable(catalog.connection_schema.properties?.[f] ?? {}, catalog.connection_schema).schema.description : undefined}
                        value={secrets[f]}
                        disabled={!canWrite}
                        onChange={(v) => {
                          const next = { ...secrets };
                          if (v) next[f] = v;
                          else delete next[f];
                          setSecrets(next);
                        }}
                      />
                      ),
                    )
                  ) : (
                    <p className="text-[13px] text-muted">This integration takes no secrets.</p>
                  )}
                </div>
              </Card>
              <WhoConnects ns={ns} value={personal} onChange={setPersonal} secretFields={secretFields} disabled={!canWrite} />
              <Card>
                <CardHeader title="Settings" description="Non-secret configuration, rendered from the integration's schema." />
                <div className="p-4">
                  {catalog ? (
                    <fieldset disabled={!canWrite}>
                      <SchemaForm schema={catalog.connection_schema} value={config} onChange={setConfig} hidden={catalog.secret_fields} collapseOptional />
                    </fieldset>
                  ) : (
                    <p className="text-[13px] text-muted">Integration “{integration}” is not installed on this server; edit the settings as YAML.</p>
                  )}
                </div>
              </Card>
            </TabsContent>
            <TabsContent value="yaml">
              <YamlPanel value={yaml.text} onChange={canWrite ? yaml.change : undefined} readOnly={!canWrite} error={yaml.error} />
              <ErrorList error={put.error} className="mt-3" />
            </TabsContent>
            {!isNew && (
              <TabsContent value="actions">
                <ConnectionActions ns={ns} name={name} catalog={catalog} />
              </TabsContent>
            )}
            {!isNew && (
              <TabsContent value="history">
                <VersionsPanel plural="connections" ns={ns} name={name} current={existing.data} />
              </TabsContent>
            )}
          </Tabs>
        </div>
        <aside className="space-y-4">
          {!isNew && (existing.data?.spec as PersonalSpec | undefined)?.credentials && (existing.data?.spec as PersonalSpec).credentials !== "shared" && (
            <MyCredentialCard ns={ns} connection={name} spec={existing.data!.spec as PersonalSpec} />
          )}
          {!isNew && <ConnectionSidebar ns={ns} name={name} webhookUrl={existing.data?.status?.webhook_url as string | undefined} />}
          {!isNew && <ConnectionWebhooks ns={ns} name={name} />}
        </aside>
      </div>
    </Page>
  );
}

function PickIntegration({ integrations, onPick, loading }: { integrations: IntegrationCatalog[]; onPick: (k: string) => void; loading: boolean }) {
  const ns = useNs();
  return (
    <Page>
      <PageHeader breadcrumb={<Crumb to={nsPath(ns, "connections")}>Connections</Crumb>} title="New connection" description="Pick the integration to connect." />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {loading && Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
        {integrations.map((i) => (
          <button key={i.key} onClick={() => onPick(i.key)} className="flex items-start gap-3 rounded-xl border border-border bg-panel p-4 text-left shadow-card transition hover:border-accent hover:shadow-pop">
            <IntegrationMark integrationKey={i.key} />
            <div className="min-w-0">
              <div className="text-[14px] font-semibold">{i.title}</div>
              <p className="mt-0.5 line-clamp-2 text-xs text-muted">{i.description}</p>
            </div>
          </button>
        ))}
      </div>
    </Page>
  );
}

function ConnectionSidebar({ ns, name, webhookUrl }: { ns: string; name: string; webhookUrl?: string }) {
  const { can } = useAuth();
  const webhook = useQuery({
    queryKey: ["connection-webhook", ns, name],
    queryFn: () => rawJson<{ url: string; instructions: string }>(`/v1/namespaces/${ns}/connections/${name}/webhook`),
    retry: false,
  });
  const test = useMutation({
    mutationFn: () => rawJson<Record<string, unknown>>(`/v1/namespaces/${ns}/connections/${name}/test`, { method: "POST" }),
  });
  const result = test.data;
  const ok = result ? result.ok !== false && !result.error : false;
  const url = webhook.data?.url ?? webhookUrl;
  return (
    <>
      {can("connections:use", ns) && (
        <Card>
          <CardHeader title="Test connection" description="Checks the credentials against the service." />
          <div className="space-y-3 p-4">
            <Button className="w-full justify-center" icon={<PlugZap className="size-4" />} loading={test.isPending} onClick={() => test.mutate()}>
              Run test
            </Button>
            {test.error ? (
              <div className="rounded-lg border border-bad/25 bg-bad-soft p-3 text-xs text-bad">
                <div className="flex items-center gap-1.5 font-medium">
                  <XCircle className="size-3.5" /> Test failed
                </div>
                <p className="mt-1">{test.error instanceof ApiError ? test.error.detail : String(test.error)}</p>
              </div>
            ) : result ? (
              <div className={cn("rounded-lg border p-3 text-xs", ok ? "border-ok/25 bg-ok-soft text-ok" : "border-bad/25 bg-bad-soft text-bad")}>
                <div className="flex items-center gap-1.5 font-medium">
                  {ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />} {ok ? "Connected" : "Test failed"}
                </div>
                {typeof result.error === "string" && <p className="mt-1">{result.error}</p>}
                <JsonBlock className="mt-2 bg-panel" value={result} maxHeight="200px" />
              </div>
            ) : null}
          </div>
        </Card>
      )}
      {url && (
        <Card>
          <CardHeader title={<span className="flex items-center gap-1.5"><Webhook className="size-3.5" /> Webhook URL</span>} description="Point the service's webhooks here so triggers fire. Triggers that register their own webhook get their own URL." />
          <div className="space-y-2 p-4">
            <CopyField value={url} />
            {webhook.data?.instructions && <p className="text-xs leading-relaxed break-all text-muted">{webhook.data.instructions}</p>}
            <Badge tone="info">Signature-verified with the webhook secret</Badge>
          </div>
        </Card>
      )}
    </>
  );
}

function ConnectionActions({ ns, name, catalog }: { ns: string; name: string; catalog?: IntegrationCatalog }) {
  const caps = useConnectionCapabilities(ns, name);
  if (caps.isLoading) return <Skeleton className="h-40" />;
  const actions = caps.data?.actions ?? catalog?.actions ?? [];
  const resources = caps.data?.resources ?? catalog?.resources ?? [];
  const key = caps.data?.integration ?? catalog?.key ?? "";
  return (
    <div className="space-y-5">
      {caps.error instanceof ApiError && caps.error.status === 428 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-[13px] text-info">
          <KeyRound className="size-4 shrink-0" />
          <span className="min-w-0 flex-1">This connection uses each person's own credential, so its tools are listed with yours.</span>
          <ConnectLink ns={ns} connection={name} />
        </div>
      ) : (
        <ErrorList error={caps.error} />
      )}
      {key === "mcp" ? (
        <p className="text-[13px] text-muted">
          The tools this MCP server offers right now. Give an agent all of them with <code className="font-mono">mcp:{name}</code>, or one with{" "}
          <code className="font-mono">mcp:{name}.&lt;tool&gt;</code>. Workflows can also call them as <code className="font-mono">action</code> steps.
        </p>
      ) : (
        <p className="text-[13px] text-muted">
          What workflows and agents can do through this connection. Use them as <code className="font-mono">action</code> steps or agent tools.
        </p>
      )}
      <Card>
        {actions.map((a) => (
          <Expandable key={a.name} title={key === "mcp" ? `mcp:${name}.${a.name}` : `${key}.${a.name}`} description={a.description} badges={a.mutating ? <Badge tone="warn">Mutating</Badge> : <Badge>Read-only</Badge>}>
            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Input</div>
                <SchemaTable schema={a.input_schema} empty="Takes no input." />
              </div>
              <div>
                <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Output</div>
                <SchemaTable schema={a.output_schema} empty="Returns free-form JSON." />
              </div>
            </div>
          </Expandable>
        ))}
        {!actions.length && <EmptyState title={key === "mcp" ? "No tools" : "No actions"} description={key === "mcp" ? "The server listed no tools, or the allow-list filters them all out." : "This connection's integration exposes no actions."} />}
      </Card>
      {resources.length > 0 && (
        <Card>
          <CardHeader title="Resources" description="Queryable through this connection; usable as campaign targets." />
          {resources.map((r) => (
            <Expandable key={r.name} title={r.name} description={r.description}>
              <SchemaTable schema={r.filter_schema} />
            </Expandable>
          ))}
        </Card>
      )}
    </div>
  );
}
