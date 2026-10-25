/**
 * Pure functions between the workflow document and the canvas graph.
 *
 * Engine semantics (awm.core.models.WorkflowSpec.dependencies): a step's
 * dependencies are its explicit `needs`, or else the previous step in the
 * list. Every structural edit therefore (1) materialises all dependencies,
 * (2) applies the change, (3) re-orders steps topologically (ties broken by
 * canvas position, top to bottom) and (4) writes `needs` back only where it
 * differs from the implicit "previous step", so YAML stays as terse as a
 * hand-written file.
 */
import dagre from "@dagrejs/dagre";
import type { Layout, Position, StepDoc, WorkflowSpecDoc } from "./types";

export type Deps = Map<string, string[]>;

/** Resolved dependencies of every step, exactly as the engine computes them. */
export function dependencies(steps: StepDoc[]): Deps {
  const out: Deps = new Map();
  let prev: string | null = null;
  for (const s of steps) {
    out.set(s.id, Array.isArray(s.needs) ? [...s.needs] : prev ? [prev] : []);
    prev = s.id;
  }
  return out;
}

/** Ids of every step that `id` (transitively) depends on. */
export function ancestors(deps: Deps, id: string): Set<string> {
  const seen = new Set<string>();
  const stack = [...(deps.get(id) ?? [])];
  while (stack.length) {
    const n = stack.pop()!;
    if (seen.has(n)) continue;
    seen.add(n);
    stack.push(...(deps.get(n) ?? []));
  }
  return seen;
}

export function wouldCycle(deps: Deps, from: string, to: string): boolean {
  // Adding "to needs from" cycles if `from` already depends on `to`.
  return from === to || ancestors(deps, from).has(to);
}

/**
 * Order steps so each comes after its dependencies; among ready steps, keep
 * the visual order (y, then x), falling back to the previous list order.
 */
export function topoOrder(steps: StepDoc[], deps: Deps, layout: Layout = {}): StepDoc[] {
  const index = new Map(steps.map((s, i) => [s.id, i]));
  const key = (s: StepDoc): [number, number, number] => {
    const p = layout[s.id];
    return p ? [Math.round(p.y / 10), p.x, index.get(s.id)!] : [Infinity, 0, index.get(s.id)!];
  };
  const cmp = (a: StepDoc, b: StepDoc) => {
    const ka = key(a);
    const kb = key(b);
    for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
    return 0;
  };
  const known = new Set(steps.map((s) => s.id));
  const remaining = new Map(steps.map((s) => [s.id, new Set((deps.get(s.id) ?? []).filter((d) => known.has(d)))]));
  const out: StepDoc[] = [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  while (remaining.size) {
    const ready = [...remaining.entries()].filter(([, d]) => d.size === 0).map(([id]) => byId.get(id)!);
    // A cycle (only possible in hand-edited YAML) keeps the original order for the rest.
    const pick = ready.length ? ready.sort(cmp)[0] : [...remaining.keys()].map((id) => byId.get(id)!).sort(cmp)[0];
    out.push(pick);
    remaining.delete(pick.id);
    for (const d of remaining.values()) d.delete(pick.id);
  }
  return out;
}

/** Write steps back in order with the minimal `needs` that keep `deps`. */
export function rebuild(steps: StepDoc[], deps: Deps, layout: Layout = {}): StepDoc[] {
  const ordered = topoOrder(steps, deps, layout);
  return ordered.map((s, i) => {
    const want = deps.get(s.id) ?? [];
    const implicit = i === 0 ? [] : [ordered[i - 1].id];
    const { needs: _drop, ...rest } = s;
    const same = want.length === implicit.length && want.every((d, j) => d === implicit[j]);
    return same ? (rest as StepDoc) : ({ ...rest, needs: want } as StepDoc);
  });
}

export function withSteps(spec: WorkflowSpecDoc, steps: StepDoc[]): WorkflowSpecDoc {
  return { ...spec, steps };
}

// --------------------------------------------------------------------------- edits

export function connect(spec: WorkflowSpecDoc, from: string, to: string, layout: Layout): WorkflowSpecDoc {
  const deps = dependencies(spec.steps);
  if (wouldCycle(deps, from, to)) return spec;
  const cur = deps.get(to) ?? [];
  if (cur.includes(from)) return spec;
  deps.set(to, [...cur, from]);
  return withSteps(spec, rebuild(spec.steps, deps, layout));
}

export function disconnect(spec: WorkflowSpecDoc, from: string, to: string, layout: Layout): WorkflowSpecDoc {
  const deps = dependencies(spec.steps);
  deps.set(to, (deps.get(to) ?? []).filter((d) => d !== from));
  return withSteps(spec, rebuild(spec.steps, deps, layout));
}

/** Set a step's dependencies explicitly (inspector). */
export function setNeeds(spec: WorkflowSpecDoc, id: string, needs: string[], layout: Layout): WorkflowSpecDoc {
  const deps = dependencies(spec.steps);
  deps.set(id, needs.filter((n) => n !== id && !ancestorsAfter(deps, id).has(n)));
  return withSteps(spec, rebuild(spec.steps, deps, layout));
}

function ancestorsAfter(deps: Deps, id: string): Set<string> {
  // steps that depend on `id` (descendants) - can't become its dependencies
  const out = new Set<string>();
  let grew = true;
  while (grew) {
    grew = false;
    for (const [n, ds] of deps) {
      if (out.has(n)) continue;
      if (ds.includes(id) || ds.some((d) => out.has(d))) {
        out.add(n);
        grew = true;
      }
    }
  }
  return out;
}

/** Add a step; with `after`, it depends on that step, otherwise on nothing. */
export function addStep(spec: WorkflowSpecDoc, step: StepDoc, layout: Layout, after?: string | null): WorkflowSpecDoc {
  const deps = dependencies(spec.steps);
  deps.set(step.id, after ? [after] : []);
  return withSteps(spec, rebuild([...spec.steps, step], deps, layout));
}

/** Remove steps; their dependants inherit the removed steps' dependencies. */
export function removeSteps(spec: WorkflowSpecDoc, ids: string[], layout: Layout): WorkflowSpecDoc {
  const gone = new Set(ids);
  const deps = dependencies(spec.steps);
  const resolve = (list: string[]): string[] => {
    const out: string[] = [];
    for (const d of list) {
      if (gone.has(d)) out.push(...resolve(deps.get(d) ?? []));
      else out.push(d);
    }
    return [...new Set(out)];
  };
  const next: Deps = new Map();
  for (const [id, list] of deps) if (!gone.has(id)) next.set(id, resolve(list));
  return withSteps(spec, rebuild(spec.steps.filter((s) => !gone.has(s.id)), next, layout));
}

/** Re-order after nodes moved, so YAML order follows the canvas. */
export function reorder(spec: WorkflowSpecDoc, layout: Layout): WorkflowSpecDoc {
  return withSteps(spec, rebuild(spec.steps, dependencies(spec.steps), layout));
}

export function replaceStep(spec: WorkflowSpecDoc, id: string, step: StepDoc): WorkflowSpecDoc {
  return withSteps(
    spec,
    spec.steps.map((s) => (s.id === id ? step : s)),
  );
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function renameRefs(value: unknown, from: string, to: string): unknown {
  if (typeof value === "string") {
    return value.replace(new RegExp(`\\bsteps\\.${escapeRe(from)}\\b`, "g"), `steps.${to}`);
  }
  if (Array.isArray(value)) return value.map((v) => renameRefs(v, from, to));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, renameRefs(v, from, to)]));
  }
  return value;
}

