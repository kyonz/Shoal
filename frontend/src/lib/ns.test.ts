import { describe, expect, it } from "vitest";
import { nsPath } from "./ns";

describe("nsPath", () => {
  it("builds namespaced paths", () => {
    expect(nsPath("acme", "workflows")).toBe("/ns/acme/workflows");
    expect(nsPath("acme", "connections", "gh main")).toBe("/ns/acme/connections/gh%20main");
    expect(nsPath("shared", "workflows")).toBe("/ns/shared/workflows");
  });

  it("sends the organisation's connections and secrets to the admin console", () => {
    expect(nsPath("shared", "connections")).toBe("/admin/connections");
    expect(nsPath("shared", "connections", "github-acme")).toBe("/admin/connections/github-acme");
    expect(nsPath("shared", "secrets")).toBe("/admin/secrets");
  });
});
