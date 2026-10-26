import { describe, expect, it } from "vitest";
import { alignKeys, stripServerDefaults } from "./document";

describe("stripServerDefaults", () => {
  it("drops only the step defaults the server fills in", () => {
    const spec = {
      steps: [
        { id: "a", type: "set", values: { version: "3", namespace: "x" }, retry: { max_attempts: 1, backoff: "10s", backoff_factor: 2, max_backoff: "1h" }, on_error: "fail" },
        { id: "b", type: "script", needs: [], code: "x", fail_on_error: true, inputs: {} },
        { id: "c", type: "set", values: {}, on_error: "continue", retry: { max_attempts: 3 } },
      ],
    };
    expect(stripServerDefaults(spec).steps).toEqual([
      { id: "a", type: "set", values: { version: "3", namespace: "x" } },
      { id: "b", type: "script", needs: [], code: "x", inputs: {} },
      { id: "c", type: "set", values: {}, on_error: "continue", retry: { max_attempts: 3 } },
    ]);
  });
});

describe("alignKeys", () => {
  it("orders keys like the reference and matches steps by id", () => {
    const value = { spec: { steps: [{ type: "set", id: "b" }, { values: {}, id: "a", type: "set" }] }, kind: "Workflow" };
    const like = { kind: "Workflow", spec: { steps: [{ id: "a", type: "set" }, { id: "b", type: "set", extra: 1 }] } };
    const out = alignKeys(value, like) as typeof value;
    expect(Object.keys(out)).toEqual(["kind", "spec"]);
    expect(Object.keys(out.spec.steps[0])).toEqual(["id", "type"]);
    expect(Object.keys(out.spec.steps[1])).toEqual(["id", "type", "values"]);
    expect(out.spec.steps.map((s) => s.id)).toEqual(["b", "a"]); // list order is kept
  });
});
