/** Workflow <-> YAML document helpers. */
import { parse, stringify } from "yaml";
import * as shape from "@/lib/shape";
import { errorMessage, isRecord } from "@/lib/shape";
import type { StepDoc, WorkflowSpecDoc } from "./types";

export interface WorkflowDocument {
  apiVersion: "shoal.dev/v1";
  kind: "Workflow";
  metadata: { name: string; description?: string; labels?: Record<string, string> };
  spec: WorkflowSpecDoc;
}

export function toDocument(name: string, description: string, labels: Record<string, string>, spec: WorkflowSpecDoc): WorkflowDocument {
  const metadata: WorkflowDocument["metadata"] = { name: name || "untitled" };
  if (description) metadata.description = description;
  if (Object.keys(labels).length) metadata.labels = labels;
  return { apiVersion: "shoal.dev/v1", kind: "Workflow", metadata, spec };
}

export function toYaml(doc: unknown): string {
  return stringify(doc, { lineWidth: 0, indent: 2, aliasDuplicateObjects: false });
}

export type ParsedWorkflow =
  | { ok: true; doc: WorkflowDocument }
  | { ok: false; error: string; line?: number };

/**
 * Parse YAML from the code view; only well-formed workflow documents are
 * accepted. Beyond YAML syntax this checks the shape of everything the canvas,
 * inspector and settings render from (lists are lists, mappings are mappings,
 * step ids are unique), so a half-typed document is reported, never applied.
 */
export function parseWorkflowYaml(text: string): ParsedWorkflow {
  let raw: unknown;
  try {
    raw = parse(text, { prettyErrors: true });
  } catch (e) {
    const err = e as { message: string; linePos?: { line: number }[] };
    return { ok: false, error: err.message.split("\n")[0], line: err.linePos?.[0]?.line };
  }
  if (!isRecord(raw)) return { ok: false, error: "Expected a Workflow document" };
  try {
    JSON.stringify(raw);
  } catch {
    return { ok: false, error: "The document refers to itself (a YAML alias inside its own anchor)" };
  }
  const doc = raw as Partial<WorkflowDocument>;
  if (doc.kind && doc.kind !== "Workflow") return { ok: false, error: `kind must be Workflow (got ${doc.kind})` };
  if (!isRecord(doc.spec)) return { ok: false, error: "Missing spec" };
  const spec = doc.spec as WorkflowSpecDoc;
  if (!Array.isArray(spec.steps)) return { ok: false, error: "spec.steps must be a list" };
  const seen = new Set<string>();
  for (const [i, s] of spec.steps.entries()) {
    if (!isRecord(s)) return { ok: false, error: `steps[${i}] must be a mapping` };
    const step = s as StepDoc;
    if (!step.id || typeof step.id !== "string") return { ok: false, error: `steps[${i}] needs an id` };
    if (!step.type || typeof step.type !== "string") return { ok: false, error: `step '${step.id}' needs a type` };
    if (seen.has(step.id)) return { ok: false, error: `Two steps have the id '${step.id}'` };
    seen.add(step.id);
  }
  try {
    checkShape(doc.metadata, spec);
  } catch (e) {
    return { ok: false, error: errorMessage(e) };
  }
  const metadata = (doc.metadata ?? {}) as Partial<WorkflowDocument["metadata"]>;
  return {
    ok: true,
    doc: {
      apiVersion: "shoal.dev/v1",
      kind: "Workflow",
      metadata: { name: String(metadata.name ?? ""), description: metadata.description, labels: metadata.labels },
      spec,
    },
  };
}

