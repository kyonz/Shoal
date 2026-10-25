/**
 * /instance/orchestrator: what drives runs on every tenant. Instance admins connect a Temporal
 * server (credentials from a tenant's shared secrets), test it and switch new runs to it or back.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Database, PlugZap, RotateCcw, Workflow, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { dateTime } from "@/lib/format";
import { actorName } from "@/lib/actor";
import { Badge, Button, Card, CardHeader, ErrorNotice, Field, Input, NativeSelect, Page, PageHeader, Segmented, Skeleton, Switch, useConfirm } from "@/components/ui";
import { SHARED_NS } from "@/pages/catalog/extensions";
import { SecretPicker } from "@/pages/connect/shared";
import { KV } from "@/pages/admin/shared";
import { instanceKeys, useOrchestrator, useTenantSummaries, type OrchestratorStatus, type TemporalCheck, type TemporalSettings } from "./api";

type Choice = "builtin" | "temporal";
const detail = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));
const LABEL: Record<string, string> = { builtin: "Built-in", temporal: "Temporal" };
const SECRETS = [
  ["api_key_secret", "API key", "Temporal Cloud API key. Turns on TLS."],
  ["tls_cert_secret", "Client certificate", "mTLS client certificate (PEM)."],
  ["tls_key_secret", "Client key", "mTLS client key (PEM)."],
  ["tls_ca_secret", "Server CA", "CA for the server certificate (PEM), when it is not publicly trusted."],
] as const;
type SecretKey = (typeof SECRETS)[number][0];

/** The form starts from what is saved, or else from what the environment says. */
function initial(status: OrchestratorStatus, tenant: string | null): TemporalSettings {
  const env = status.environment;
  const saved = status.setting?.temporal;
  if (saved) return { ...saved, secrets_tenant: saved.secrets_tenant ?? tenant };
  return {
    address: env.temporal_address,
    namespace: env.temporal_namespace,
    task_queue: env.temporal_task_queue,
    tls: false,
    secrets_tenant: tenant,
  };
}

/** What the server accepts: empty secret names dropped, and no tenant when no secret is picked. */
function clean(t: TemporalSettings): TemporalSettings {
  const out: TemporalSettings = { address: t.address.trim(), namespace: t.namespace.trim() || "default", task_queue: t.task_queue.trim() || "shoal", tls: t.tls };
  const picked = SECRETS.filter(([k]) => t[k]?.trim());
  for (const [k] of picked) out[k] = t[k]!.trim();
  if (picked.length) out.secrets_tenant = t.secrets_tenant || null;
  return out;
}

export default function Orchestrator() {
  const status = useOrchestrator();
  return (
    <Page>
      <PageHeader
        icon={<Workflow className="size-5" />}
        title="Orchestrator"
        description="What schedules runs on every tenant: Shoal's built-in scheduler in the database, or your Temporal cluster. Switching only affects new runs."
      />
      {status.error ? <ErrorNotice error={status.error} /> : !status.data ? <Skeleton className="h-96" /> : <Editor status={status.data} />}
    </Page>
  );
}

