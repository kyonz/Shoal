/**
 * Provider webhooks that Shoal registers for triggers (`Trigger.spec.webhook`): the settings on the
 * trigger editor, the registration's live status, and the list on a connection's page.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, Webhook } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ApiError, rawJson, type Schemas } from "@/api/client";
import { keys, type IntegrationCatalog } from "@/api/hooks";
import { SchemaForm } from "@/components/forms/SchemaForm";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import { nsPath } from "@/lib/ns";
import { Badge, Button, Card, CardHeader, CopyField, Switch } from "@/components/ui";

export type TriggerWebhook = Schemas["TriggerWebhookOut"];
type Tone = "neutral" | "ok" | "warn" | "bad" | "info";

const STATUS: Record<string, { tone: Tone; label: string }> = {
  active: { tone: "ok", label: "Registered" },
  pending: { tone: "neutral", label: "Pending" },
  error: { tone: "bad", label: "Failed" },
  removing: { tone: "warn", label: "Removing" },
};

export function WebhookBadge({ status }: { status?: string }) {
  const s = STATUS[status ?? "pending"] ?? STATUS.pending;
  return (
    <Badge tone={s.tone} dot>
      {s.label}
    </Badge>
  );
}

function providerIds(state: Record<string, unknown>): string | undefined {
  if (state.webhook_id) return String(state.webhook_id);
  if (Array.isArray(state.webhook_ids)) return state.webhook_ids.join(", ");
  return undefined;
}

/** The "register the webhook for me" block of the trigger editor, with a live preview of what gets registered. */
export function WebhookFields({ ns, catalog, connection, event, value, onChange }: {
  ns: string;
  catalog: IntegrationCatalog;
  connection?: string;
  event: string;
  value?: Record<string, unknown>;
  onChange: (v: Record<string, unknown> | undefined) => void;
}) {
  const on = value !== undefined;
  const [debounced, setDebounced] = useState(JSON.stringify(value ?? {}));
  useEffect(() => {
    const t = setTimeout(() => setDebounced(JSON.stringify(value ?? {})), 400);
    return () => clearTimeout(t);
  }, [value]);
  const preview = useQuery({
    queryKey: ["webhook-preview", ns, connection, event, debounced],
    queryFn: () =>
      rawJson<{ events?: string[]; jql?: string | null; column?: string }>(`/v1/namespaces/${ns}/trigger-webhook-preview`, {
        method: "POST",
        body: JSON.stringify({ connection, event, webhook: JSON.parse(debounced) }),
      }),
    enabled: on && !!connection && !!event,
    retry: false,
  });
  return (
    <div className="rounded-lg border border-border bg-subtle/40 p-3.5">
      <Switch
        checked={on}
        onCheckedChange={(v) => onChange(v ? {} : undefined)}
        label={
          <span className="text-[13px]">
            <span className="font-medium">Register the webhook in {catalog.title}</span>
            <span className="block text-xs text-muted">
              Shoal creates it, keeps it in sync with this trigger, refreshes it before it expires and deletes it with the trigger. It gets its own URL and signing secret.
            </span>
          </span>
        }
      />
      {on && catalog.webhook_schema && (
        <div className="mt-3.5 space-y-3">
          <SchemaForm schema={catalog.webhook_schema} value={value} onChange={onChange} order={["jql", "board", "column", "statuses"]} collapseOptional />
          {preview.error ? (
            <p className="flex items-start gap-1.5 text-xs text-bad">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              {preview.error instanceof ApiError ? preview.error.detail : String(preview.error)}
            </p>
          ) : preview.data ? (
            <div className="rounded-md border border-border bg-panel p-2.5 text-xs">
              <div className="mb-1 text-[11px] font-semibold tracking-wider text-faint uppercase">Will register</div>
              <div className="space-y-0.5">
                <div>
                  <span className="text-muted">Events </span>
                  <code className="font-mono">{(preview.data.events ?? []).join(", ")}</code>
                </div>
                <div>
                  <span className="text-muted">Filter </span>
                  <code className="font-mono break-all">{preview.data.jql ?? "none (every issue)"}</code>
                </div>
                {preview.data.column && <div className="text-muted">Column {preview.data.column}</div>}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** Live status of a saved trigger's registration, with "Sync now". */
export function WebhookStatusCard({ ns, name }: { ns: string; name: string }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["trigger-webhook", ns, name],
    queryFn: () => rawJson<TriggerWebhook | null>(`/v1/namespaces/${ns}/triggers/${name}/webhook`),
    refetchInterval: 15_000,
  });
  const sync = useMutation({
    mutationFn: () => rawJson<TriggerWebhook | null>(`/v1/namespaces/${ns}/triggers/${name}/webhook/sync`, { method: "POST" }),
    onSuccess: (w) => {
      qc.setQueryData(["trigger-webhook", ns, name], w);
      void qc.invalidateQueries({ queryKey: keys.resource("triggers", ns, name) });
      if (w?.status === "active") toast.success("Webhook registered");
      else if (w?.error) toast.error(w.error);
    },
  });
  const w = status.data;
  const state = (w?.state ?? {}) as Record<string, unknown>;
  const rows: [string, string | undefined][] = w
    ? [
        ["Mode", state.mode as string | undefined],
        ["Events", Array.isArray(state.events) ? state.events.join(", ") : undefined],
        ["Filter", (state.jql as string | null | undefined) ?? undefined],
        ["Provider id", providerIds(state)],
        ["Registered by", w.registered_by ?? undefined],
      ]
    : [];
  return (
    <Card>
      <CardHeader
        title={<span className="flex items-center gap-1.5"><Webhook className="size-3.5" /> Registered webhook</span>}
        description="The webhook Shoal holds in the provider for this trigger."
      />
      <div className="space-y-3 p-4 text-xs">
        {!w ? (
          <p className="text-muted">{status.isLoading ? "Loading…" : "Not registered yet. It is registered when the trigger is saved and enabled."}</p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2">
              <WebhookBadge status={w.status} />
              {w.last_received_at && <span className="text-faint" title={dateTime(w.last_received_at)}>last delivery {relTime(w.last_received_at)}</span>}
            </div>
            {w.error && (
              <div className="rounded-md border border-bad/25 bg-bad-soft p-2 text-bad">
                {w.error}
                {w.next_attempt_at && <div className="mt-1 text-[11px] opacity-80">Retrying {relTime(w.next_attempt_at)}</div>}
              </div>
            )}
            {w.url && <CopyField value={w.url} />}
            <dl className="grid grid-cols-[96px_1fr] gap-x-2 gap-y-1">
              {rows.filter(([, v]) => v).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted">{k}</dt>
                  <dd className="font-mono break-all">{v}</dd>
                </div>
              ))}
              {w.expires_at && (
                <div className="contents">
                  <dt className="text-muted">Expires</dt>
                  <dd title={dateTime(w.expires_at)}>{relTime(w.expires_at)} · refreshed automatically</dd>
                </div>
              )}
            </dl>
          </>
        )}
        {can("triggers:write", ns) && (
          <Button className="w-full justify-center" icon={<RefreshCw className="size-4" />} loading={sync.isPending} onClick={() => sync.mutate()}>
            Sync now
          </Button>
        )}
      </div>
    </Card>
  );
}

/** Webhooks registered through a connection, one per trigger (connection page). */
export function ConnectionWebhooks({ ns, name }: { ns: string; name: string }) {
  const { can } = useAuth();
  const list = useQuery({
    queryKey: ["connection-webhooks", ns, name],
    queryFn: () => rawJson<TriggerWebhook[]>(`/v1/namespaces/${ns}/connections/${name}/webhooks`),
    enabled: can("connections:read", ns),
    refetchInterval: 30_000,
  });
  if (!list.data?.length) return null;
  return (
    <Card>
      <CardHeader title="Registered webhooks" description="Webhooks Shoal manages in the provider for triggers on this connection." />
      <ul className="divide-y divide-border">
        {list.data.map((w) => (
          <li key={w.id} className="flex items-center justify-between gap-2 px-4 py-2.5 text-xs">
            <div className="min-w-0">
              <Link className="font-mono font-medium text-accent" to={nsPath(w.namespace, "triggers", w.trigger)}>
                {w.namespace === ns ? w.trigger : `${w.namespace}/${w.trigger}`}
              </Link>
              <div className="truncate text-faint" title={w.error ?? String((w.state as Record<string, unknown>).jql ?? "")}>
                {w.error ?? String((w.state as Record<string, unknown>).jql ?? (w.state as Record<string, unknown>).mode ?? "")}
              </div>
            </div>
            <WebhookBadge status={w.status} />
          </li>
        ))}
      </ul>
    </Card>
  );
}
