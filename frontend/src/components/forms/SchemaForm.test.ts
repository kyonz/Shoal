import { describe, expect, it } from "vitest";
import type { JsonSchema } from "@/api/hooks";
import { checkAgainstSchema } from "./SchemaForm";

const schema: JsonSchema = {
  type: "object",
  properties: {
    model: { type: "string" },
    temperature: { anyOf: [{ type: "number" }, { type: "null" }] },
    tools: { type: "array", items: { type: "string" } },
    files: { type: "object", additionalProperties: { type: "string" } },
    cases: { type: "array", items: { $ref: "#/$defs/Case" } },
    enabled: { type: "boolean" },
  },
  $defs: { Case: { type: "object", properties: { name: { type: "string" }, inputs: { type: "object" } } } },
};

const check = (spec: unknown) => () => checkAgainstSchema(spec, schema, "spec");

describe("checkAgainstSchema", () => {
  it("accepts a matching document, missing fields and expressions", () => {
    expect(check({ model: "m", temperature: null, tools: ["a"], files: { "a.md": "x" }, cases: [{ name: "c", inputs: {} }], enabled: "{{ inputs.on }}" })).not.toThrow();
    expect(check({})).not.toThrow();
  });

  it.each([
    [{ tools: "a" }, "spec.tools must be a list"],
    [{ tools: [{}] }, "spec.tools[0] must be text"],
    [{ files: ["a"] }, "spec.files must be a mapping"],
    [{ files: { "a.md": {} } }, "spec.files.a.md must be text"],
    [{ cases: [null] }, "spec.cases[0] is empty"],
    [{ cases: [{ inputs: "x" }] }, "spec.cases[0].inputs must be a mapping"],
    [{ temperature: "hot" }, "spec.temperature must be a number"],
    [{ enabled: "yes" }, "spec.enabled must be true or false"],
    [[1], "spec must be a mapping"],
  ])("rejects %j", (spec, message) => {
    expect(check(spec)).toThrow(message);
  });
});
