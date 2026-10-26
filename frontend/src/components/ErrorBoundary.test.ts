import { describe, expect, it } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

describe("ErrorBoundary", () => {
  const derive = (resetKeys: unknown[] | undefined, state: { error: Error | null; keys: unknown[] | undefined }) =>
    ErrorBoundary.getDerivedStateFromProps({ children: null, resetKeys }, state);

  it("keeps showing the error while its inputs are unchanged", () => {
    const doc = { steps: [] };
    expect(derive([doc], { error: new Error("x"), keys: [doc] })).toBeNull();
  });

  it("tries again once its inputs change", () => {
    expect(derive([{ steps: [] }], { error: new Error("x"), keys: [{ steps: [] }] })).toMatchObject({ error: null });
    expect(derive(["/b"], { error: new Error("x"), keys: ["/a"] })).toMatchObject({ error: null, keys: ["/b"] });
  });

  it("turns anything thrown into an Error", () => {
    expect(ErrorBoundary.getDerivedStateFromError("boom")).toMatchObject({ error: new Error("boom") });
  });
});
