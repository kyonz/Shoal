/**
 * `{{ }}` expression support: what can be referenced from a step, and a
 * CodeMirror completion source built from it.
 */
import type { Completion, CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import type { IntegrationCatalog, JsonSchema } from "@/api/hooks";
import { ancestors, dependencies } from "./graph";
import type { StepDoc, WorkflowSpecDoc } from "./types";

export interface ExprVar {
  label: string;
  detail?: string;
  info?: string;
  type?: "variable" | "property" | "function" | "keyword";
}

const STATIC_OUTPUTS: Record<string, string[]> = {
  script: ["exit_code", "stdout", "stderr"],
  wait: ["satisfied", "timed_out", "result"],
  approval: ["approved", "by", "comment"],
  decide: ["answers", "confidence", "low_confidence"],
  grade: ["score", "level", "passed", "reason"],
  workflow: ["outputs", "runs"],
};

function schemaProps(schema: JsonSchema | null | undefined): string[] {
  return schema?.properties ? Object.keys(schema.properties) : [];
}

export function stepOutputKeys(step: StepDoc, integrations: IntegrationCatalog[] = []): string[] {
  switch (step.type) {
    case "agent":
      return [...Object.keys((step.outputs as object) ?? {}), "summary"];
    case "set":
      return Object.keys((step.values as object) ?? {});
    case "action": {
      const [key, name] = String(step.action ?? "").split(".");
      const action = integrations.find((i) => i.key === key)?.actions.find((a) => a.name === name);
      return schemaProps(action?.output_schema);
    }
    case "wait":
      return step.signal ? ["received", "timed_out", "payload", "name", "source", "sender", "received_at"] : STATIC_OUTPUTS.wait;
    case "decide":
      return [...STATIC_OUTPUTS.decide, ...Object.keys((step.questions as object) ?? {}).map((q) => `answers.${q}`)];
    default:
      return STATIC_OUTPUTS[step.type] ?? [];
  }
}

/** Everything an expression in `stepId` may reference (upstream steps only). */
export function expressionVars(
  spec: WorkflowSpecDoc,
  stepId: string | null,
  { integrations = [], secrets = [] }: { integrations?: IntegrationCatalog[]; secrets?: string[] } = {},
): ExprVar[] {
  const vars: ExprVar[] = [];
  for (const [name, p] of Object.entries(spec.inputs ?? {})) {
    vars.push({ label: `inputs.${name}`, detail: p.type ?? "string", info: p.description, type: "variable" });
  }
  const deps = dependencies(spec.steps);
  const upstream = stepId ? ancestors(deps, stepId) : new Set(spec.steps.map((s) => s.id));
  for (const s of spec.steps) {
    if (!upstream.has(s.id)) continue;
    vars.push({ label: `steps.${s.id}.status`, detail: "succeeded | failed | skipped", type: "property" });
    vars.push({ label: `steps.${s.id}.output`, detail: s.type, type: "property" });
    for (const k of stepOutputKeys(s, integrations)) {
      vars.push({ label: `steps.${s.id}.output.${k}`, detail: s.type, type: "property" });
    }
    vars.push({ label: `steps.${s.id}.error.message`, detail: "error", type: "property" });
  }
  for (const v of ["run.id", "run.url", "run.namespace", "run.workflow", "run.initiator", "run.signal_url"]) vars.push({ label: v, type: "variable" });
  vars.push({ label: "trigger.event", detail: "trigger payload", type: "variable" });
  vars.push({ label: "campaign", detail: "campaign context", type: "variable" });
  vars.push({ label: "target", detail: "campaign target", type: "variable" });
  for (const s of secrets) vars.push({ label: `secrets.${s}`, detail: "secret (redacted)", type: "variable" });
  return vars;
}

const FILTERS = ["lower", "upper", "trim", "default('')", "tojson", "length", "join(', ')", "replace('a', 'b')", "first", "last", "int", "string"];

/** Is the cursor inside an unclosed `{{`? */
function insideExpression(text: string): boolean {
  const open = text.lastIndexOf("{{");
  return open !== -1 && text.indexOf("}}", open) === -1;
}

export function expressionCompletions(getVars: () => ExprVar[]) {
  return (ctx: CompletionContext): CompletionResult | null => {
    const line = ctx.state.doc.lineAt(ctx.pos);
    const before = line.text.slice(0, ctx.pos - line.from);
    if (!insideExpression(before)) {
      // Offer to open an expression after typing "{{"
      return null;
    }
    const pipe = ctx.matchBefore(/\|\s*\w*/);
    if (pipe) {
      const word = ctx.matchBefore(/\w*/)!;
      return {
        from: word.from,
        options: FILTERS.map((f) => ({ label: f, type: "function", detail: "filter" })),
        validFor: /^\w*$/,
      };
    }
    const word = ctx.matchBefore(/[\w.]*/);
    if (!word || (word.from === word.to && !ctx.explicit && !/\{\{\s*$/.test(before))) return null;
    const options: Completion[] = getVars().map((v) => ({
      label: v.label,
      detail: v.detail,
      info: v.info,
      type: v.type,
      boost: v.label.startsWith("inputs.") ? 2 : v.label.startsWith("steps.") ? 1 : 0,
    }));
    return { from: word.from, options, validFor: /^[\w.]*$/ };
  };
}

/** `steps.<id>` references in any value (for validation). */
export function referencedSteps(value: unknown, out = new Set<string>()): Set<string> {
  if (typeof value === "string") {
    for (const m of value.matchAll(/\{\{([^}]*)\}\}/g)) {
      for (const r of m[1].matchAll(/\bsteps\.([a-zA-Z_][a-zA-Z0-9_]*)/g)) out.add(r[1]);
    }
  } else if (Array.isArray(value)) value.forEach((v) => referencedSteps(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => referencedSteps(v, out));
  return out;
}

export function referencedInputs(value: unknown, out = new Set<string>()): Set<string> {
  if (typeof value === "string") {
    for (const m of value.matchAll(/\{\{([^}]*)\}\}/g)) {
      for (const r of m[1].matchAll(/\binputs\.([a-zA-Z_][a-zA-Z0-9_]*)/g)) out.add(r[1]);
    }
  } else if (Array.isArray(value)) value.forEach((v) => referencedInputs(v, out));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => referencedInputs(v, out));
  return out;
}
