/**
 * Renders a form from a JSON Schema (pydantic v2 output), so plugin step
 * types, integration connections and action inputs need no UI changes.
 *
 * Supported: objects, strings (enum, multi-line), numbers, booleans, string
 * arrays (tags), object arrays, string maps, optional (`anyOf [.., null]`),
 * `$ref`/`$defs`. Anything else falls back to a JSON editor. Callers can swap
 * any field for a custom widget by key.
 */
import { ChevronRight, Plus, Trash2, X } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import type { JsonSchema } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { Button, CodeEditor, Field, Input, NativeSelect, Switch, Textarea } from "@/components/ui";

export type Widget = (props: {
  value: unknown;
  onChange: (v: unknown) => void;
  schema: JsonSchema;
  name: string;
  required: boolean;
}) => ReactNode;

export interface SchemaFormProps {
  schema: JsonSchema;
  value: Record<string, unknown> | undefined;
  onChange: (v: Record<string, unknown>) => void;
  /** Root schema used to resolve `$ref` (defaults to `schema`). */
  root?: JsonSchema;
  /** Custom widgets by property name. */
  widgets?: Record<string, Widget>;
  /** Property names not to render (handled elsewhere). */
  hidden?: string[];
  /** Property names rendered first, in this order. */
  order?: string[];
  /** Field-level errors by property name. */
  errors?: Record<string, string>;
  /** Collapse optional fields under "More options". */
  collapseOptional?: boolean;
  className?: string;
}

// --------------------------------------------------------------------------- schema helpers

export function resolve(schema: JsonSchema | undefined, root: JsonSchema): JsonSchema {
  let s = schema ?? {};
  for (let i = 0; i < 10 && s.$ref; i++) {
    const name = s.$ref.split("/").pop()!;
    const target = root.$defs?.[name] ?? (root as { definitions?: Record<string, JsonSchema> }).definitions?.[name];
    const { $ref: _ignored, ...rest } = s;
    s = { ...(target ?? {}), ...rest };
  }
  if (s.allOf?.length === 1) {
    const { allOf, ...rest } = s;
    s = { ...resolve(allOf[0], root), ...rest };
  }
  return s;
}

/** Strip `anyOf: [X, {type: null}]` to X and report optionality. */
export function unwrapNullable(schema: JsonSchema, root: JsonSchema): { schema: JsonSchema; nullable: boolean } {
  const s = resolve(schema, root);
  const variants = s.anyOf ?? s.oneOf;
  if (variants && !s.discriminator) {
    const nonNull = variants.filter((v) => v.type !== "null");
    if (nonNull.length === 1 && nonNull.length < variants.length) {
      const { anyOf: _a, oneOf: _o, ...rest } = s;
      return { schema: { ...resolve(nonNull[0], root), ...rest }, nullable: true };
    }
  }
  return { schema: s, nullable: false };
}

function typeOf(s: JsonSchema): string | undefined {
  if (Array.isArray(s.type)) return s.type.find((t) => t !== "null");
  if (s.type) return s.type;
  if (s.enum) return "string";
  if (s.properties) return "object";
  return undefined;
}

export function humanize(key: string): string {
  const s = key.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const MULTILINE = /(prompt|instructions|body|description|message|run|script|readme|check|state|subject|text|query)$/i;

function isEmpty(v: unknown): boolean {
  if (v == null || v === "") return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

/** Drop undefined/empty values so YAML stays minimal. */
export function compactObject(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && !(typeof v === "string" && v === "")) out[k] = v;
  return out;
}

// --------------------------------------------------------------------------- the form

export function SchemaForm({ schema, value, onChange, root, widgets, hidden = [], order = [], errors, collapseOptional, className }: SchemaFormProps) {
  const r = root ?? schema;
  const s = resolve(schema, r);
  const props = s.properties ?? {};
  const required = new Set(s.required ?? []);
  const v = value ?? {};
  const [showMore, setShowMore] = useState(false);

  const keys = useMemo(() => {
    const all = Object.keys(props).filter((k) => !hidden.includes(k));
    const first = order.filter((k) => all.includes(k));
    return [...first, ...all.filter((k) => !first.includes(k))];
  }, [props, hidden, order]);

  const set = (k: string, val: unknown) => {
    const next = { ...v };
    if (val === undefined) delete next[k];
    else next[k] = val;
    onChange(next);
  };

  const primary = collapseOptional ? keys.filter((k) => required.has(k) || !isEmpty(v[k]) || widgets?.[k]) : keys;
  const secondary = collapseOptional ? keys.filter((k) => !primary.includes(k)) : [];

  const render = (k: string) => (
    <SchemaField
      key={k}
      name={k}
      schema={props[k]}
      root={r}
      value={v[k]}
      required={required.has(k)}
      onChange={(val) => set(k, val)}
      widget={widgets?.[k]}
      error={errors?.[k]}
    />
  );

  return (
    <div className={cn("space-y-3.5", className)}>
      {primary.map(render)}
      {secondary.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowMore((x) => !x)}
            className="flex items-center gap-1 text-xs font-medium text-muted hover:text-fg"
          >
            <ChevronRight className={cn("size-3.5 transition-transform", showMore && "rotate-90")} />
            {showMore ? "Fewer options" : `More options (${secondary.length})`}
          </button>
          {showMore && <div className="mt-3 space-y-3.5">{secondary.map(render)}</div>}
        </div>
      )}
    </div>
  );
}

