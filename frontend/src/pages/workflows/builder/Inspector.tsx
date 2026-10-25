import { AlertTriangle, CircleAlert, Copy, Sparkles, Trash2, X } from "lucide-react";
import { useMemo } from "react";
import { useResource, useStepSchema, type JsonSchema } from "@/api/hooks";
import { useNs } from "@/lib/ns";
import { Badge, Button, CodeEditor, Field, Input, NativeSelect, ReadOnlyContext, Segmented, Switch, Tooltip } from "@/components/ui";
import { KeyValueInput, SchemaForm, TagInput } from "@/components/forms/SchemaForm";
import { dependencies } from "@/features/workflow/graph";
import { ExpressionInput } from "@/features/workflow/ExpressionInput";
import { useAssistant } from "@/features/assistant/store";
import { SCRIPT_LANGUAGES, SCRIPT_TEMPLATES, editorLanguage, type ScriptLanguage } from "@/features/workflow/scriptTemplates";
import { stepType } from "@/features/workflow/stepTypes";
import type { Issue } from "@/features/workflow/validate";
import type { ParamDoc, StepDoc } from "@/features/workflow/types";
import { STEP_ID } from "@/features/workflow/types";
import { ActionPicker, ExpressionMap, ParamValues, ParamsEditor, RenameInput, ResourceSelect, ResourceTags, Section, useAction } from "./fields";
import { useBuilder } from "./store";

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

