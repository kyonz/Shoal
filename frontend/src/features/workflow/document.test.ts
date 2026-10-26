import { describe, expect, it } from "vitest";
import { useBuilder } from "@/pages/workflows/builder/store";
import { alignKeys, parseWorkflowYaml, stripServerDefaults, toDocument, toYaml } from "./document";
import { expressionVars } from "./expressions";
import { toEdges, toNodes } from "./flow";
import { validateWorkflow } from "./validate";

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

const VALID = `apiVersion: shoal.dev/v1
kind: Workflow
metadata:
  name: demo
  description: A demo
  labels:
    team: core
spec:
  inputs:
    name:
      type: string
      required: true
    count: {type: integer, default: 2}
  outputs:
    out: "{{ steps.third.output.text }}"
  defaults: {agent: helper}
  steps:
    - id: greet
      type: set
      values: {text: "Hello {{ inputs.name }}"}
    - id: second
      type: agent
      prompt: hi
      outputs:
        verdict: {type: string}
      needs: [greet]
    - id: script
      type: script
      language: python
      code: print(1)
      inputs: {x: "{{ steps.greet.output.text }}"}
      env: {A: b}
    - id: third
      type: set
      if: "{{ steps.second.output.verdict == 'ok' }}"
      needs: [greet, second]
      retry: {max_attempts: 3}
      values: {text: three}
`;

describe("parseWorkflowYaml", () => {
  const error = (text: string) => {
    const r = parseWorkflowYaml(text);
    return r.ok ? null : r.error;
  };
  const withSpec = (extra: string, steps = "    - id: a\n      type: set\n      values: {x: 1}\n") =>
    `apiVersion: shoal.dev/v1\nkind: Workflow\nmetadata:\n  name: demo\nspec:\n${extra}  steps:\n${steps}`;

  it("accepts a well-formed workflow", () => {
    expect(error(VALID)).toBeNull();
  });

  it("reports syntax errors with a line", () => {
    const r = parseWorkflowYaml("spec:\n  steps: [oops\n");
    expect(r.ok).toBe(false);
    expect(!r.ok && r.line).toBeTruthy();
  });

  it.each([
    ["an input with no settings yet", withSpec("  inputs:\n    name:\n"), "Input 'name' needs its settings"],
    ["inputs written as text", withSpec("  inputs: foo\n"), "spec.inputs must be a mapping"],
    ["needs written as text", withSpec("", "    - id: a\n      type: set\n    - id: b\n      type: set\n      needs: a\n"), "step 'b' needs must be a list"],
    ["needs with a number", withSpec("", "    - id: a\n      type: set\n      needs: [1]\n"), "step 'a' needs[0] must be text"],
    ["two steps with one id", withSpec("", "    - id: a\n      type: set\n    - id: a\n      type: set\n"), "Two steps have the id 'a'"],
    ["labels written as text", "kind: Workflow\nmetadata:\n  labels: x\nspec:\n  steps: []\n", "metadata.labels must be a mapping"],
    ["a list as metadata", "kind: Workflow\nmetadata: [a]\nspec:\n  steps: []\n", "metadata must be a mapping"],
    ["values written as text", withSpec("", "    - id: a\n      type: set\n      values: hi\n"), "step 'a' values must be a mapping"],
    ["agent outputs with no settings", withSpec("", "    - id: a\n      type: agent\n      prompt: p\n      outputs:\n        x:\n"), "step 'a' outputs.x must be a mapping"],
    ["connections as a list", withSpec("  connections: [a]\n"), "spec.connections must be a mapping"],
    ["a self-referencing alias", withSpec("", "    - &s\n      id: a\n      type: set\n      values: {x: *s}\n"), "refers to itself"],
  ])("rejects %s", (_, text, message) => {
    expect(error(text)).toContain(message);
  });
});

/**
 * Every intermediate state of typing (each prefix of a document, each line
 * removed, each value blanked) must either be rejected with a message or be
 * safe for everything the builder renders from it.
 */
describe("half-typed workflow YAML", () => {
  const variants = (text: string): string[] => {
    const lines = text.split("\n");
    const out = new Set<string>();
    for (let i = 0; i <= text.length; i++) out.add(text.slice(0, i));
    lines.forEach((_, i) => out.add([...lines.slice(0, i), ...lines.slice(i + 1)].join("\n")));
    lines.forEach((line, i) => {
      const m = line.match(/^(\s*-?\s*[\w]+:)\s*\S.*$/);
      if (!m) return;
      for (const v of ["", " null", " []", " x", " 5", " {}"]) out.add([...lines.slice(0, i), m[1] + v, ...lines.slice(i + 1)].join("\n"));
    });
    return [...out];
  };

  it("never hands the builder a document it cannot render", () => {
    let accepted = 0;
    for (const text of variants(VALID)) {
      const r = parseWorkflowYaml(text);
      if (!r.ok) {
        expect(r.error).toBeTruthy();
        continue;
      }
      accepted++;
      const spec = r.doc.spec;
      expect(() => {
        useBuilder.getState().setSpec(spec, "yaml");
        useBuilder.getState().setMeta({ description: r.doc.metadata.description ?? "", labels: r.doc.metadata.labels ?? {} }, "yaml");
        const st = useBuilder.getState();
        const issues = validateWorkflow(st.spec, { agents: ["helper"], workflows: [] });
        toNodes(st.spec.steps, st.layout, () => ({ errors: issues.map((i) => i.message) }));
        toEdges(st.spec.steps);
        for (const s of st.spec.steps) expressionVars(st.spec, s.id);
        toYaml(toDocument(st.meta.name, st.meta.description, st.meta.labels, st.spec));
      }, text).not.toThrow();
    }
    expect(accepted).toBeGreaterThan(50);
  });
});
