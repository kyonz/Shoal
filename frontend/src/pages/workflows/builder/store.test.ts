import { describe, expect, it } from "vitest";
import { renamedSteps, useBuilder } from "./store";

const step = (id: string) => ({ id, type: "set", values: { x: "1" } });

describe("renamedSteps", () => {
  it("pairs a changed id with the step it replaced", () => {
    expect([...renamedSteps([step("a"), step("b")], [step("a"), step("bc")])]).toEqual([["b", "bc"]]);
    expect(renamedSteps([step("a")], [step("a"), step("b")]).size).toBe(0); // an added step is not a rename
  });

  it("keeps a step's position and selection while its id is typed in YAML", () => {
    const st = useBuilder.getState();
    st.load({ spec: { steps: [step("a"), step("b")] }, layout: { a: { x: 0, y: 0 }, b: { x: 500, y: 300 } }, meta: { name: "w", description: "", labels: {} } });
    st.select("b");
    useBuilder.getState().setSpec({ steps: [step("a"), step("bc")] }, "yaml");
    expect(useBuilder.getState().layout.bc).toEqual({ x: 500, y: 300 });
    expect(useBuilder.getState().selected).toBe("bc");
  });
});
