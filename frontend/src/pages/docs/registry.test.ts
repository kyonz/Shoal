import { describe, expect, it } from "vitest";
import { REPO_URL, resolveDocHref } from "./registry";

describe("resolveDocHref", () => {
  it("keeps links between docs pages in the app", () => {
    expect(resolveDocHref("skills.md", "cli")).toBe("/docs/skills");
    expect(resolveDocHref("integrations.md#remote-protocol", "cli")).toBe("/docs/integrations#remote-protocol");
    expect(resolveDocHref("../architecture.md", "guides/workflow-builder")).toBe("/docs/architecture");
  });

  it("sends links to other repository files to the repository", () => {
    expect(resolveDocHref("../deploy/README.md", "cli")).toBe(`${REPO_URL}/deploy/README.md`);
    expect(resolveDocHref("../examples/remote-integration-node/server.mjs", "integrations")).toBe(
      `${REPO_URL}/examples/remote-integration-node/server.mjs`,
    );
    expect(resolveDocHref("assets/brand/", "brand")).toBe(`${REPO_URL}/docs/assets/brand`);
  });

  it("leaves external, absolute and anchor links alone", () => {
    expect(resolveDocHref("https://example.com/x", "cli")).toBe("https://example.com/x");
    expect(resolveDocHref("/docs/getting-started", "cli")).toBe("/docs/getting-started");
    expect(resolveDocHref("#connecting", "cli")).toBe("#connecting");
  });
});
