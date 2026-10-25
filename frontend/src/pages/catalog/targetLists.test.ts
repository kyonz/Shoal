import { describe, expect, it } from "vitest";
import { keyField } from "./targetLists";

describe("keyField", () => {
  it("reads the field a simple key expression uses", () => {
    expect(keyField("{{ target.full_name }}")).toBe("full_name");
    expect(keyField(undefined)).toBe("id");
  });
  it("gives nothing for computed keys", () => {
    expect(keyField("{{ target.owner.login }}/{{ target.name }}")).toBeUndefined();
  });
});
