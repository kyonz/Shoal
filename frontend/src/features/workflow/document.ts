/** Workflow <-> YAML document helpers. */
import { parse, stringify } from "yaml";
import type { StepDoc, WorkflowSpecDoc } from "./types";

export interface WorkflowDocument {
  apiVersion: "awm.dev/v1";
  kind: "Workflow";
  metadata: { name: string; description?: string; labels?: Record<string, string> };
  spec: WorkflowSpecDoc;
}

export function toDocument(name: string, description: string, labels: Record<string, string>, spec: WorkflowSpecDoc): WorkflowDocument {
  const metadata: WorkflowDocument["metadata"] = { name: name || "untitled" };
  if (description) metadata.description = description;
  if (Object.keys(labels).length) metadata.labels = labels;
  return { apiVersion: "awm.dev/v1", kind: "Workflow", metadata, spec };
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
      apiVersion: "awm.dev/v1",
      kind: "Workflow",
      metadata: { name: String(doc.metadata?.name ?? ""), description: doc.metadata?.description, labels: doc.metadata?.labels },
      spec,
    },
  };
}

const DEFAULT_RETRY = { max_attempts: 1, backoff: "10s", backoff_factor: 2, max_backoff: "1h" };

/** Remove server-filled defaults so version diffs show real changes only. */
export function normalizeForDiff(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeForDiff);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (k === "retry" && JSON.stringify(v) === JSON.stringify(DEFAULT_RETRY)) continue;
      if (k === "on_error" && v === "fail") continue;
      if (k === "fail_on_error" && v === true) continue;
      if (["created_at", "updated_at", "version", "updated_by", "namespace", "annotations"].includes(k)) continue;
      const n = normalizeForDiff(v);
      if (n && typeof n === "object" && !Array.isArray(n) && !Object.keys(n).length) continue;
      if (Array.isArray(n) && !n.length) continue;
      out[k] = n;
    }
    return out;
  }
  return value;
}