export function Inspector({ stepId, issues, readOnly }: { stepId: string; issues: Issue[]; readOnly?: boolean }) {
  const step = useBuilder((s) => s.spec.steps.find((x) => x.id === stepId));
  const spec = useBuilder((s) => s.spec);
  const { updateStep, rename, remove, duplicate, select, setDeps } = useBuilder.getState();
  if (!step) return null;
  const info = stepType(step.type);
  const Icon = info.icon;
  const set = (patch: Record<string, unknown>, key?: string) => {
    const next: StepDoc = { ...step };
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete next[k];
      else next[k] = v;
    }
    updateStep(step.id, next, key ?? `step:${step.id}:${Object.keys(patch).join(",")}`);
  };
  const deps = dependencies(spec.steps).get(step.id) ?? [];
  const mine = issues.filter((i) => i.stepId === step.id);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start gap-2.5 border-b border-border px-4 py-3">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg text-white" style={{ background: info.color }}>
          <Icon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-medium" style={{ color: info.color }}>
            {info.label}
          </div>
          {readOnly ? (
            <div className="h-7 truncate font-mono text-[14px] leading-7 font-semibold">{step.id}</div>
          ) : (
            <RenameInput
              value={step.id}
              onRename={(to) => STEP_ID.test(to) && rename(step.id, to)}
              className="-ml-1.5 h-7 text-[14px] font-semibold"
            />
          )}
        </div>
        {!readOnly && (
          <>
            <Tooltip content="Duplicate (⌘D)">
              <Button variant="ghost" size="icon-sm" onClick={() => duplicate(step.id)} aria-label="Duplicate step">
                <Copy className="size-3.5" />
              </Button>
            </Tooltip>
            <Tooltip content="Delete (Del)">
              <Button variant="ghost" size="icon-sm" onClick={() => remove([step.id])} aria-label="Delete step">
                <Trash2 className="size-3.5" />
              </Button>
            </Tooltip>
          </>
        )}
        <Button variant="ghost" size="icon-sm" onClick={() => select(null)} aria-label="Close inspector">
          <X className="size-4" />
        </Button>
      </div>

      <ReadOnlyContext.Provider value={!!readOnly}>
        <fieldset disabled={readOnly} className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            {mine.length > 0 && (
              <div className="space-y-1 border-b border-border px-4 py-3">
                {mine.map((i, n) => (
                  <div key={n} className={`flex items-start gap-2 text-xs ${i.level === "error" ? "text-bad" : "text-warn"}`}>
                    {i.level === "error" ? <CircleAlert className="mt-0.5 size-3.5 shrink-0" /> : <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />}
                    <span>
                      {i.message}
                      {i.source === "server" && <Badge className="ml-1.5">server</Badge>}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <Section title="Step">
              <div className="space-y-3">
                <Field label="Display name" hint="optional">
                  <Input value={str(step.name)} placeholder={step.id} onChange={(e) => set({ name: e.target.value })} />
                </Field>
                <p className="text-xs text-faint">{info.description}</p>
              </div>
            </Section>
            <TypeEditor step={step} set={set} />
            <Section title="Flow" description="When this step runs and what happens if it fails.">
              <div className="space-y-3">
                <Field label="Runs after" description="Drag between nodes on the canvas to connect steps.">
                  <div className="flex flex-wrap gap-1.5">
                    {spec.steps
                      .filter((s) => s.id !== step.id)
                      .map((s) => {
                        const on = deps.includes(s.id);
                        return (
                          <button
                            key={s.id}
                            type="button"
                            onClick={() => setDeps(step.id, on ? deps.filter((d) => d !== s.id) : [...deps, s.id])}
                            className={`h-6 rounded-md border px-2 font-mono text-[11px] transition-colors ${on ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-faint hover:text-fg"}`}
                          >
                            {s.id}
                          </button>
                        );
                      })}
                    {spec.steps.length < 2 && <span className="text-xs text-faint">No other steps yet.</span>}
                  </div>
                </Field>
                <Field label="Run only if" description="Expression; the step is skipped when it is false.">
                  <ExpressionInput value={str(step.if)} onChange={(v) => set({ if: v }, `step:${step.id}:if`)} placeholder="{{ steps.fix.output.tests_passed }}" />
                </Field>
                <div className="grid grid-cols-2 gap-2.5">
                  <Field label="On error">
                    <NativeSelect value={str(step.on_error) || "fail"} onChange={(e) => set({ on_error: e.target.value === "fail" ? undefined : e.target.value })}>
                      <option value="fail">Fail the run</option>
                      <option value="continue">Continue</option>
                    </NativeSelect>
                  </Field>
                  <Field label="Timeout" hint="e.g. 30m, 2h">
                    <Input mono value={str(step.timeout)} placeholder="none" onChange={(e) => set({ timeout: e.target.value })} />
                  </Field>
                </div>
                <RetryEditor value={(step.retry as Record<string, unknown>) ?? {}} onChange={(r) => set({ retry: Object.keys(r).length ? r : undefined })} />
              </div>
            </Section>
        </fieldset>
      </ReadOnlyContext.Provider>
    </div>
  );
}

function RetryEditor({ value, onChange }: { value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void }) {
  const attempts = Number(value.max_attempts ?? 1);
  const upd = (k: string, v: unknown) => {
    const next = { ...value, [k]: v };
    if (v === undefined || v === "") delete next[k];
    if (Number(next.max_attempts ?? 1) <= 1) {
      onChange({});
      return;
    }
    onChange(next);
  };
  return (
    <div className="grid grid-cols-2 gap-2.5">
      <Field label="Attempts">
        <Input type="number" min={1} max={100} value={attempts} onChange={(e) => upd("max_attempts", Math.max(1, Number(e.target.value) || 1))} />
      </Field>
      <Field label="Backoff">
        <Input mono disabled={attempts <= 1} value={str(value.backoff)} placeholder="10s" onChange={(e) => upd("backoff", e.target.value)} />
      </Field>
    </div>
  );
}

// --------------------------------------------------------------------------- per-type editors

type SetFn = (patch: Record<string, unknown>, key?: string) => void;

function TypeEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  switch (step.type) {
    case "agent":
      return <AgentEditor step={step} set={set} />;
    case "action":
      return <ActionEditor step={step} set={set} />;
    case "script":
      return <ScriptEditor step={step} set={set} />;
    case "wait":
      return <WaitEditor step={step} set={set} />;
    case "approval":
      return <ApprovalEditor step={step} set={set} />;
    case "set":
      return (
        <Section title="Values" description="Each value becomes steps.<id>.output.<name>.">
          <ExpressionMap value={(step.values as Record<string, unknown>) ?? {}} onChange={(v) => set({ values: v })} keyPlaceholder="value" />
        </Section>
      );
    case "workflow":
      return <SubWorkflowEditor step={step} set={set} />;
    default:
      return <GenericEditor step={step} set={set} />;
  }
}

function AgentEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const defaultAgent = useBuilder((s) => s.spec.defaults?.agent);
  return (
    <>
      <Section title="Agent">
        <div className="space-y-3">
          <Field label="Agent" description={!step.agent && defaultAgent ? `Uses the workflow default: ${defaultAgent}` : undefined}>
            <ResourceSelect plural="agents" value={step.agent as string} onChange={(v) => set({ agent: v })} placeholder={defaultAgent ? `Workflow default (${defaultAgent})` : "Choose an agent"} />
          </Field>
          <Field label="Prompt" required description="Type {{ to reference inputs and earlier steps.">
            <ExpressionInput multiline minHeight="160px" value={str(step.prompt)} onChange={(v) => set({ prompt: v }, `step:${step.id}:prompt`)} placeholder="Describe the task for the agent…" />
          </Field>
        </div>
      </Section>
      <Section title="Outputs" description="The agent must return these; later steps read steps.<id>.output.<name>.">
        <ParamsEditor kind="output" value={(step.outputs as Record<string, ParamDoc>) ?? {}} onChange={(v) => set({ outputs: Object.keys(v).length ? v : undefined })} />
      </Section>
      <Section title="Environment">
        <div className="space-y-3">
          <Field label="Sandbox profile">
            <ResourceSelect plural="sandbox-profiles" value={step.sandbox as string} onChange={(v) => set({ sandbox: v })} placeholder="Agent / workflow default" />
          </Field>
          <Field label="Extra skills">
            <ResourceTags plural="skills" value={(step.skills as string[]) ?? []} onChange={(v) => set({ skills: v })} />
          </Field>
          <Field label="Extra tools" description="Built-ins (shell, read_file, http…) or integration actions (github.*).">
            <TagInput value={(step.tools as string[]) ?? []} onChange={(v) => set({ tools: v })} />
          </Field>
        </div>
      </Section>
    </>
  );
}

function ActionEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const action = useAction(step.action as string);
  const integration = String(step.action ?? "").split(".")[0];
  return (
    <Section title="Action">
      <div className="space-y-3">
        <Field label="Action" required>
          <ActionPicker value={step.action as string} onChange={(v) => set({ action: v })} />
        </Field>
        {action?.mutating && (
          <p className="flex items-center gap-1.5 rounded-lg bg-warn-soft px-2.5 py-1.5 text-xs text-warn">
            <AlertTriangle className="size-3.5" /> This action changes things in {integration}.
          </p>
        )}
        <Field label="Connection" description="Defaults to the workflow's connection for this integration.">
          <ResourceSelect
            plural="connections"
            value={step.connection as string}
            onChange={(v) => set({ connection: v })}
            filter={(spec) => !integration || spec.integration === integration}
            placeholder="Workflow default"
          />
        </Field>
        {step.action ? (
          <div className="pt-1">
            <div className="mb-2 text-[11px] font-semibold tracking-wider text-faint uppercase">Parameters</div>
            <ParamValues schema={action?.input_schema} value={(step.with as Record<string, unknown>) ?? {}} onChange={(v) => set({ with: v }, `step:${step.id}:with`)} />
          </div>
        ) : null}
      </div>
    </Section>
  );
}

function ScriptEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const workflow = useBuilder((s) => s.meta.name);
  const ask = useAssistant((s) => s.ask);
  const mode = step.code !== undefined || step.run === undefined ? "code" : "run";
  const language = (step.language as ScriptLanguage | undefined) ?? "shell";
  const switchMode = (m: "code" | "run") => {
    if (m === mode) return;
    if (m === "run") set({ code: undefined, language: undefined, run: "echo hello" });
    else set({ run: undefined, language: "python", code: SCRIPT_TEMPLATES.python });
  };
  const switchLanguage = (lang: ScriptLanguage) => {
    const code = str(step.code);
    const untouched = !code.trim() || Object.values(SCRIPT_TEMPLATES).includes(code);
    set({ language: lang === "shell" ? undefined : lang, ...(untouched ? { code: SCRIPT_TEMPLATES[lang] } : {}) });
  };
  const outputs = Object.keys((step.outputs as Record<string, unknown>) ?? {});
  const inputs = Object.keys((step.inputs as Record<string, unknown>) ?? {});
  const askAssistant = () =>
    ask(
      `Write the ${SCRIPT_LANGUAGES.find((l) => l.value === language)?.label ?? language} code for the script step \`${step.id}\`` +
        (workflow ? ` in workflow \`${workflow}\`` : "") +
        (inputs.length ? ` (inputs: ${inputs.join(", ")})` : "") +
        (outputs.length ? ` (outputs: ${outputs.join(", ")})` : "") +
        ". It should: ",
    );
  return (
    <>
      <Section
        title="Script"
        description="Deterministic code in the sandbox: faster, cheaper and repeatable compared with an agent."
        action={<Segmented value={mode} onChange={switchMode} options={[{ value: "code", label: "Code" }, { value: "run", label: "Command" }]} />}
      >
        <div className="space-y-3">
          {mode === "code" ? (
            <>
              <div className="flex items-center gap-2">
                <NativeSelect value={language} onChange={(e) => switchLanguage(e.target.value as ScriptLanguage)} className="w-36">
                  {SCRIPT_LANGUAGES.map((l) => (
                    <option key={l.value} value={l.value}>
                      {l.label}
                    </option>
                  ))}
                </NativeSelect>
                <div className="flex-1" />
                <Button size="sm" variant="ghost" icon={<Sparkles className="size-3.5" />} onClick={askAssistant}>
                  Write with assistant
                </Button>
              </div>
              <Field label="Code" required description="Used as written (expressions are not rendered here): pass data in through Inputs.">
                <CodeEditor
                  value={str(step.code)}
                  onChange={(v) => set({ code: v }, `step:${step.id}:code`)}
                  language={editorLanguage(language)}
                  minHeight="180px"
                  maxHeight="460px"
                  className="rounded-lg border border-border bg-subtle/30 font-mono text-[12px]"
                />
              </Field>
            </>
          ) : (
            <Field label="Command" required description="A shell command line; {{ }} expressions are rendered. Output: exit_code, stdout, stderr.">
              <ExpressionInput multiline mono minHeight="120px" value={str(step.run)} onChange={(v) => set({ run: v }, `step:${step.id}:run`)} />
            </Field>
          )}
        </div>
      </Section>
      <Section title="Inputs" description="Rendered expressions, passed to the program as JSON ($SHOAL_INPUTS; Python and Node: shoal_step.inputs).">
        <ExpressionMap value={(step.inputs as Record<string, unknown>) ?? {}} onChange={(v) => set({ inputs: Object.keys(v).length ? v : undefined })} keyPlaceholder="input" addLabel="Add input" />
      </Section>
      <Section title="Outputs" description="Typed values the program writes ($SHOAL_OUTPUTS or set_output); later steps read steps.<id>.output.<name>.">
        <ParamsEditor kind="output" value={(step.outputs as Record<string, ParamDoc>) ?? {}} onChange={(v) => set({ outputs: Object.keys(v).length ? v : undefined })} />
      </Section>
      <Section title="Environment">
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2.5">
            <Field label="Sandbox profile">
              <ResourceSelect plural="sandbox-profiles" value={step.sandbox as string} onChange={(v) => set({ sandbox: v })} placeholder="Default" />
            </Field>
            <Field label="Working directory">
              <Input mono value={str(step.workdir)} placeholder="/workspace" onChange={(e) => set({ workdir: e.target.value })} />
            </Field>
          </div>
          <Field label="Environment variables">
            <KeyValueInput value={(step.env as Record<string, string>) ?? {}} onChange={(v) => set({ env: Object.keys(v).length ? v : undefined })} keyPlaceholder="NAME" />
          </Field>
          <Switch checked={step.fail_on_error !== false} onCheckedChange={(c) => set({ fail_on_error: c ? undefined : false })} label={<span className="text-xs">Fail the step on a non-zero exit code</span>} />
        </div>
      </Section>
    </>
  );
}

function WaitEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const mode = step.signal ? "signal" : step.condition ? "condition" : step.until ? "until" : "duration";
  const signal = (step.signal as { name?: string; event?: string; match?: string }) ?? {};
  const sigSource = signal.event !== undefined ? "event" : "name";
  const setSig = (patch: Record<string, string | undefined>, key?: string) => {
    const next: Record<string, string> = { ...signal } as Record<string, string>;
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined) delete next[k];
      else next[k] = v;
    }
    set({ signal: next }, key);
  };
  const condition = (step.condition as Record<string, unknown>) ?? {};
  const action = useAction(condition.action as string);
  const switchMode = (m: string) => {
    if (m === mode) return;
    set({
      duration: m === "duration" ? "1h" : undefined,
      until: m === "until" ? "{{ inputs.deadline }}" : undefined,
      condition: m === "condition" ? { action: "", check: "{{ result }}", poll_interval: "15m" } : undefined,
      signal: m === "signal" ? { name: "approved" } : undefined,
    });
  };
  const setCond = (patch: Record<string, unknown>, key?: string) => {
    const next = { ...condition, ...patch };
    for (const [k, v] of Object.entries(patch)) if (v === undefined || v === "") delete next[k];
    set({ condition: next }, key);
  };
  return (
    <Section title="Wait">
      <div className="space-y-3">
        <Segmented
          value={mode}
          onChange={switchMode}
          options={[
            { value: "duration", label: "Duration", title: "Wait for a fixed duration" },
            { value: "until", label: "Time", title: "Wait until a point in time" },
            { value: "condition", label: "Condition", title: "Poll until a condition holds" },
            { value: "signal", label: "Signal", title: "Wait for a signal sent into the run" },
          ]}
        />
        {mode === "duration" && (
          <Field label="Duration" description="e.g. 30s, 15m, 4h, 7d. Durable: survives restarts.">
            <Input mono value={str(step.duration)} onChange={(e) => set({ duration: e.target.value })} />
          </Field>
        )}
        {mode === "until" && (
          <Field label="Until" description="Expression producing an ISO-8601 timestamp.">
            <ExpressionInput value={str(step.until)} onChange={(v) => set({ until: v }, `step:${step.id}:until`)} />
          </Field>
        )}
        {mode === "condition" && (
          <>
            <Field label="Poll action" required>
              <ActionPicker value={condition.action as string} onChange={(v) => setCond({ action: v })} />
            </Field>
            {condition.action ? (
              <ParamValues schema={action?.input_schema} value={(condition.with as Record<string, unknown>) ?? {}} onChange={(v) => setCond({ with: v }, `step:${step.id}:cond-with`)} />
            ) : null}
            <Field label="Done when" required description="Evaluated with `result` (the action's output).">
              <ExpressionInput value={str(condition.check)} onChange={(v) => setCond({ check: v }, `step:${step.id}:check`)} placeholder="{{ result.review_decision == 'approved' }}" />
            </Field>
            <div className="grid grid-cols-2 gap-2.5">
              <Field label="Poll every">
                <Input mono value={str(condition.poll_interval)} placeholder="15m" onChange={(e) => setCond({ poll_interval: e.target.value })} />
              </Field>
              <Field label="Give up after">
                <Input mono value={str(step.max_wait)} placeholder="never" onChange={(e) => set({ max_wait: e.target.value })} />
              </Field>
            </div>
            <Field label="Wake early on events" description="Event keys, e.g. github:pr:{{ inputs.repo }}#12.">
              <TagInput value={(condition.wake_on as string[]) ?? []} onChange={(v) => setCond({ wake_on: v.length ? v : undefined })} />
            </Field>
            <Field label="When time runs out">
              <NativeSelect value={str(step.on_timeout) || "continue"} onChange={(e) => set({ on_timeout: e.target.value === "continue" ? undefined : e.target.value })}>
                <option value="continue">Continue (output.timed_out = true)</option>
                <option value="fail">Fail the step</option>
              </NativeSelect>
            </Field>
          </>
        )}
        {mode === "signal" && (
          <>
            <Segmented
              value={sigSource}
              onChange={(v) => setSig(v === "event" ? { name: undefined, event: "" } : { event: undefined, name: "" })}
              options={[
                { value: "name", label: "Named signal" },
                { value: "event", label: "Integration event" },
              ]}
            />
            {sigSource === "name" ? (
              <Field
                label="Signal name"
                required
                description="Send it with Send signal on the run page, POST /v1/runs/{id}/signals/<name>, or the run's callback URL {{ run.signal_url }}/<name>. Signals sent early are kept."
              >
                <Input mono value={signal.name ?? ""} placeholder="approved" onChange={(e) => setSig({ name: e.target.value })} />
              </Field>
            ) : (
              <Field label="Event key" required description="Takes the next integration event with this wake key, e.g. github:pr:{{ inputs.repo }}#{{ inputs.pr }}.">
                <ExpressionInput value={signal.event ?? ""} onChange={(v) => setSig({ event: v }, `step:${step.id}:sig-event`)} />
              </Field>
            )}
            <Field label="Only accept when" description="Optional. Evaluated with `signal` (the payload); signals that don't match stay queued.">
              <ExpressionInput value={signal.match ?? ""} onChange={(v) => setSig({ match: v || undefined }, `step:${step.id}:sig-match`)} placeholder="{{ signal.status == 'done' }}" />
            </Field>
            <div className="grid grid-cols-2 gap-2.5">
              <Field label="Give up after">
                <Input mono value={str(step.max_wait)} placeholder="never" onChange={(e) => set({ max_wait: e.target.value || undefined })} />
              </Field>
              <Field label="When time runs out">
                <NativeSelect value={str(step.on_timeout) || "continue"} onChange={(e) => set({ on_timeout: e.target.value === "continue" ? undefined : e.target.value })}>
                  <option value="continue">Continue</option>
                  <option value="fail">Fail the step</option>
                </NativeSelect>
              </Field>
            </div>
            <p className="text-[11.5px] text-faint">
              Outputs: <code className="font-mono">received</code>, <code className="font-mono">payload</code>, <code className="font-mono">source</code>, <code className="font-mono">sender</code>,{" "}
              <code className="font-mono">timed_out</code>.
            </p>
          </>
        )}
      </div>
    </Section>
  );
}

function ApprovalEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  return (
    <Section title="Approval" description="The run pauses here until someone approves or rejects it.">
      <div className="space-y-3">
        <Field label="Message" required>
          <ExpressionInput multiline value={str(step.message)} onChange={(v) => set({ message: v }, `step:${step.id}:message`)} />
        </Field>
        <Field label="Approvers" description="Users or groups; empty means anyone with runs:approve.">
          <TagInput value={(step.approvers as string[]) ?? []} onChange={(v) => set({ approvers: v })} placeholder="user:dana@acme.com" />
        </Field>
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Expire after">
            <Input mono value={str(step.max_wait)} placeholder="never" onChange={(e) => set({ max_wait: e.target.value })} />
          </Field>
          <Field label="On expiry">
            <NativeSelect value={str(step.on_timeout) || "reject"} onChange={(e) => set({ on_timeout: e.target.value === "reject" ? undefined : e.target.value })}>
              <option value="reject">Reject</option>
              <option value="approve">Approve</option>
            </NativeSelect>
          </Field>
        </div>
      </div>
    </Section>
  );
}

function SubWorkflowEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const ns = useNs();
  const child = useResource("workflows", ns, (step.workflow as string) || undefined);
  const inputs = (child.data?.spec as { inputs?: Record<string, ParamDoc> } | undefined)?.inputs;
  const schema: JsonSchema | undefined = inputs
    ? {
        type: "object",
        properties: Object.fromEntries(Object.entries(inputs).map(([k, p]) => [k, { type: p.type === "any" ? undefined : p.type, description: p.description, default: p.default }])),
        required: Object.entries(inputs).filter(([, p]) => p.required ?? p.default == null).map(([k]) => k),
      }
    : undefined;
  return (
    <Section title="Sub-workflow">
      <div className="space-y-3">
        <Field label="Workflow" required>
          <ResourceSelect plural="workflows" value={step.workflow as string} onChange={(v) => set({ workflow: v })} placeholder="Choose a workflow" />
        </Field>
        {step.workflow ? (
          <div>
            <div className="mb-2 text-[11px] font-semibold tracking-wider text-faint uppercase">Inputs</div>
            <ParamValues schema={schema} value={(step.with as Record<string, unknown>) ?? {}} onChange={(v) => set({ with: v }, `step:${step.id}:with`)} />
          </div>
        ) : null}
        <Field label="For each" hint="optional" description="Expression producing a list; runs once per item with `item` and `index`.">
          <ExpressionInput value={str(step.for_each)} onChange={(v) => set({ for_each: v }, `step:${step.id}:for_each`)} placeholder="{{ steps.list.output.items }}" />
        </Field>
        {step.for_each ? (
          <Field label="Concurrency">
            <Input type="number" min={1} value={str(step.concurrency) || "5"} onChange={(e) => set({ concurrency: Number(e.target.value) || undefined })} />
          </Field>
        ) : null}
      </div>
    </Section>
  );
}

