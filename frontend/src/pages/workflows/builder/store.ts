/**
 * Builder state: the workflow document is the single source of truth; the
 * canvas, inspector and YAML view are projections of it. Every change goes
 * through `commit`, which records undo history. Rapid edits to the same field
 * (typing) are coalesced into one undo step.
 */
import { create } from "zustand";
import {
  addStep,
  autoLayout,
  connect,
  disconnect,
  pruneLayout,
  removeSteps,
  renameStep,
  reorder,
  replaceStep,
  setNeeds,
  uniqueId,
} from "@/features/workflow/graph";
import { stepType } from "@/features/workflow/stepTypes";
import type { Layout, Position, StepDoc, WorkflowSpecDoc } from "@/features/workflow/types";

export interface Meta {
  name: string;
  description: string;
  labels: Record<string, string>;
}

interface Snapshot {
  spec: WorkflowSpecDoc;
  layout: Layout;
  meta: Meta;
}

interface BuilderState extends Snapshot {
  selected: string | null;
  past: Snapshot[];
  future: Snapshot[];
  /** Coalescing key of the last commit and when it happened. */
  lastKey: string | null;
  lastAt: number;
  /** Snapshot of what is saved on the server, for the dirty check. */
  saved: string;

  load: (s: Snapshot) => void;
  markSaved: () => void;
  commit: (next: Partial<Snapshot>, key?: string) => void;
  select: (id: string | null) => void;
  undo: () => void;
  redo: () => void;

  updateStep: (id: string, step: StepDoc, key?: string) => void;
  rename: (from: string, to: string) => void;
  add: (type: string, preset?: Partial<StepDoc>, at?: Position, after?: string | null) => string;
  duplicate: (id: string) => void;
  remove: (ids: string[]) => void;
  link: (from: string, to: string) => void;
  unlink: (from: string, to: string) => void;
  setDeps: (id: string, needs: string[]) => void;
  moveNodes: (positions: Layout) => void;
  relayout: () => void;
  setSpec: (spec: WorkflowSpecDoc, key?: string) => void;
  setMeta: (meta: Partial<Meta>, key?: string) => void;
}

const HISTORY = 200;
const COALESCE_MS = 900;

export function snapshotKey(s: Snapshot): string {
  return JSON.stringify([s.spec, s.meta, s.layout]);
}

