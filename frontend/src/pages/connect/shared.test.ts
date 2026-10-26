import { describe, expect, it } from "vitest";
import { fromYaml } from "./shared";

describe("fromYaml", () => {
  it("reads description and spec", () => {
    expect(fromYaml("metadata:\n  description: d\nspec:\n  event: push\n")).toEqual({ description: "d", spec: { event: "push" } });
  });

  it.each([
    ["", "Document is empty"],
    ["- a\n", "The document must be a mapping"],
    ["metadata: {}\n", "Missing `spec`"],
    ["spec: [a]\n", "spec must be a mapping"],
    ["spec: text\n", "spec must be a mapping"],
    ["metadata: [x]\nspec: {}\n", "metadata must be a mapping"],
    ["metadata:\n  description: [x]\nspec: {}\n", "metadata.description must be text"],
  ])("rejects %j", (text, message) => {
    expect(() => fromYaml(text)).toThrow(message);
  });
});