/** Any other step type (decide, grade, plugin steps): rendered from the server's JSON schema. */
function GenericEditor({ step, set }: { step: StepDoc; set: SetFn }) {
  const { data: root } = useStepSchema();
  const schema = useMemo(() => {
    if (!root) return undefined;
    const variants = (root.oneOf ?? []).map((v) => {
      const name = v.$ref?.split("/").pop();
      return name ? root.$defs?.[name] : v;
    });
    return variants.find((v) => {
      const t = v?.properties?.type;
      return t && (t.const === step.type || (t.enum as unknown[] | undefined)?.includes(step.type) || t.default === step.type);
    });
  }, [root, step.type]);
  if (!schema || !root) return <Section title="Settings"><p className="text-xs text-faint">Loading schema…</p></Section>;
  const { id: _i, type: _t, name: _n, needs: _d, if: _if, retry: _r, timeout: _to, on_error: _e, ...rest } = step;
  return (
    <Section title="Settings">
      <SchemaForm
        schema={schema}
        root={root}
        value={rest}
        hidden={["id", "type", "name", "needs", "if", "retry", "timeout", "on_error"]}
        collapseOptional
        stringWidget={({ value, onChange, name }) => (
          <ExpressionInput
            value={str(value)}
            onChange={(v) => onChange(v || undefined)}
            multiline={/instructions|state|subject|prompt|message/.test(name)}
          />
        )}
        onChange={(v) => {
          const next: Record<string, unknown> = {};
          for (const k of Object.keys(rest)) if (!(k in v)) next[k] = undefined;
          set({ ...next, ...v }, `step:${step.id}:generic`);
        }}
      />
    </Section>
  );
}