/** Rename a step and every reference to it (needs and `steps.<id>` expressions). */
export function renameStep(spec: WorkflowSpecDoc, from: string, to: string): WorkflowSpecDoc {
  if (from === to) return spec;
  const steps = spec.steps.map((s) => {
    const next = renameRefs(s, from, to) as StepDoc;
    if (s.id === from) next.id = to;
    if (Array.isArray(next.needs)) next.needs = next.needs.map((n) => (n === from ? to : n));
    return next;
  });
  return { ...spec, steps, outputs: renameRefs(spec.outputs, from, to) as WorkflowSpecDoc["outputs"] };
}

export function uniqueId(spec: WorkflowSpecDoc, base: string): string {
  const clean = base.replace(/[^a-zA-Z0-9_]/g, "_").replace(/^([0-9])/, "_$1") || "step";
  const taken = new Set(spec.steps.map((s) => s.id));
  if (!taken.has(clean)) return clean;
  for (let i = 2; ; i++) if (!taken.has(`${clean}_${i}`)) return `${clean}_${i}`;
}

// --------------------------------------------------------------------------- layout

export const NODE_W = 264;
export const NODE_H = 96;

/** Positions for every step: keep known ones, lay out the rest with dagre. */
export function autoLayout(steps: StepDoc[], keep: Layout = {}, force = false): Layout {
  const missing = steps.filter((s) => force || !keep[s.id]);
  if (!missing.length) return keep;
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 48, ranksep: 64, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const s of steps) g.setNode(s.id, { width: NODE_W, height: NODE_H });
  for (const [id, ds] of dependencies(steps)) for (const d of ds) if (g.hasNode(d)) g.setEdge(d, id);
  dagre.layout(g);
  const out: Layout = force ? {} : { ...keep };
  for (const s of steps) {
    if (!force && keep[s.id]) continue;
    const n = g.node(s.id) as { x: number; y: number };
    out[s.id] = { x: Math.round(n.x - NODE_W / 2), y: Math.round(n.y - NODE_H / 2) };
  }
  return out;
}

export function pruneLayout(layout: Layout, steps: StepDoc[]): Layout {
  const ids = new Set(steps.map((s) => s.id));
  return Object.fromEntries(Object.entries(layout).filter(([id]) => ids.has(id)));
}

/** Compact serialisation for the layout annotation: {"id":[x,y]}. */
export function encodeLayout(layout: Layout): string {
  return JSON.stringify(Object.fromEntries(Object.entries(layout).map(([id, p]) => [id, [Math.round(p.x), Math.round(p.y)]])));
}

export function decodeLayout(raw: string | undefined | null): Layout {
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw) as Record<string, [number, number] | Position>;
    return Object.fromEntries(
      Object.entries(obj).map(([id, p]) => [id, Array.isArray(p) ? { x: p[0], y: p[1] } : p]),
    );
  } catch {
    return {};
  }
}
