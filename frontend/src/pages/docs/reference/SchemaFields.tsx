/**
 * Field tables for JSON Schemas (pydantic output and OpenAPI components),
 * with referenced object types expandable inline.
 */
import { ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { JsonSchema } from "@/api/hooks";
import { cn } from "@/lib/cn";

export type RefLookup = (ref: string) => JsonSchema | undefined;

/** Look up `#/$defs/X`, `#/definitions/X` or `#/components/schemas/X`. */
export function makeLookup(...roots: (JsonSchema | undefined)[]): RefLookup {
  return (ref) => {
    const name = ref.split("/").pop()!;
    for (const r of roots) {
      if (!r) continue;
      const found =
        r.$defs?.[name] ??
        (r as { definitions?: Record<string, JsonSchema> }).definitions?.[name] ??
        (r as { components?: { schemas?: Record<string, JsonSchema> } }).components?.schemas?.[name];
      if (found) return found;
    }
    return undefined;
  };
}

export function refName(ref: string) {
  return ref.split("/").pop() ?? ref;
}

/** Follow $ref / single allOf; returns the schema and the def name it came from. */
export function deref(s: JsonSchema | undefined, lookup: RefLookup): { schema: JsonSchema; name?: string } {
  let schema = s ?? {};
  let name: string | undefined;
  for (let i = 0; i < 8; i++) {
    if (schema.$ref) {
      name = refName(schema.$ref);
      const { $ref, ...rest } = schema;
      schema = { ...(lookup($ref) ?? {}), ...rest };
    } else if (schema.allOf?.length === 1) {
      const { allOf, ...rest } = schema;
      schema = { ...allOf[0], ...rest };
    } else break;
  }
  return { schema, name };
}

function nonNull(variants: JsonSchema[]) {
  return variants.filter((v) => v.type !== "null");
}

export function isNullable(s: JsonSchema): boolean {
  const v = s.anyOf ?? s.oneOf;
  return !!v && v.some((x) => x.type === "null");
}

export function typeLabel(s: JsonSchema | undefined, lookup: RefLookup, depth = 0): string {
  if (!s || depth > 5) return "any";
  if (s.$ref) return refName(s.$ref);
  if (s.const !== undefined) return JSON.stringify(s.const);
  if (s.enum) return s.enum.map((e) => JSON.stringify(e)).join(" | ");
  const variants = s.anyOf ?? s.oneOf;
  if (variants) {
    const nn = nonNull(variants);
    return nn.map((v) => typeLabel(v, lookup, depth + 1)).join(" | ") || "null";
  }
  if (s.allOf?.length === 1) return typeLabel(s.allOf[0], lookup, depth + 1);
  const type = Array.isArray(s.type) ? s.type.filter((t) => t !== "null").join(" | ") : s.type;
  if (type === "array") return `${typeLabel(s.items, lookup, depth + 1)}[]`;
  if (type === "object" || (!type && s.additionalProperties)) {
    if (s.properties) return "object";
    if (typeof s.additionalProperties === "object") return `map<string, ${typeLabel(s.additionalProperties, lookup, depth + 1)}>`;
    return "object";
  }
  if (s.format === "duration" || /duration/i.test(String(s.title ?? ""))) return "duration";
  return (type as string) ?? "any";
}

/** Object defs a field points at (directly, in arrays, maps or unions). */
function nestedDefs(s: JsonSchema | undefined, lookup: RefLookup, depth = 0): { name: string; schema: JsonSchema }[] {
  if (!s || depth > 4) return [];
  if (s.$ref) {
    const { schema, name } = deref(s, lookup);
    return schema.properties && name ? [{ name, schema }] : [];
  }
  const out: { name: string; schema: JsonSchema }[] = [];
  for (const v of s.anyOf ?? s.oneOf ?? s.allOf ?? []) out.push(...nestedDefs(v, lookup, depth + 1));
  if (s.items) out.push(...nestedDefs(s.items, lookup, depth + 1));
  if (typeof s.additionalProperties === "object") out.push(...nestedDefs(s.additionalProperties, lookup, depth + 1));
  return out;
}

function defaultLabel(v: unknown): string | null {
  if (v === undefined || v === null) return null;
  if (typeof v === "object" && v !== null && Object.keys(v).length === 0) return Array.isArray(v) ? "[]" : "{}";
  return JSON.stringify(v);
}

export function FieldsTable({ schema, lookup, exclude = [], seen = [] }: {
  schema: JsonSchema;
  lookup: RefLookup;
  exclude?: string[];
  seen?: string[];
}) {
  const { schema: s } = deref(schema, lookup);
  const props = Object.entries(s.properties ?? {}).filter(([k]) => !exclude.includes(k));
  const required = new Set(s.required ?? []);
  if (!props.length) return <p className="text-[13px] text-faint">No fields.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="bg-subtle text-left text-[11px] font-semibold tracking-wide text-faint uppercase">
            <th className="w-[30%] px-3 py-2">Field</th>
            <th className="w-[25%] px-3 py-2">Type</th>
            <th className="px-3 py-2">Description</th>
          </tr>
        </thead>
        <tbody>
          {props.map(([name, field]) => (
            <FieldRow key={name} name={name} field={field} required={required.has(name)} lookup={lookup} seen={seen} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FieldRow({ name, field, required, lookup, seen }: {
  name: string;
  field: JsonSchema;
  required: boolean;
  lookup: RefLookup;
  seen: string[];
}) {
  const [open, setOpen] = useState(false);
  const { schema: resolved } = deref(field, lookup);
  const nested = nestedDefs(field, lookup).filter((d) => !seen.includes(d.name));
  const def = defaultLabel(field.default ?? resolved.default);
  const description = field.description ?? resolved.description;
  return (
    <>
      <tr className="border-t border-border align-top">
        <td className="px-3 py-2">
          <div className="flex items-center gap-1.5">
            {nested.length > 0 ? (
              <button onClick={() => setOpen((o) => !o)} className="-ml-1 rounded p-0.5 text-faint hover:text-fg" aria-label={open ? "Collapse" : "Expand"}>
                <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
              </button>
            ) : null}
            <code className="font-mono text-[12.5px] font-medium text-fg">{name}</code>
          </div>
          {required && <span className="mt-1 inline-block rounded bg-bad-soft px-1 text-[10px] font-semibold tracking-wide text-bad uppercase">required</span>}
        </td>
        <td className="px-3 py-2">
          <code className="font-mono text-[12px] break-words text-info">{typeLabel(field, lookup)}</code>
          {isNullable(field) && <span className="ml-1 text-[11px] text-faint">optional</span>}
        </td>
        <td className="px-3 py-2 text-muted">
          {description && <span className="text-fg/90">{description}</span>}
          {def && (
            <div className={cn("text-xs", description && "mt-1")}>
              Default: <code className="font-mono text-[11.5px]">{def}</code>
            </div>
          )}
        </td>
      </tr>
      {open &&
        nested.map((d) => (
          <tr key={d.name} className="border-t border-border bg-subtle/40">
            <td colSpan={3} className="px-3 py-3 pl-7">
              <div className="mb-2 text-xs font-semibold text-muted">
                <code className="font-mono">{d.name}</code>
                {d.schema.description && <span className="ml-2 font-normal text-faint">{d.schema.description}</span>}
              </div>
              <FieldsTable schema={d.schema} lookup={lookup} seen={[...seen, d.name]} />
            </td>
          </tr>
        ))}
    </>
  );
}

export function SectionAnchor({ id, children, sub, label }: { id: string; children: ReactNode; sub?: ReactNode; label?: string }) {
  return (
    <h2 id={id} data-toc-label={label} className="mt-10 mb-3 scroll-mt-6 border-t border-border pt-5 text-[1.25rem] font-semibold tracking-tight">
      <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {children}
        {sub && <span className="text-[12px] font-normal tracking-normal text-faint">{sub}</span>}
      </span>
    </h2>
  );
}
