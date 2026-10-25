import { describe, expect, it } from "vitest";
import { kindPath, kindPermissions, looksLikeCredential } from "./kinds";

describe("kindPath", () => {
  it("puts tenant-wide kinds in the admin console", () => {
    expect(kindPath("gateways", "team-a")).toBe("/admin/gateways");
    expect(kindPath("model-policies", "team-a", "coding")).toBe("/admin/model-policies/coding");
  });
  it("keeps namespaced kinds under the namespace", () => {
    expect(kindPath("agents", "team-a", "bugfixer")).toBe("/ns/team-a/agents/bugfixer");
  });
});

describe("kindPermissions", () => {
  it("uses the tenant manage permission for admin kinds", () => {
    expect(kindPermissions("gateways")).toEqual({ write: "gateways:manage", delete: "gateways:manage", tenant: true });
    expect(kindPermissions("agents").write).toBe("agents:write");
  });
});

describe("looksLikeCredential", () => {
  it("matches the names the engine rejects", () => {
    for (const n of ["authorization", "x-api-key", "X-Portkey-Api-Key", "api_key", "token", "x-auth-token", "cookie"]) expect(looksLikeCredential(n)).toBe(true);
    for (const n of ["x-team", "x-region", "keyboard", "monkey", "x-request-id"]) expect(looksLikeCredential(n)).toBe(false);
  });
});
