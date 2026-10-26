/** Workflow <-> YAML document helpers. */
import { parse, stringify } from "yaml";
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

/** Parse YAML from the code view; only well-formed workflow documents are accepted. */
export function parseWorkflowYaml(text: string): ParsedWorkflow {
  let raw: unknown;
  try {
    raw = parse(text, { prettyErrors: true });
  } catch (e) {
    const err = e as { message: string; linePos?: { line: number }[] };
    return { ok: false, error: err.message.split("\n")[0], line: err.linePos?.[0]?.line };
  }
  if (!raw || typeof raw !== "object") return { ok: false, error: "Expected a Workflow document" };
  const doc = raw as Partial<WorkflowDocument>;
  if (doc.kind && doc.kind !== "Workflow") return { ok: false, error: `kind must be Workflow (got ${doc.kind})` };
  const spec = doc.spec as WorkflowSpecDoc | undefined;
  if (!spec || typeof spec !== "object") return { ok: false, error: "Missing spec" };
  if (!Array.isArray(spec.steps)) return { ok: false, error: "spec.steps must be a list" };
  for (const [i, s] of spec.steps.entries()) {
    if (!s || typeof s !== "object") return { ok: false, error: `steps[${i}] must be a mapping` };
    const step = s as StepDoc;
    if (!step.id || typeof step.id !== "string") return { ok: false, error: `steps[${i}] needs an id` };
    if (!step.type || typeof step.type !== "string") return { ok: false, error: `step '${step.id}' needs a type` };
  }
  return {
    ok: true,
    doc: {
      apiVersion: "shoal.dev/v1",
      kind: "Workflow",
      metadata: { name: String(doc.metadata?.name ?? ""), description: doc.metadata?.description, labels: doc.metadata?.labels },
      spec,
    },
  };
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

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

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
