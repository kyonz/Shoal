import { AlertTriangle, CircleAlert, Settings2 } from "lucide-react";
import { useIntegrations } from "@/api/hooks";
import { Field, Input, ReadOnlyContext, Textarea } from "@/components/ui";
import type { Issue } from "@/features/workflow/validate";
import type { ParamDoc, WorkflowSpecDoc } from "@/features/workflow/types";
import { ExpressionMap, ParamsEditor, ResourceSelect, Section } from "./fields";
import { useBuilder } from "./store";

export function WorkflowSettings({ isNew, issues, readOnly }: { isNew: boolean; issues: Issue[]; readOnly?: boolean }) {
  const spec = useBuilder((s) => s.spec);
  const meta = useBuilder((s) => s.meta);
  const { setSpec, setMeta, select } = useBuilder.getState();
  const { data: integrations = [] } = useIntegrations();
  const used = new Set(
    spec.steps.flatMap((s) => {
      const out: string[] = [];
      if (typeof s.action === "string") out.push(s.action.split(".")[0]);
      const c = s.condition as { action?: string } | undefined;
      if (c?.action) out.push(c.action.split(".")[0]);
      return out;
    }),
  );
  for (const k of Object.keys(spec.connections ?? {})) used.add(k);
  const patch = (p: Partial<WorkflowSpecDoc>, key: string) => {
    const next: WorkflowSpecDoc = { ...spec, ...p };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined || (typeof v === "object" && v && !Array.isArray(v) && !Object.keys(v).length)) delete next[k];
    }
    setSpec(next, key);
  };
  const general = issues.filter((i) => !i.stepId);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
        <div className="flex size-8 items-center justify-center rounded-lg border border-border bg-subtle text-muted">
          <Settings2 className="size-4" />
        </div>
        <div>
          <div className="text-[13px] font-semibold">Workflow settings</div>
          <div className="text-[11px] text-faint">{readOnly ? "You can view this workflow but not change it." : "Select a step on the canvas to edit it."}</div>
        </div>
      </div>
      <ReadOnlyContext.Provider value={!!readOnly}>
        <fieldset disabled={readOnly} className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            {general.length > 0 && (
              <div className="space-y-1 border-b border-border px-4 py-3">
                {general.map((i, n) => (
                  <div key={n} className={`flex items-start gap-2 text-xs ${i.level === "error" ? "text-bad" : "text-warn"}`}>
                    {i.level === "error" ? <CircleAlert className="mt-0.5 size-3.5 shrink-0" /> : <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />}
                    {i.message}
                  </div>
                ))}
              </div>
            )}
            <Section title="General">
              <div className="space-y-3">
                <Field label="Name" required description={isNew ? "Lowercase letters, digits and dashes. Cannot be changed later." : undefined}>
                  <Input
                    mono
                    value={meta.name}
                    disabled={!isNew}
                    placeholder="my-workflow"
                    onChange={(e) => setMeta({ name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") }, "meta:name")}
                  />
                </Field>
                <Field label="Description">
                  <Textarea value={meta.description} className="min-h-14" placeholder="What does this workflow do?" onChange={(e) => setMeta({ description: e.target.value }, "meta:description")} />
                </Field>
              </div>
            </Section>
            <Section title="Inputs" description="Values supplied when a run starts (by a person, a trigger or a campaign).">
              <ParamsEditor kind="input" value={(spec.inputs as Record<string, ParamDoc>) ?? {}} onChange={(v) => patch({ inputs: v }, "spec:inputs")} />
            </Section>
            <Section title="Outputs" description="Expressions evaluated when the run finishes.">
              <ExpressionMap value={spec.outputs ?? {}} onChange={(v) => patch({ outputs: v }, "spec:outputs")} keyPlaceholder="output" addLabel="Add output" />
            </Section>
            <Section title="Defaults">
              <div className="space-y-3">
                <Field label="Default agent" description="Used by agent steps that don't pick one.">
                  <ResourceSelect plural="agents" value={spec.defaults?.agent} onChange={(v) => patch({ defaults: { ...spec.defaults, agent: v } }, "spec:defaults")} />
                </Field>
                <Field label="Default sandbox profile">
                  <ResourceSelect plural="sandbox-profiles" value={spec.defaults?.sandbox} onChange={(v) => patch({ defaults: { ...spec.defaults, sandbox: v } }, "spec:defaults")} />
                </Field>
                <Field label="Run timeout" hint="e.g. 7d">
                  <Input mono value={spec.timeout ?? ""} placeholder="none" onChange={(e) => patch({ timeout: e.target.value || undefined }, "spec:timeout")} />
                </Field>
              </div>
            </Section>
            <Section title="Connections" description="Which connection each integration uses by default.">
              {used.size === 0 ? (
                <p className="text-xs text-faint">Add an action step to choose connections.</p>
              ) : (
                <div className="space-y-2.5">
                  {[...used].map((key) => (
                    <Field key={key} label={integrations.find((i) => i.key === key)?.title ?? key}>
                      <ResourceSelect
                        plural="connections"
                        value={spec.connections?.[key]}
                        filter={(s) => s.integration === key}
                        placeholder="Choose a connection"
                        onChange={(v) => {
                          const next = { ...(spec.connections ?? {}) };
                          if (v) next[key] = v;
                          else delete next[key];
                          patch({ connections: next }, "spec:connections");
                        }}
                      />
                    </Field>
                  ))}
                </div>
              )}
            </Section>
            <div className="px-4 py-4">
              <button className="text-xs text-muted hover:text-fg" onClick={() => select(null)}>
                Tip: press <b>?</b> on the canvas for keyboard shortcuts.
              </button>
            </div>
        </fieldset>
      </ReadOnlyContext.Provider>
    </div>
  );
}
