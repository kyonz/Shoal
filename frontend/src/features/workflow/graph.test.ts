import { describe, expect, it } from "vitest";
import { addStep, connect, decodeLayout, dependencies, disconnect, encodeLayout, rebuild, removeSteps, renameStep, topoOrder } from "./graph";
import type { StepDoc, WorkflowSpecDoc } from "./types";

const s = (id: string, extra: Partial<StepDoc> = {}): StepDoc => ({ id, type: "set", values: {}, ...extra });
const spec = (steps: StepDoc[]): WorkflowSpecDoc => ({ steps });
const deps = (w: WorkflowSpecDoc) => Object.fromEntries(dependencies(w.steps));

describe("dependencies", () => {
  it("defaults to the previous step", () => {
    expect(deps(spec([s("a"), s("b"), s("c", { needs: ["a"] }), s("d", { needs: [] })]))).toEqual({
      a: [],
      b: ["a"],
      c: ["a"],
      d: [],
    });
  });
});

describe("rebuild", () => {
  it("omits needs that equal the implicit previous step", () => {
    const w = spec([s("a"), s("b", { needs: ["a"] }), s("c", { needs: ["b"] })]);
    const out = rebuild(w.steps, dependencies(w.steps));
    expect(out.map((x) => x.needs)).toEqual([undefined, undefined, undefined]);
  });

  it("keeps semantics when the order changes", () => {
    const w = spec([s("b", { needs: ["a"] }), s("a", { needs: [] })]);
    const out = rebuild(w.steps, dependencies(w.steps));
    expect(out.map((x) => x.id)).toEqual(["a", "b"]);
    expect(deps(spec(out))).toEqual({ a: [], b: ["a"] });
  });

  it("orders ready steps by canvas position", () => {
    const w = spec([s("a"), s("x", { needs: ["a"] }), s("y", { needs: ["a"] })]);
    const order = topoOrder(w.steps, dependencies(w.steps), { a: { x: 0, y: 0 }, x: { x: 0, y: 300 }, y: { x: 0, y: 150 } });
    expect(order.map((x) => x.id)).toEqual(["a", "y", "x"]);
  });
});

describe("edits", () => {
  it("connect adds a dependency and refuses cycles", () => {
    const w = spec([s("a"), s("b", { needs: [] })]);
    const c = connect(w, "a", "b", {});
    expect(deps(c)).toEqual({ a: [], b: ["a"] });
    expect(c.steps[1].needs).toBeUndefined();
    expect(connect(c, "b", "a", {})).toBe(c);
  });

  it("disconnect makes a step independent", () => {
    const w = spec([s("a"), s("b")]);
    const d = disconnect(w, "a", "b", {});
    expect(deps(d)).toEqual({ a: [], b: [] });
  });

  it("addStep without a parent runs in parallel", () => {
    const w = addStep(spec([s("a"), s("b")]), s("c"), {});
    expect(deps(w)).toEqual({ a: [], b: ["a"], c: [] });
  });

  it("removeSteps reconnects dependants to the removed step's parents", () => {
    const w = removeSteps(spec([s("a"), s("b"), s("c")]), ["b"], {});
    expect(deps(w)).toEqual({ a: [], c: ["a"] });
  });

  it("renameStep updates needs and expressions", () => {
    const w = spec([s("fetch"), s("fix", { needs: ["fetch"], prompt: "{{ steps.fetch.output.summary }} {{ steps.fetcher.x }}" })]);
    const r = renameStep({ ...w, outputs: { o: "{{ steps.fetch.output.url }}" } }, "fetch", "issue");
    expect(r.steps[0].id).toBe("issue");
    expect(r.steps[1].needs).toEqual(["issue"]);
    expect(r.steps[1].prompt).toBe("{{ steps.issue.output.summary }} {{ steps.fetcher.x }}");
    expect(r.outputs).toEqual({ o: "{{ steps.issue.output.url }}" });
  });
});

describe("layout encoding", () => {
  it("round-trips", () => {
    const l = { a: { x: 10.4, y: 20 }, b: { x: -5, y: 0 } };
    expect(decodeLayout(encodeLayout(l))).toEqual({ a: { x: 10, y: 20 }, b: { x: -5, y: 0 } });
    expect(decodeLayout("not json")).toEqual({});
  });
});
