/** /instance/assistant: the assistant's gateway, model and effort for every tenant that doesn't set its own. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquareText, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { Button, Card, CardHeader, Field, Input, NativeSelect, Page, PageHeader } from "@/components/ui";
import { useProviders } from "@/pages/catalog/widgets";

type Setting = "gateway" | "model" | "effort";
type Values = Partial<Record<Setting, string>>;
interface InstanceAssistant {
  server: Record<Setting, string>;
  instance: Values;
  effective: Record<Setting, string>;
  updated_by?: string | null;
  updated_at?: string | null;
}

const KEY = ["instance", "assistant"] as const;
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export default function InstanceAssistantPage() {
  const { instance } = useAuth();
  const canWrite = instance.admin;
  const qc = useQueryClient();
  const status = useQuery({ queryKey: KEY, queryFn: () => rawJson<InstanceAssistant>("/v1/instance/assistant") });
  const gateways = useResources("gateways", "shared", { retry: false });
  const providers = useProviders();
  const [draft, setDraft] = useState<Values>({});
  useEffect(() => setDraft(status.data?.instance ?? {}), [status.data]);
  const save = useMutation({
    mutationFn: (body: Values) => rawJson<InstanceAssistant>("/v1/instance/assistant", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (data) => {
      qc.setQueryData(KEY, data);
      void qc.invalidateQueries({ queryKey: ["assistant"] });
      toast.success("Saved the assistant's instance settings");
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const server = status.data?.server;
  const set = (k: Setting, v: string) => setDraft((d) => ({ ...d, [k]: v || undefined }));
  const suggestions = [...new Set([...(gateways.data ?? []).map((g) => g.metadata.name), ...(providers.data ?? []).map((p) => p.name)])];

  return (
    <Page>
      <PageHeader
        icon={<MessageSquareText className="size-4" />}
        title="Assistant"
        description="The gateway, model and effort the in-app assistant uses in every tenant. A tenant's own choice under Admin → Assistant wins; anything left blank here uses the server's SHOAL_ASSISTANT_* setting."
      />
      <Card>
        <CardHeader
          title="Instance default"
          description={status.data?.updated_at ? `Saved by ${status.data.updated_by ?? "someone"} on ${dateTime(status.data.updated_at)}` : "Nothing saved here yet: tenants use the server's settings."}
        />
        <fieldset disabled={!canWrite || !status.data} className="space-y-4 p-4">
          <Field
            label="Gateway"
            description={`Server default: ${server?.gateway ?? "…"}. Name a Gateway each tenant has (gateways belong to tenants), such as a claude-code gateway, or a provider such as anthropic.`}
          >
            <Input mono list="instance-assistant-gateways" value={draft.gateway ?? ""} placeholder={server?.gateway} onChange={(e) => set("gateway", e.target.value.trim())} />
            <datalist id="instance-assistant-gateways">
              {suggestions.map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
          </Field>
          <Field label="Model" description={`Server default: ${server?.model ?? "…"}`}>
            <Input mono value={draft.model ?? ""} placeholder={server?.model} onChange={(e) => set("model", e.target.value.trim())} />
          </Field>
          <Field label="Effort" description={`Server default: ${server?.effort ?? "…"}`}>
            <NativeSelect value={draft.effort ?? ""} onChange={(e) => set("effort", e.target.value)}>
              <option value="">Server default ({server?.effort ?? "…"})</option>
              {EFFORTS.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <p className="text-[13px] text-muted">
            A claude-code gateway on people's own Claude plans runs each chat on the plan of the person chatting; they link it under Settings, Connected accounts.
          </p>
          {canWrite && (
            <div className="flex gap-2">
              <Button variant="primary" loading={save.isPending} onClick={() => save.mutate(draft)}>
                Save
              </Button>
              <Button icon={<RotateCcw className="size-3.5" />} disabled={!Object.keys(status.data?.instance ?? {}).length} onClick={() => save.mutate({})}>
                Use server settings
              </Button>
            </div>
          )}
        </fieldset>
      </Card>
    </Page>
  );
}
