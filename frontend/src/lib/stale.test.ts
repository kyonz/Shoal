import { describe, expect, it } from "vitest";
import { isChunkLoadError } from "./stale";

describe("isChunkLoadError", () => {
  it("recognises each browser's failed chunk import", () => {
    expect(isChunkLoadError(new TypeError("Failed to fetch dynamically imported module: http://x/assets/Overview-ClVDkMyT.js"))).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module: http://x/assets/a.js"))).toBe(true);
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true);
  });

  it("ignores other errors", () => {
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false);
    expect(isChunkLoadError(undefined)).toBe(false);
  });
});
