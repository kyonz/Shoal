import { describe, expect, it } from "vitest";
import type { Resource } from "./client";
import { visibleResources } from "./hooks";

const res = (name: string, namespace: string) =>
  ({ apiVersion: "shoal.dev/v1", kind: "Connection", metadata: { name, namespace }, spec: {} }) as unknown as Resource;

describe("visibleResources", () => {
  it("lists the namespace's own resources, then shared ones it does not shadow", () => {
    const out = visibleResources([res("tickets", "default")], [res("tickets", "shared"), res("linear", "shared")]);
    expect(out.map((r) => [r.metadata.name, r.metadata.namespace, r.shared])).toEqual([
      ["tickets", "default", false],
      ["linear", "shared", true],
    ]);
  });
});
