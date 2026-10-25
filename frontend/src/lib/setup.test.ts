import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import { askAdmin, problemFrom, problemTitle, setupProblem, whoFixes } from "./setup";

const gatewayProblem = {
  type: "https://shoal.dev/problems/missing_configuration",
  status: 409,
  code: "missing_configuration",
  detail: "no gateway named 'anthropic' is set up",
  details: {
    code: "missing_configuration",
    missing: { kind: "Gateway", name: "anthropic" },
    fix: { href: "/admin/gateways/new?name=anthropic", label: "Add the anthropic gateway", permission: "gateways:manage", namespace: null },
  },
};

describe("setup problems", () => {
  it("reads a missing_configuration problem document", () => {
    const p = setupProblem(new ApiError(409, gatewayProblem))!;
    expect(p.code).toBe("missing_configuration");
    expect(p.missing).toEqual({ kind: "Gateway", name: "anthropic" });
    expect(p.fix?.href).toBe("/admin/gateways/new?name=anthropic");
    expect(problemTitle(p)).toBe("LLM gateway needed");
  });

  it("reads a failed step's error, which carries the same fields", () => {
    const p = problemFrom({ message: "secret 'k' is not set", retryable: false, code: "missing_configuration",
      missing: { kind: "Secret", name: "k", namespace: "shared" },
      fix: { href: "/ns/shared/secrets", label: "Set the secret", permission: "secrets:write", namespace: "shared" } })!;
    expect(p.detail).toBe("secret 'k' is not set");
    expect(problemTitle(p)).toBe("A secret is not set");
  });

  it("ignores other errors", () => {
    expect(setupProblem(new ApiError(404, { code: "not_found", detail: "nope" }))).toBeNull();
    expect(problemFrom({ message: "boom" })).toBeNull();
    expect(setupProblem(new Error("x"))).toBeNull();
  });

  it("offers the fix to people who can make it and tells everyone else who to ask", () => {
    const p = setupProblem(new ApiError(409, gatewayProblem))!;
    expect(whoFixes(p, (perm) => perm === "gateways:manage")).toBe("you");
    expect(whoFixes(p, () => false)).toBe("admin");
    expect(askAdmin(p)).toBe("Ask a tenant admin to add the anthropic gateway.");
    expect(askAdmin({ ...p, fix: { ...p.fix!, label: "Add a gateway" } })).toBe("Ask a tenant admin to add a gateway (anthropic).");
    const policy = { ...p, fix: { href: "/admin/model-policies/default", label: "Edit the model policy", permission: "model-policies:manage" } };
    expect(problemTitle({ ...policy, missing: { kind: "ModelPolicy", name: "default" } })).toBe("Check the model policy");
  });

  it("treats a person's own account as theirs to connect", () => {
    const p = setupProblem(new ApiError(428, { code: "credential_required", detail: "connect it", details: {
      connection: "jira", namespace: "default", fix: { href: "/settings/credentials?connection=default/jira", label: "Connect your account", permission: null } } }))!;
    expect(whoFixes(p, () => false)).toBe("self");
    expect(problemTitle(p)).toBe("Connect your account");
  });
});
