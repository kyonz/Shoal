import { describe, expect, it } from "vitest";
import { describeCall, resultStatus, resultsById, type ChatMessage } from "./api";

const history: ChatMessage[] = [
  { role: "user", content: "make a workflow" },
  {
    role: "assistant",
    content: [
      { type: "thinking", text: "" },
      { type: "tool_use", id: "t1", name: "api_get", input: { path: "/v1/namespaces/default/workflows" } },
      { type: "tool_use", id: "t2", name: "apply_resources", input: { yaml: "kind: Workflow", summary: "Create workflow word-count" } },
    ],
  },
  { role: "user", content: [{ type: "tool_result", tool_use_id: "t1", content: '{"status": 200, "body": []}' }] },
];

describe("assistant history helpers", () => {
  it("pairs tool calls with their results", () => {
    const results = resultsById(history);
    expect([...results.keys()]).toEqual(["t1"]);
    expect(resultStatus(results.get("t1"))).toBe(200);
    expect(resultStatus(results.get("t2"))).toBeNull();
  });

  it("describes calls in plain words", () => {
    const calls = (history[1].content as { type: string }[]).filter((b) => b.type === "tool_use");
    expect(calls.map((c) => describeCall(c))).toEqual(["Read /v1/namespaces/default/workflows", "Create workflow word-count"]);
  });
});
