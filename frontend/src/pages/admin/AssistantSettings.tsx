/** /admin/assistant: which gateway, model and effort the in-app assistant uses in this organisation. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquareText, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Card, CardHeader, Field, Input, NativeSelect, Page, PageHeader } from "@/components/ui";
import { useProviders } from "@/pages/catalog/widgets";

type Setting = "gateway" | "model" | "effort";
interface AssistantInfo {
  enabled: boolean;
  gateway: string;
  model: string;
  effort: string;
  defaults: Record<Setting, string>;
  overrides: Partial<Record<Setting, string>>;
}

const KEY = ["assistant"] as const;
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export default function AssistantSettings() {
  const { can } = useAuth();
  const write = can("tenant:manage");
  const qc = useQueryClient();
  const info = useQuery({ queryKey: KEY, queryFn: () => rawJson<AssistantInfo>("/v1/assistant") });
  const gateways = useResources("gateways", "shared");
  const providers = useProviders();
  const [draft, setDraft] = useState<Partial<Record<Setting, string>>>({});
  useEffect(() => setDraft(info.data?.overrides ?? {}), [info.data]);

  const save = useMutation({
    mutationFn: (body: Partial<Record<Setting, string>>) =>
      rawJson<AssistantInfo>("/v1/assistant/settings", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      toast.success("Saved the assistant's settings");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const defaults = info.data?.defaults;
  const set = (k: Setting, v: string) => setDraft((d) => ({ ...d, [k]: v || undefined }));
  const chosen = (gateways.data ?? []).find((g) => g.metadata.name === (draft.gateway ?? defaults?.gateway));
  const chosenSpec = (chosen?.spec ?? {}) as { type?: string; api_key_secret?: string; config?: { mode?: string } };
  const subscription = chosenSpec.type === "claude-code" && (chosenSpec.config?.mode ?? (chosenSpec.api_key_secret ? "api_key" : "subscription")) === "subscription";
  const gatewayNames = new Set((gateways.data ?? []).map((g) => g.metadata.name));

  return (
    <Page>
      <PageHeader
        icon={<MessageSquareText className="size-4" />}
        title="Assistant"
        description="The gateway, model and effort the in-app assistant uses for everyone in this organisation. Anything left blank uses the server's SHOAL_ASSISTANT_* setting."
      />
      <Card>
        <CardHeader
          title="Model"
          actions={info.data && !info.data.enabled ? <Badge tone="warn">Turned off on this server (SHOAL_ASSISTANT_ENABLED)</Badge> : undefined}
        />
        <fieldset disabled={!write || !info.data} className="space-y-4 p-4">
          <Field label="Gateway" description={<>Server default: <span className="font-mono">{defaults?.gateway ?? "…"}</span>. Gateways are managed under <Link to="/admin/gateways" className="text-accent hover:underline">Gateways</Link>.</>}>
            <NativeSelect value={draft.gateway ?? ""} onChange={(e) => set("gateway", e.target.value)}>
              <option value="">Server default ({defaults?.gateway ?? "…"})</option>
              <optgroup label="Gateways">
                {(gateways.data ?? []).map((g) => (
                  <option key={g.metadata.name} value={g.metadata.name}>
                    {g.metadata.name} ({String((g.spec as { type?: string }).type ?? "anthropic")})
                  </option>
                ))}
              </optgroup>
              <optgroup label="Providers used directly">
                {(providers.data ?? []).filter((p) => !gatewayNames.has(p.name) && p.name !== "claude-code").map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </optgroup>
            </NativeSelect>
          </Field>
          {subscription && (
            <p className="rounded-lg border border-border bg-subtle px-3 py-2 text-[13px] text-muted">
              This Claude Code gateway runs on each person's own Claude plan: everyone chats on their own subscription once they link it under{" "}
              <Link to="/settings/credentials" className="text-accent hover:underline">Connected accounts</Link>. Each reply starts the Claude Code CLI on the server, so it is slower than an API key gateway.
            </p>
          )}
          <Field label="Model" description={`Server default: ${defaults?.model ?? "…"}`}>
            <Input mono value={draft.model ?? ""} placeholder={defaults?.model} onChange={(e) => set("model", e.target.value)} />
          </Field>
          <Field label="Effort" description={`Server default: ${defaults?.effort ?? "…"}`}>
            <NativeSelect value={draft.effort ?? ""} onChange={(e) => set("effort", e.target.value)}>
              <option value="">Server default ({defaults?.effort ?? "…"})</option>
              {EFFORTS.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </NativeSelect>
          </Field>
          {write && (
            <div className="flex gap-2">
              <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(draft)}>
                Save
              </Button>
              <Button icon={<RotateCcw className="size-3.5" />} disabled={!Object.keys(info.data?.overrides ?? {}).length} onClick={() => save.mutate({})}>
                Use server defaults
              </Button>
            </div>
          )}
        </fieldset>
      </Card>
    </Page>
  );
}
