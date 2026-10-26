/** Custom integrations: the organisation registers a remote service or an installed package. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Package, RefreshCw, Server, TriangleAlert } from "lucide-react";
import { Link } from "react-router";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { keys } from "@/api/hooks";
import { lastNamespace, nsPath } from "@/lib/ns";
import { Badge, Button, Card, CardHeader, Segmented } from "@/components/ui";
import type { EditorContext, EditorExtension } from "./ResourceEditorPage";
import { SharedSecretPicker } from "./extensions";
import { NamespaceMultiPicker } from "./widgets";

export interface CustomIntegrationStatus {
  ready?: boolean;
  error?: string | null;
  title?: string;
  connection_scopes?: string[];
  secret_fields?: string[];
  auth?: string | null;
  actions?: number;
  resources?: number;
  events?: number;
  version?: string | null;
}

const SCOPES = [
  { value: "org", label: "Organisation", hint: "Admins connect it once and share it with namespaces." },
  { value: "namespace", label: "Namespace", hint: "Each team connects it with its own credentials." },
];

/** Where connections may live; empty means whatever the integration itself declares. */
export function ConnectionScopesPicker({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const list = (value as string[] | undefined) ?? [];
  const mode = list.length === 1 ? list[0] : "both";
  return (
    <div className="space-y-1.5">
      <Segmented
        value={mode}
        onChange={(v) => onChange(v === "both" ? undefined : [v])}
        options={[{ value: "both", label: "Both" }, ...SCOPES.map((s) => ({ value: s.value, label: `${s.label} only`, title: s.hint }))]}
      />
      <p className="text-xs text-muted">
        {mode === "both" ? "Whatever the integration supports: organisation connections, namespace connections, or both." : SCOPES.find((s) => s.value === mode)?.hint}
      </p>
    </div>
  );
}

export const customIntegrationExtension: EditorExtension = {
  widgets: () => ({
    source: ({ value, onChange }) => (
      <Segmented
        value={(value as string) ?? "remote"}
        onChange={onChange}
        options={[
          { value: "remote", label: "Remote service (HTTP)", icon: <Server className="size-3.5" /> },
          { value: "package", label: "Installed Python package", icon: <Package className="size-3.5" /> },
        ]}
      />
    ),
    token_secret: ({ value, onChange }) => <SharedSecretPicker value={value} onChange={onChange} />,
    connection_scopes: ({ value, onChange }) => <ConnectionScopesPicker value={value} onChange={onChange} />,
    namespaces: ({ value, onChange }) => <NamespaceMultiPicker value={value} onChange={onChange} />,
  }),
  actions: (ctx) => <RefreshButton ctx={ctx} />,
  banner: (ctx) => <StatusBanner ctx={ctx} />,
};

function RefreshButton({ ctx }: { ctx: EditorContext }) {
  const qc = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => rawJson<CustomIntegrationStatus>(`/v1/custom-integrations/${encodeURIComponent(ctx.name ?? "")}/refresh`, { method: "POST" }),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: keys.resource("custom-integrations", ctx.ns, ctx.name ?? "") });
      void qc.invalidateQueries({ queryKey: keys.integrations() });
      if (s.ready) toast.success("Pulled the latest manifest");
      else toast.error(s.error ?? "Could not pull the manifest");
    },
  });
  if (ctx.isNew || !ctx.canWrite) return null;
  return (
    <Button variant="ghost" icon={<RefreshCw className="size-4" />} loading={refresh.isPending} onClick={() => refresh.mutate()}>
      Pull manifest
    </Button>
  );
}

function StatusBanner({ ctx }: { ctx: EditorContext }) {
  if (ctx.isNew) {
    return (
      <Card className="mb-4 p-4 text-[13px] text-muted">
        Point Shoal at a service speaking the <code className="font-mono">shoal.integration/v1</code> protocol, or at a package installed on the server.
        Shoal pulls its manifest (connection settings, auth, actions, resources and events) when you save, and it appears in every allowed namespace's
        integrations catalogue under this name.
      </Card>
    );
  }
  const status = (ctx.resource?.status ?? {}) as CustomIntegrationStatus;
  if (!status.ready) {
    return (
      <div className="mb-4 flex items-start gap-2 rounded-lg border border-bad/25 bg-bad-soft px-3 py-2.5 text-[13px] text-bad">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0">
          <div className="font-medium">Shoal could not pull this integration's manifest, so nobody can connect it yet.</div>
          {status.error && <p className="mt-0.5 break-words">{status.error}</p>}
        </div>
      </div>
    );
  }
  const ns = lastNamespace();
  return (
    <Card className="mb-4">
      <CardHeader
        title={
          <span className="flex items-center gap-1.5">
            <CheckCircle2 className="size-4 text-ok" /> Ready: {status.title}
            {status.version && <Badge>v{status.version}</Badge>}
          </span>
        }
        description="Pulled from the integration's manifest. People connect it with forms rendered from its settings schema."
        actions={
          <Link to={nsPath(ns, "integrations", ctx.name ?? "")} className="text-xs text-accent hover:underline">
            View in catalogue
          </Link>
        }
      />
      <div className="flex flex-wrap gap-1.5 px-4 pb-4 text-[11px] text-muted">
        <span className="rounded-md bg-subtle px-1.5 py-0.5">{status.actions ?? 0} actions</span>
        <span className="rounded-md bg-subtle px-1.5 py-0.5">{status.events ?? 0} events</span>
        <span className="rounded-md bg-subtle px-1.5 py-0.5">{status.resources ?? 0} resources</span>
        {status.auth && <span className="rounded-md bg-subtle px-1.5 py-0.5">auth: {status.auth}</span>}
        {(status.secret_fields ?? []).length > 0 && <span className="rounded-md bg-subtle px-1.5 py-0.5">secrets: {status.secret_fields!.join(", ")}</span>}
        {(status.connection_scopes ?? []).map((s) => (
          <Badge key={s} tone="info">
            {s === "org" ? "Organisation connections" : "Namespace connections"}
          </Badge>
        ))}
      </div>
    </Card>
  );
}