export function SchemaField({ name, schema, root, value, onChange, required, widget, error, label }: {
  name: string;
  schema: JsonSchema;
  root: JsonSchema;
  value: unknown;
  onChange: (v: unknown) => void;
  required: boolean;
  widget?: Widget;
  error?: string;
  label?: ReactNode;
}) {
  const { schema: s } = unwrapNullable(schema, root);
  const title = label ?? s.title ?? humanize(name);
  const description = s.description;
  const type = typeOf(s);
  const placeholder = s.default != null && typeof s.default !== "object" ? String(s.default) : undefined;

  const wrap = (control: ReactNode) => (
    <Field label={title} required={required} description={description} error={error}>
      {control}
    </Field>
  );

  if (widget) return wrap(widget({ value, onChange, schema: s, name, required }));

  if (s.const !== undefined) return null;

  if (s.enum) {
    return wrap(
      <NativeSelect value={value == null ? "" : String(value)} onChange={(e) => onChange(e.target.value === "" ? undefined : coerceEnum(e.target.value, s.enum!))}>
        <option value="">{placeholder ? `Default (${placeholder})` : "—"}</option>
        {s.enum.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </NativeSelect>,
    );
  }

  switch (type) {
    case "boolean":
      return (
        <div className="flex items-start justify-between gap-4 rounded-lg border border-border bg-subtle/40 px-3 py-2">
          <div className="min-w-0">
            <div className="text-xs font-medium">{title}</div>
            {description && <div className="mt-0.5 text-xs text-faint">{description}</div>}
          </div>
          <Switch checked={value === undefined ? s.default === true : !!value} onCheckedChange={(c) => onChange(c)} />
        </div>
      );
    case "integer":
    case "number":
      return wrap(
        <Input
          type="number"
          value={value == null ? "" : String(value)}
          placeholder={placeholder}
          min={s.minimum}
          max={s.maximum}
          step={type === "integer" ? 1 : "any"}
          onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
        />,
      );
    case "string": {
      if (MULTILINE.test(name) || s.format === "textarea") {
        return wrap(
          <Textarea
            value={(value as string) ?? ""}
            placeholder={placeholder}
            mono={/(run|script|check)$/.test(name)}
            onChange={(e) => onChange(e.target.value || undefined)}
          />,
        );
      }
      return wrap(
        <Input
          value={value == null ? "" : String(value)}
          placeholder={placeholder}
          type={/secret|password|token/i.test(name) && s.format === "password" ? "password" : "text"}
          onChange={(e) => onChange(e.target.value || undefined)}
        />,
      );
    }
    case "array": {
      const items = unwrapNullable(s.items ?? {}, root).schema;
      const itemType = typeOf(items);
      if (itemType === "string" || itemType === "number" || itemType === "integer" || items.enum) {
        return wrap(<TagInput value={(value as string[]) ?? []} onChange={(l) => onChange(l.length ? l : undefined)} options={items.enum as string[] | undefined} />);
      }
      if (itemType === "object" && items.properties) {
        return wrap(<ObjectList value={(value as Record<string, unknown>[]) ?? []} schema={items} root={root} onChange={(l) => onChange(l.length ? l : undefined)} />);
      }
      return wrap(<JsonField value={value} onChange={onChange} />);
    }
    case "object": {
      if (s.properties && Object.keys(s.properties).length) {
        return (
          <fieldset className="rounded-lg border border-border p-3">
            <legend className="px-1 text-xs font-medium">{title}</legend>
            {description && <p className="-mt-1 mb-2 text-xs text-faint">{description}</p>}
            <SchemaForm schema={s} root={root} value={value as Record<string, unknown>} onChange={(o) => onChange(isEmpty(o) ? undefined : o)} />
          </fieldset>
        );
      }
      const ap = s.additionalProperties;
      const valueSchema = typeof ap === "object" ? unwrapNullable(ap, root).schema : undefined;
      if (valueSchema && typeOf(valueSchema) === "string") {
        return wrap(<KeyValueInput value={(value as Record<string, string>) ?? {}} onChange={(o) => onChange(isEmpty(o) ? undefined : o)} />);
      }
      return wrap(<JsonField value={value} onChange={onChange} />);
    }
    default:
      return wrap(<JsonField value={value} onChange={onChange} />);
  }
}

function coerceEnum(raw: string, options: unknown[]): unknown {
  return options.find((o) => String(o) === raw) ?? raw;
}

// --------------------------------------------------------------------------- composite inputs

export function TagInput({ value, onChange, placeholder = "Type and press Enter", options }: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  options?: string[];
}) {
  const [draft, setDraft] = useState("");
  const add = (raw: string) => {
    const parts = raw.split(",").map((p) => p.trim()).filter(Boolean);
    if (!parts.length) return;
    onChange([...value, ...parts.filter((p) => !value.includes(p))]);
    setDraft("");
  };
  if (options) {
    return (
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => {
          const on = value.includes(o);
          return (
            <button
              type="button"
              key={o}
              onClick={() => onChange(on ? value.filter((x) => x !== o) : [...value, o])}
              className={cn(
                "h-6 rounded-md border px-2 text-xs transition-colors",
                on ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:text-fg",
              )}
            >
              {o}
            </button>
          );
        })}
      </div>
    );
  }
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-1 rounded-lg border border-border bg-panel px-1.5 py-1 shadow-card focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/25">
      {value.map((t) => (
        <span key={t} className="inline-flex h-5 items-center gap-1 rounded bg-subtle pr-0.5 pl-1.5 font-mono text-[11px]">
          {t}
          <button type="button" className="rounded p-0.5 text-faint hover:text-fg" onClick={() => onChange(value.filter((x) => x !== t))} aria-label={`Remove ${t}`}>
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            add(draft);
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={() => add(draft)}
        placeholder={value.length ? "" : placeholder}
        className="h-5 min-w-24 flex-1 bg-transparent px-1 text-[13px] outline-none placeholder:text-faint"
      />
    </div>
  );
}

export function KeyValueInput({ value, onChange, keyPlaceholder = "key", valuePlaceholder = "value", valueWidget }: {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  valueWidget?: (v: string, set: (v: string) => void) => ReactNode;
}) {
  const [rows, setRows] = useState<[string, string][]>(() => Object.entries(value ?? {}));
  const commit = (next: [string, string][]) => {
    setRows(next);
    onChange(Object.fromEntries(next.filter(([k]) => k.trim())));
  };
  return (
    <div className="space-y-1.5">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex gap-1.5">
          <Input mono className="w-2/5" value={k} placeholder={keyPlaceholder} onChange={(e) => commit(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <div className="min-w-0 flex-1">
            {valueWidget ? (
              valueWidget(v, (nv) => commit(rows.map((r, j) => (j === i ? [r[0], nv] : r))))
            ) : (
              <Input mono value={v} placeholder={valuePlaceholder} onChange={(e) => commit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
            )}
          </div>
          <Button variant="ghost" size="icon" type="button" onClick={() => commit(rows.filter((_, j) => j !== i))} aria-label="Remove">
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      ))}
      <Button variant="ghost" size="sm" type="button" icon={<Plus className="size-3.5" />} onClick={() => setRows([...rows, ["", ""]])}>
        Add
      </Button>
    </div>
  );
}

function ObjectList({ value, onChange, schema, root }: {
  value: Record<string, unknown>[];
  onChange: (v: Record<string, unknown>[]) => void;
  schema: JsonSchema;
  root: JsonSchema;
}) {
  return (
    <div className="space-y-2">
      {value.map((item, i) => (
        <div key={i} className="relative rounded-lg border border-border bg-subtle/30 p-3 pr-10">
          <Button variant="ghost" size="icon-sm" type="button" className="absolute top-2 right-2" onClick={() => onChange(value.filter((_, j) => j !== i))} aria-label="Remove item">
            <Trash2 className="size-3.5" />
          </Button>
          <SchemaForm schema={schema} root={root} value={item} onChange={(o) => onChange(value.map((x, j) => (j === i ? o : x)))} />
        </div>
      ))}
      <Button variant="secondary" size="sm" type="button" icon={<Plus className="size-3.5" />} onClick={() => onChange([...value, {}])}>
        Add item
      </Button>
    </div>
  );
}

export function JsonField({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(() => (value === undefined ? "" : JSON.stringify(value, null, 2)));
  const [bad, setBad] = useState(false);
  return (
    <div className={cn("rounded-lg border bg-panel", bad ? "border-bad" : "border-border")}>
      <CodeEditor
        value={text}
        language="json"
        lineNumbers={false}
        minHeight="60px"
        maxHeight="240px"
        placeholder="JSON value"
        onChange={(t) => {
          setText(t);
          if (!t.trim()) {
            setBad(false);
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(t));
            setBad(false);
          } catch {
            setBad(true);
          }
        }}
      />
    </div>
  );
}