export const useBuilder = create<BuilderState>((set, get) => ({
  spec: { steps: [] },
  layout: {},
  meta: { name: "", description: "", labels: {} },
  selected: null,
  past: [],
  future: [],
  lastKey: null,
  lastAt: 0,
  saved: "",

  load: (s) => {
    const layout = autoLayout(s.spec.steps, pruneLayout(s.layout, s.spec.steps));
    set({ ...s, layout, past: [], future: [], selected: null, lastKey: null, saved: snapshotKey({ ...s, layout }) });
  },

  markSaved: () => set((st) => ({ saved: snapshotKey(st) })),

  commit: (next, key) => {
    const st = get();
    const now = Date.now();
    const coalesce = !!key && key === st.lastKey && now - st.lastAt < COALESCE_MS;
    const prev: Snapshot = { spec: st.spec, layout: st.layout, meta: st.meta };
    set({
      ...next,
      past: coalesce ? st.past : [...st.past.slice(-HISTORY + 1), prev],
      future: [],
      lastKey: key ?? null,
      lastAt: now,
    });
  },

  select: (id) => set({ selected: id }),

  undo: () => {
    const st = get();
    const prev = st.past[st.past.length - 1];
    if (!prev) return;
    set({
      ...prev,
      past: st.past.slice(0, -1),
      future: [{ spec: st.spec, layout: st.layout, meta: st.meta }, ...st.future],
      lastKey: null,
      selected: st.selected && prev.spec.steps.some((s) => s.id === st.selected) ? st.selected : null,
    });
  },

  redo: () => {
    const st = get();
    const next = st.future[0];
    if (!next) return;
    set({
      ...next,
      future: st.future.slice(1),
      past: [...st.past, { spec: st.spec, layout: st.layout, meta: st.meta }],
      lastKey: null,
    });
  },

  updateStep: (id, step, key) => {
    const { spec, commit } = get();
    commit({ spec: replaceStep(spec, id, step) }, key ?? `step:${id}`);
  },

  rename: (from, to) => {
    const { spec, layout, selected, commit } = get();
    if (!to || from === to || spec.steps.some((s) => s.id === to)) return;
    const nextLayout = { ...layout, [to]: layout[from] };
    delete nextLayout[from];
    commit({ spec: renameStep(spec, from, to), layout: nextLayout }, `rename:${from}`);
    if (selected === from) set({ selected: to });
  },

  add: (type, preset = {}, at, after) => {
    const { spec, layout, commit } = get();
    const info = stepType(type);
    const base = preset.id ?? (type === "action" && typeof preset.action === "string" ? preset.action.split(".")[1] : type);
    const id = uniqueId(spec, base);
    const step: StepDoc = { type, ...info.defaults(), ...preset, id };
    let pos = at;
    if (!pos && after && layout[after]) pos = { x: layout[after].x, y: layout[after].y + 150 };
    if (!pos) {
      const ys = Object.values(layout).map((p) => p.y);
      pos = { x: 80, y: ys.length ? Math.max(...ys) + 150 : 80 };
    }
    const nextLayout = { ...layout, [id]: pos };
    commit({ spec: addStep(spec, step, nextLayout, after), layout: nextLayout });
    set({ selected: id });
    return id;
  },

  duplicate: (id) => {
    const { spec, layout, add } = get();
    const step = spec.steps.find((s) => s.id === id);
    if (!step) return;
    const { id: _id, needs: _needs, ...rest } = step;
    const p = layout[id] ?? { x: 0, y: 0 };
    add(step.type, { ...structuredClone(rest), id: uniqueId(spec, `${id}_copy`) }, { x: p.x + 40, y: p.y + 40 });
  },

  remove: (ids) => {
    const { spec, layout, selected, commit } = get();
    if (!ids.length) return;
    const nextLayout = { ...layout };
    for (const id of ids) delete nextLayout[id];
    commit({ spec: removeSteps(spec, ids, nextLayout), layout: nextLayout });
    if (selected && ids.includes(selected)) set({ selected: null });
  },

  link: (from, to) => {
    const { spec, layout, commit } = get();
    const next = connect(spec, from, to, layout);
    if (next !== spec) commit({ spec: next });
  },

  unlink: (from, to) => {
    const { spec, layout, commit } = get();
    commit({ spec: disconnect(spec, from, to, layout) });
  },

  setDeps: (id, needs) => {
    const { spec, layout, commit } = get();
    commit({ spec: setNeeds(spec, id, needs, layout) });
  },

  moveNodes: (positions) => {
    const { spec, layout, commit } = get();
    const nextLayout = { ...layout, ...positions };
    commit({ layout: nextLayout, spec: reorder(spec, nextLayout) });
  },

  relayout: () => {
    const { spec, commit } = get();
    const layout = autoLayout(spec.steps, {}, true);
    commit({ layout, spec: reorder(spec, layout) });
  },

  setSpec: (spec, key) => {
    const { layout, commit, selected } = get();
    const nextLayout = autoLayout(spec.steps, pruneLayout(layout, spec.steps));
    commit({ spec, layout: nextLayout }, key);
    if (selected && !spec.steps.some((s) => s.id === selected)) set({ selected: null });
  },

  setMeta: (meta, key) => {
    const { meta: cur, commit } = get();
    commit({ meta: { ...cur, ...meta } }, key ?? "meta");
  },
}));

export function isDirty(s: Pick<BuilderState, "spec" | "layout" | "meta" | "saved">): boolean {
  return snapshotKey(s) !== s.saved;
}

export function useDirty(): boolean {
  return useBuilder(isDirty);
}
