import { describe, expect, it } from "vitest";
import { actorName, parseActor } from "./actor";

describe("parseActor", () => {
  it("reads every actor prefix", () => {
    expect(parseActor("user:dana@acme.com")).toMatchObject({ kind: "user", name: "dana@acme.com" });
    expect(parseActor("user:dana@acme.com")?.tag).toBeUndefined();
    expect(parseActor("sa:deploy-bot")).toMatchObject({ kind: "service", name: "deploy-bot", tag: "Service" });
    expect(parseActor("account:trevor@acme.dev")).toMatchObject({ kind: "account", name: "trevor@acme.dev", tag: "Instance" });
    expect(parseActor("hub_user:ana")).toMatchObject({ kind: "hub", name: "ana" });
    expect(parseActor("system:admin")).toMatchObject({ kind: "system", name: "Admin token" });
  });

  it("keeps unknown actors as they are", () => {
    expect(parseActor("scheduler")).toMatchObject({ kind: "other", name: "scheduler" });
    expect(parseActor(null)).toBeNull();
  });
});

describe("actorName", () => {
  it("says what kind of actor it is in plain text", () => {
    expect(actorName("user:dana@acme.com")).toBe("dana@acme.com");
    expect(actorName("account:trevor@acme.dev")).toBe("trevor@acme.dev (instance)");
    expect(actorName("sa:deploy-bot")).toBe("deploy-bot (service account)");
    expect(actorName(undefined, "—")).toBe("—");
  });
});
