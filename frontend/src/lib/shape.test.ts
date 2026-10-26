import { describe, expect, it } from "vitest";
import { ShapeError, list, record, recordMap, scalar, string, stringList, stringMap } from "./shape";

describe("shape checks", () => {
  it("pass values of the right shape through", () => {
    expect(record({ a: 1 }, "x")).toEqual({ a: 1 });
    expect(list([1], "x")).toEqual([1]);
    expect(stringList(["a"], "x")).toEqual(["a"]);
    expect(stringMap({ a: "b", n: 1 }, "x")).toEqual({ a: "b", n: 1 });
    expect(scalar(30, "x")).toBe(30);
  });

  it("let optional values be missing", () => {
    expect(record(undefined, "x", true)).toBeUndefined();
    expect(string(null, "x", true)).toBeUndefined();
    expect(stringList(undefined, "x", true)).toBeUndefined();
  });

  it.each([
    [() => record(null, "spec.inputs"), "spec.inputs must be a mapping (got nothing)"],
    [() => record([1], "spec"), "spec must be a mapping (got a list)"],
    [() => list("a", "needs"), 'needs must be a list (got string "a")'],
    [() => string({}, "name"), "name must be text (got a mapping)"],
    [() => stringList(["a", 2], "needs"), "needs[1] must be text (got number 2)"],
    [() => stringMap({ a: [] }, "labels"), "labels.a must be text (got a list)"],
    [() => recordMap({ name: null }, "inputs"), "inputs.name must be a mapping (got nothing)"],
  ])("name the offending path", (check, message) => {
    expect(check).toThrow(ShapeError);
    expect(check).toThrow(message);
  });
});
