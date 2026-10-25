/**
 * Instant client-side checks while editing. The server's validation (a dry-run
 * apply) stays authoritative and is shown alongside these.
 */
import type { IntegrationCatalog } from "@/api/hooks";
import { ancestors, dependencies } from "./graph";
import { referencedInputs, referencedSteps } from "./expressions";
import { STEP_ID, type StepDoc, type WorkflowSpecDoc } from "./types";

export interface Issue {
  stepId: string | null;
  level: "error" | "warning";
  message: string;
  field?: string;
  source?: "client" | "server";
}

const blank = (v: unknown) => v == null || (typeof v === "string" && !v.trim()) || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v as object).length);

function required(step: StepDoc): [string, string][] {
  switch (step.type) {
    case "agent":
      return [["prompt", "Write a prompt for the agent"]];
    case "action":
      return [["action", "Choose an integration action"]];
    case "script":
      return step.run !== undefined && step.code === undefined ? [["run", "Add a command to run"]] : [["code", "Write the script's code"]];
    case "approval":
      return [["message", "Add a message for the approver"]];
    case "workflow":
      return [["workflow", "Choose the workflow to call"]];
    case "set":
      return [["values", "Add at least one value"]];
    case "decide":
      return [["state", "Say what the questions are about"], ["questions", "Add at least one question"]];
    case "grade":
      return [["subject", "Choose what to grade"], ["instructions", "Describe the rubric"]];
    default:
      return [];
  }
}

export function validateWorkflow(
  spec: WorkflowSpecDoc,
  ctx: { integrations?: IntegrationCatalog[]; agents?: string[]; workflows?: string[] } = {},
): Issue[] {
  const issues: Issue[] = [];
  const ids = spec.steps.map((s) => s.id);
  const known = new Set(ids);
  const deps = dependencies(spec.steps);
  const inputs = new Set(Object.keys(spec.inputs ?? {}));

  if (!spec.steps.length) issues.push({ stepId: null, level: "error", message: "Add at least one step" });

  for (const step of spec.steps) {
    const at = (message: string, level: Issue["level"] = "error", field?: string) =>
      issues.push({ stepId: step.id, level, message, field, source: "client" });

    if (!STEP_ID.test(step.id)) at("Step ids use letters, digits and _ and cannot start with a digit", "error", "id");
    if (ids.filter((i) => i === step.id).length > 1) at(`Duplicate step id '${step.id}'`, "error", "id");
    for (const [field, msg] of required(step)) if (blank(step[field])) at(msg, "error", field);

    if (step.type === "wait" && !step.duration && !step.until && !step.condition) at("Wait needs a duration, a time or a condition");

    for (const n of step.needs ?? []) if (!known.has(n)) at(`Depends on unknown step '${n}'`, "error", "needs");

    const upstream = ancestors(deps, step.id);
    for (const ref of referencedSteps(step)) {
      if (ref === step.id) continue;
      if (!known.has(ref)) at(`References unknown step '${ref}'`, "error");
      else if (!upstream.has(ref)) at(`Uses '${ref}' which may not have run yet — connect it upstream`, "warning");
    }
    for (const ref of referencedInputs(step)) if (!inputs.has(ref)) at(`References undeclared input '${ref}'`, "warning");

    if (step.type === "action" && step.action && ctx.integrations?.length) {
      const [key, name] = String(step.action).split(".");
      const integration = ctx.integrations.find((i) => i.key === key);
      if (!integration) at(`Integration '${key}' is not installed`, "warning", "action");
      else if (!integration.actions.some((a) => a.name === name)) at(`'${key}' has no action '${name}'`, "error", "action");
    }
    if (step.type === "agent" && ctx.agents) {
      const agent = (step.agent as string) || spec.defaults?.agent;
      if (!agent) at("Choose an agent, or set a default agent in workflow settings", "error", "agent");
      else if (!ctx.agents.includes(agent)) at(`Agent '${agent}' does not exist in this namespace`, "warning", "agent");
    }
    if (step.type === "workflow" && ctx.workflows && step.workflow && !ctx.workflows.includes(String(step.workflow))) {
      at(`Workflow '${step.workflow}' does not exist in this namespace`, "warning", "workflow");
    }
  }
  for (const ref of referencedSteps(spec.outputs ?? {})) {
    if (!known.has(ref)) issues.push({ stepId: null, level: "error", message: `Output references unknown step '${ref}'`, source: "client" });
  }
  return issues;
}

/** Map a server validation message (pydantic loc) to a step id. */
export function serverIssue(spec: WorkflowSpecDoc, message: string): Issue {
  const m = message.match(/spec\.steps\.(\d+)/) ?? message.match(/steps[.\[](\d+)/);
  const idx = m ? Number(m[1]) : NaN;
  const stepId = Number.isFinite(idx) ? spec.steps[idx]?.id ?? null : (message.match(/step '([^']+)'/)?.[1] ?? null);
  return { stepId, level: "error", message: message.replace(/^\('?spec'?,?\s*/, ""), source: "server" };
}