function checkShape(metadata: unknown, spec: WorkflowSpecDoc) {
  const md = shape.record(metadata, "metadata", true);
  if (md) {
    shape.scalar(md.name, "metadata.name", true);
    shape.string(md.description, "metadata.description", true);
    shape.stringMap(md.labels, "metadata.labels", true);
  }
  shape.string(spec.description, "spec.description", true);
  // The usual half-typed state: an input named but not described yet.
  for (const [k, p] of Object.entries(shape.record(spec.inputs, "spec.inputs", true) ?? {})) {
    if (p == null) throw new shape.ShapeError(`Input '${k}' needs its settings, e.g. type: string`);
  }
  const inputs = shape.recordMap(spec.inputs, "spec.inputs", true);
  for (const [k, p] of Object.entries(inputs ?? {})) checkParam(p, `spec.inputs.${k}`);
  shape.record(spec.outputs, "spec.outputs", true);
  shape.stringMap(spec.connections, "spec.connections", true);
  const defaults = shape.record(spec.defaults, "spec.defaults", true);
  if (defaults) {
    shape.string(defaults.agent, "spec.defaults.agent", true);
    shape.string(defaults.sandbox, "spec.defaults.sandbox", true);
  }
  shape.scalar(spec.timeout, "spec.timeout", true);
  for (const step of spec.steps) {
    const at = `step '${step.id}'`;
    shape.stringList(step.needs, `${at} needs`, true);
    shape.scalar(step.name, `${at} name`, true);
    shape.string(step.if, `${at} if`, true);
    shape.record(step.retry, `${at} retry`, true);
    shape.scalar(step.timeout, `${at} timeout`, true);
    // Mappings the inspector edits key by key.
    for (const key of ["inputs", "values", "env", "with", "questions", "signal"]) shape.record(step[key], `${at} ${key}`, true);
    const outputs = shape.record(step.outputs, `${at} outputs`, true);
    if (outputs && (step.type === "agent" || step.type === "script")) for (const [k, p] of Object.entries(outputs)) checkParam(shape.record(p, `${at} outputs.${k}`)!, `${at} outputs.${k}`);
    for (const key of ["skills", "tools"]) if (step[key] !== undefined) shape.list(step[key], `${at} ${key}`);
  }
}

function checkParam(p: Record<string, unknown>, path: string) {
  shape.string(p.type, `${path}.type`, true);
  shape.string(p.description, `${path}.description`, true);
  shape.boolean(p.required, `${path}.required`, true);
  shape.list(p.enum ?? undefined, `${path}.enum`, true);
}

const DEFAULT_RETRY = { max_attempts: 1, backoff: "10s", backoff_factor: 2, max_backoff: "1h" };
const STEP_DEFAULTS: [string, (v: unknown) => boolean][] = [
  ["retry", (v) => JSON.stringify(v) === JSON.stringify(DEFAULT_RETRY)],
  ["on_error", (v) => v === "fail"],
  ["fail_on_error", (v) => v === true],
];

/**
 * Drop the step defaults the server fills in, for versions saved before the
 * API kept what was written. Only those exact keys on steps are touched:
 * everything else (including empty lists such as `needs: []`) is meaningful.
 */
export function stripServerDefaults(spec: WorkflowSpecDoc): WorkflowSpecDoc {
  const steps = (spec.steps ?? []).map((step) => {
    const out: StepDoc = { ...step };
    for (const [key, isDefault] of STEP_DEFAULTS) if (key in out && isDefault(out[key])) delete out[key];
    return out;
  });
  return { ...spec, steps };
}

/**
 * `value` with its object keys in the order `like` uses (unknown keys last), and
 * list items matched by `id` where they have one, so a diff against `like`
 * shows changes rather than differences in key order.
 */
export function alignKeys(value: unknown, like: unknown): unknown {
  if (Array.isArray(value)) {
    const other = Array.isArray(like) ? like : [];
    return value.map((item, i) => {
      const id = isRecord(item) ? item.id : undefined;
      const match = id !== undefined ? other.find((o) => isRecord(o) && o.id === id) : other[i];
      return alignKeys(item, match);
    });
  }
  if (!isRecord(value)) return value;
  const ref = isRecord(like) ? like : {};
  const keys = [...Object.keys(ref).filter((k) => k in value), ...Object.keys(value).filter((k) => !(k in ref))];
  return Object.fromEntries(keys.map((k) => [k, alignKeys(value[k], ref[k])]));
}
