import { describe, expect, it } from "vitest";
import { parseBlock } from "./sse";

describe("parseBlock", () => {
  it("parses id, event and multi-line data", () => {
    expect(parseBlock("id: 7\nevent: step.started\ndata: {\"a\":\ndata: 1}")).toEqual({ id: "7", event: "step.started", data: '{"a":\n1}' });
  });
  it("ignores comments and pings", () => {
    expect(parseBlock(": ping")).toBeNull();
  });
});