function Editor({ status }: { status: OrchestratorStatus }) {
  const { me, instance } = useAuth();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const tenants = useTenantSummaries();
  const myTenant = me?.tenant?.slug ?? null;
  const [choice, setChoice] = useState<Choice>(status.orchestrator === "temporal" ? "temporal" : "builtin");
  const [temporal, setTemporal] = useState<TemporalSettings>(() => initial(status, myTenant));
  const [check, setCheck] = useState<TemporalCheck | null>(null);
  useEffect(() => {
    setChoice(status.orchestrator === "temporal" ? "temporal" : "builtin");
    setTemporal(initial(status, myTenant));
  }, [status, myTenant]);

  const saved = status.setting;
  const body = { orchestrator: choice, temporal: clean(temporal) };
  const dirty = !saved || JSON.stringify({ orchestrator: saved.orchestrator, temporal: saved.temporal && clean(saved.temporal) }) !== JSON.stringify(body);
  const set = (patch: Partial<TemporalSettings>) => {
    setTemporal({ ...temporal, ...patch });
    setCheck(null);
  };

  const test = useMutation({
    mutationFn: () => unwrap(client.POST("/v1/instance/orchestrator/test", { body: clean(temporal) })),
    onSuccess: setCheck,
    onError: (e) => toast.error(detail(e)),
  });
  const save = useMutation({
    mutationFn: () => unwrap(client.PUT("/v1/instance/orchestrator", { body })),
    onSuccess: (s) => {
      qc.setQueryData(instanceKeys.orchestrator, s);
      toast.success(s.orchestrator === status.orchestrator ? "Temporal connection saved" : `New runs now start on ${LABEL[s.orchestrator] ?? s.orchestrator}`);
    },
    onError: (e) => toast.error(detail(e)),
  });
  const reset = useMutation({
    mutationFn: () => unwrap(client.DELETE("/v1/instance/orchestrator")),
    onSuccess: (s) => {
      qc.setQueryData(instanceKeys.orchestrator, s);
      toast.success("Back to the environment's settings");
    },
    onError: (e) => toast.error(detail(e)),
  });

  const onSave = async () => {
    if (choice !== status.orchestrator) {
      const toTemporal = choice === "temporal";
      const ok = await confirm({
        title: toTemporal ? "Switch new runs to Temporal?" : "Switch new runs to the built-in orchestrator?",
        description: toTemporal
          ? "Every server picks this up within a few seconds. Runs already going keep the orchestrator they started on. Shoal checks the connection first."
          : "Every server picks this up within a few seconds. Runs already on Temporal keep running there while the connection stays saved.",
        confirmLabel: toTemporal ? "Switch to Temporal" : "Switch to built-in",
      });
      if (!ok) return;
    }
    save.mutate();
  };
  const onReset = async () => {
    const ok = await confirm({
      title: "Use the environment's settings again?",
      description: `Forgets what is saved here. New runs then start on ${LABEL[status.environment.orchestrator] ?? status.environment.orchestrator}, as SHOAL_ORCHESTRATOR says.`,
      confirmLabel: "Reset",
    });
    if (ok) reset.mutate();
  };

  const canWrite = instance.admin;
  const secretsHere = !!temporal.secrets_tenant && temporal.secrets_tenant === myTenant;
  const tenantOptions = tenants.data ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
      <div className="min-w-0 space-y-4">
        {status.error && (
          <div role="alert" className="flex items-start gap-2 rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>
              <b className="font-semibold">The saved setting can't be applied.</b> {status.error} Runs keep going with the previous setting. Fix it and save again.
            </span>
          </div>
        )}
        {!status.temporal_installed && (
          <div role="status" className="flex items-start gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>
              This server doesn't have the Temporal extra. Install it with <code className="font-mono">pip install 'shoal[temporal]'</code> (the container image already includes it).
            </span>
          </div>
        )}
        <Card>
          <CardHeader title="New runs start on" description="Each run keeps the orchestrator it started on, so you can switch without draining." />
          <div className="space-y-3 px-4 pb-4">
            <Segmented<Choice>
              value={choice}
              disabled={!canWrite}
              onChange={setChoice}
              options={[
                { value: "builtin", label: "Built-in", icon: <Database className="size-3.5" /> },
                { value: "temporal", label: "Temporal", icon: <Workflow className="size-3.5" /> },
              ]}
            />
            <p className="text-xs leading-snug text-faint">
              {choice === "builtin"
                ? "Shoal's database is the scheduler. Workers claim due runs with a lease. Needs nothing else."
                : "Each run is a Temporal workflow, and each pass over it is an activity. Waits, retries and wake-ups become durable Temporal timers and signals."}
            </p>
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Temporal connection"
            description="Saved connections keep driving their runs after you switch back. Use a task queue of its own for each Shoal installation sharing a namespace."
            actions={
              <Button size="sm" icon={<PlugZap className="size-3.5" />} loading={test.isPending} disabled={!canWrite || !temporal.address.trim()} onClick={() => test.mutate()}>
                Test connection
              </Button>
            }
          />
          <fieldset disabled={!canWrite} className="space-y-4 px-4 pb-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Address" required description="Frontend host:port" className="sm:col-span-3">
                <Input mono value={temporal.address} placeholder="temporal-frontend.temporal.svc:7233" onChange={(e) => set({ address: e.target.value })} />
              </Field>
              <Field label="Namespace" className="sm:col-span-2">
                <Input mono value={temporal.namespace} placeholder="default" onChange={(e) => set({ namespace: e.target.value })} />
              </Field>
              <Field label="Task queue">
                <Input mono value={temporal.task_queue} placeholder="shoal" onChange={(e) => set({ task_queue: e.target.value })} />
              </Field>
            </div>
            <Switch checked={temporal.tls} disabled={!canWrite} onCheckedChange={(v) => set({ tls: v })} label="Use TLS (on anyway with an API key or client certificates)" />

            <div className="space-y-3 rounded-lg border border-border p-3">
              <div>
                <div className="text-[13px] font-medium">Credentials</div>
                <p className="text-xs leading-snug text-faint">
                  Picked by name from secrets in a tenant's shared namespace, so keys never appear here. That tenant's admins can manage them. Save again after rotating one.
                </p>
              </div>
              <Field label="Secrets from tenant" className="sm:w-80">
                <NativeSelect value={temporal.secrets_tenant ?? ""} onChange={(e) => set({ secrets_tenant: e.target.value || null })}>
                  <option value="">— choose a tenant —</option>
                  {tenantOptions.map((t) => (
                    <option key={t.slug} value={t.slug}>
                      {t.name} ({t.slug})
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              <div className="grid gap-3 sm:grid-cols-2">
                {SECRETS.map(([key, label, help]) =>
                  secretsHere ? (
                    <SecretPicker
                      key={key}
                      ns={SHARED_NS}
                      field={label}
                      description={help}
                      value={temporal[key] ?? undefined}
                      disabled={!canWrite}
                      onChange={(v) => set({ [key]: v ?? null } as Partial<Record<SecretKey, string | null>>)}
                    />
                  ) : (
                    <Field key={key} label={label} description={help}>
                      <Input mono value={temporal[key] ?? ""} placeholder="secret name" onChange={(e) => set({ [key]: e.target.value || null } as Partial<Record<SecretKey, string | null>>)} />
                    </Field>
                  ),
                )}
              </div>
              {!!temporal.secrets_tenant && !secretsHere && (
                <p className="text-xs text-faint">Open {temporal.secrets_tenant} to pick or create its secrets here. Test connection tells you if a name is missing.</p>
              )}
            </div>
            {check && <CheckResult check={check} />}
          </fieldset>
        </Card>

        {canWrite && (
          <div className="flex justify-end gap-2">
            {status.source === "console" && (
              <Button variant="ghost" icon={<RotateCcw className="size-4" />} loading={reset.isPending} onClick={() => void onReset()}>
                Use environment settings
              </Button>
            )}
            <Button variant="primary" loading={save.isPending} disabled={!dirty || !temporal.address.trim()} onClick={() => void onSave()}>
              {choice !== status.orchestrator ? `Save and switch to ${LABEL[choice]}` : "Save"}
            </Button>
          </div>
        )}
      </div>
      <Summary status={status} />
    </div>
  );
}

function CheckResult({ check }: { check: TemporalCheck }) {
  return (
    <div
      role="status"
      className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px]", check.ok ? "border-ok/25 bg-ok-soft text-ok" : "border-bad/25 bg-bad-soft text-bad")}
    >
      {check.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> : <XCircle className="mt-0.5 size-4 shrink-0" />}
      <span>
        <b className="font-semibold">{check.ok ? "Connected." : "Couldn't connect."}</b> {check.detail}
        {check.server_version ? ` Temporal ${check.server_version}.` : ""}
      </span>
    </div>
  );
}

function Summary({ status }: { status: OrchestratorStatus }) {
  const runs = Object.entries(status.runs).filter(([, n]) => n > 0);
  return (
    <Card className="h-fit p-4">
      <div className="divide-y divide-border">
        <KV label="New runs">
          <Badge tone={status.orchestrator === "temporal" ? "info" : "neutral"}>{LABEL[status.orchestrator] ?? status.orchestrator}</Badge>
        </KV>
        <KV label="Set in">
          {status.source === "console" ? "This console" : <span className="text-muted">Environment</span>}
        </KV>
        {status.source === "console" && status.updated_at && (
          <KV label="Changed">
            <span className="text-muted">
              {dateTime(status.updated_at)}
              {status.updated_by ? ` by ${actorName(status.updated_by)}` : ""}
            </span>
          </KV>
        )}
        <KV label="Driving runs">{status.driving.map((d) => LABEL[d] ?? d).join(", ")}</KV>
        <KV label="Unfinished runs">
          {runs.length ? (
            <span>{runs.map(([name, n]) => `${n} on ${LABEL[name] ?? name}`).join(", ")}</span>
          ) : (
            <span className="text-muted">None</span>
          )}
        </KV>
        {status.environment.orchestrator === "temporal" && (
          <KV label="Environment">
            <span className="font-mono text-xs break-all">{status.environment.temporal_address}</span>
          </KV>
        )}
      </div>
    </Card>
  );
}
