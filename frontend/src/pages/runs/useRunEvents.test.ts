import { describe, expect, it, vi } from "vitest";

const calls: string[] = [];
vi.mock("@/api/client", () => ({
  rawJson: async (path: string) => {
    calls.push(path);
    const after = Number(new URL(path, "http://x").searchParams.get("after"));
    const total = 1203;
    const ids = Array.from({ length: Math.max(0, Math.min(500, total - after)) }, (_, i) => after + i + 1);
    return ids.map((id) => ({ id, type: "step.started" }));
  },
}));
vi.mock("@/api/sse", () => ({ subscribe: () => () => undefined }));
vi.mock("@/api/hooks", () => ({ keys: { run: (id: string) => ["run", id] } }));

describe("fetchEvents", () => {
  it("follows pages until a short one, so long runs show every event", async () => {
    const { fetchEvents } = await import("./useRunEvents");
    const events = await fetchEvents("run_1");
    expect(events).toHaveLength(1203);
    expect(events[1202].id).toBe(1203);
    expect(calls).toEqual(["/v1/runs/run_1/events?after=0", "/v1/runs/run_1/events?after=500", "/v1/runs/run_1/events?after=1000"]);
  });
});
