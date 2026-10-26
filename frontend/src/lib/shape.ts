/**
 * Shape checks for documents typed or pasted into a YAML/JSON editor. A live
 * editor must never hand the rest of the page a value of the wrong shape (a
 * string where a list is expected, `null` where a mapping is): forms and
 * canvases render straight from it. Each check throws a `ShapeError` naming
 * the offending path, which the editor shows inline instead of applying.
 */

export class ShapeError extends Error {}

export type Path = string;

export const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

const describe = (v: unknown) => (v === null ? "nothing" : Array.isArray(v) ? "a list" : typeof v === "object" ? "a mapping" : `${typeof v} ${JSON.stringify(v)}`);

function fail(path: Path, want: string, got: unknown): never {
  throw new ShapeError(`${path} must be ${want} (got ${describe(got)})`);
}

/** A mapping (optional: undefined passes). */
export function record(v: unknown, path: Path, optional = false): Record<string, unknown> | undefined {
  if (v === undefined && optional) return undefined;
  if (!isRecord(v)) fail(path, "a mapping", v);
  return v;
}

/** A list (optional: undefined passes). */
export function list(v: unknown, path: Path, optional = false): unknown[] | undefined {
  if (v === undefined && optional) return undefined;
  if (!Array.isArray(v)) fail(path, "a list", v);
  return v;
}

/** A string (optional: undefined or null pass). */
export function string(v: unknown, path: Path, optional = false): string | undefined {
  if (v == null && optional) return undefined;
  if (typeof v !== "string") fail(path, "text", v);
  return v;
}

export function boolean(v: unknown, path: Path, optional = false): boolean | undefined {
  if (v == null && optional) return undefined;
  if (typeof v !== "boolean") fail(path, "true or false", v);
  return v;
}

/** A string, or a number YAML read from an unquoted value such as `30` (optional: undefined or null pass). */
export function scalar(v: unknown, path: Path, optional = false): string | number | undefined {
  if (v == null && optional) return undefined;
  if (typeof v !== "string" && typeof v !== "number") fail(path, "text", v);
  return v;
}

/** A list of strings. */
export function stringList(v: unknown, path: Path, optional = false): string[] | undefined {
  const l = list(v, path, optional);
  l?.forEach((x, i) => string(x, `${path}[${i}]`));
  return l as string[] | undefined;
}

/** A mapping of string (or scalar) values, such as labels. */
export function stringMap(v: unknown, path: Path, optional = false): Record<string, string> | undefined {
  const r = record(v, path, optional);
  if (r) for (const [k, x] of Object.entries(r)) scalar(x, `${path}.${k}`);
  return r as Record<string, string> | undefined;
}

/** A mapping whose every value is a mapping, such as typed inputs. */
export function recordMap(v: unknown, path: Path, optional = false): Record<string, Record<string, unknown>> | undefined {
  const r = record(v, path, optional);
  if (r) for (const [k, x] of Object.entries(r)) record(x, `${path}.${k}`);
  return r as Record<string, Record<string, unknown>> | undefined;
}

/** The message of a thrown value, for an inline error. */
export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
