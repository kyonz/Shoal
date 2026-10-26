/** Inspector inputs specific to workflow authoring. */
import { Check, ChevronDown, Plus, Search, Trash2, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { useIntegrations, useVisibleResources, type IntegrationAction, type JsonSchema, type Plural } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { useNs } from "@/lib/ns";
import { Badge, Button, Input, NativeSelect, Popover, Switch } from "@/components/ui";
import { humanize, resolve } from "@/components/forms/SchemaForm";
import { ExpressionInput } from "@/features/workflow/ExpressionInput";
import type { ParamDoc } from "@/features/workflow/types";
import { ResourceOptions } from "@/pages/catalog/widgets";

// --------------------------------------------------------------------------- resource pickers

export function ResourceSelect({ plural, value, onChange, placeholder = "—", filter, allowEmpty = true }: {
  plural: Plural;
  value: string | undefined;
  onChange: (v: string | undefined) => void;
  placeholder?: string;
  filter?: (spec: Record<string, unknown>) => boolean;
  allowEmpty?: boolean;
}) {
  const ns = useNs();
  const { data, isLoading } = useVisibleResources(plural, ns);
  const items = data.filter((r) => !filter || filter(r.spec));
  const missing = value && !isLoading && !items.some((r) => r.metadata.name === value);
  return (
    <NativeSelect value={value ?? ""} onChange={(e) => onChange(e.target.value || undefined)} className={cn(missing && "border-warn")}>
      {allowEmpty && <option value="">{placeholder}</option>}
      {missing && <option value={value}>{value} (not found)</option>}
      <ResourceOptions items={items} />
    </NativeSelect>
  );
}

export function ResourceTags({ plural, value, onChange }: { plural: Plural; value: string[]; onChange: (v: string[]) => void }) {
  const ns = useNs();
  const { data } = useVisibleResources(plural, ns);
  const names = data.map((r) => r.metadata.name);
  const shared = new Set(data.filter((r) => r.shared).map((r) => r.metadata.name));
  const all = [...new Set([...names, ...value])];
  if (!all.length) return <p className="text-xs text-faint">None in this namespace or the shared namespace yet.</p>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {all.map((n) => {
        const on = value.includes(n);
        return (
          <button
            type="button"
            key={n}
            onClick={() => onChange(on ? value.filter((x) => x !== n) : [...value, n])}
            className={cn(
              "inline-flex h-6 items-center gap-1 rounded-md border px-2 text-xs transition-colors",
              on ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:text-fg",
              !names.includes(n) && "border-warn/50",
            )}
          >
            {on && <Check className="size-3" />}
            {n}
            {shared.has(n) && <span className="text-[10px] text-faint">shared</span>}
          </button>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------- action picker

export function ActionPicker({ value, onChange }: { value: string | undefined; onChange: (v: string) => void }) {
  const { data: integrations = [] } = useIntegrations();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [key, name] = (value ?? "").split(".");
  const current = integrations.find((i) => i.key === key)?.actions.find((a) => a.name === name);
  const groups = integrations
    .map((i) => ({
      i,
      actions: i.actions.filter((a) => !q || `${i.key}.${a.name} ${a.description}`.toLowerCase().includes(q.toLowerCase())),
    }))
    .filter((g) => g.actions.length);
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      className="w-[360px] p-0"
      trigger={
        <button
          type="button"
          className="flex h-auto min-h-8 w-full items-center gap-2 rounded-lg border border-border bg-panel px-2.5 py-1.5 text-left shadow-card hover:border-border-strong"
        >
          <Zap className="size-3.5 shrink-0 text-accent" />
          <span className="min-w-0 flex-1">
            {value ? (
              <>
                <span className="block truncate font-mono text-xs">{value}</span>
                {current && <span className="block truncate text-[11px] text-faint">{current.description}</span>}
              </>
            ) : (
              <span className="text-[13px] text-faint">Choose an action…</span>
            )}
          </span>
          <ChevronDown className="size-3.5 text-faint" />
        </button>
      }
    >
      <div className="flex items-center gap-2 border-b border-border px-3">
        <Search className="size-3.5 text-faint" />
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search actions…" className="h-10 flex-1 bg-transparent text-[13px] outline-none" />
      </div>
      <div className="max-h-80 overflow-y-auto p-1">
        {groups.map(({ i, actions }) => (
          <div key={i.key} className="mb-1">
            <div className="px-2 pt-1.5 pb-1 text-[11px] font-semibold tracking-wide text-faint uppercase">{i.title}</div>
            {actions.map((a) => {
              const full = `${i.key}.${a.name}`;
              return (
                <button
                  type="button"
                  key={full}
                  onClick={() => {
                    onChange(full);
                    setOpen(false);
                  }}
                  className={cn("flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-subtle", full === value && "bg-accent-soft")}
                >
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="font-mono text-xs">{a.name}</span>
                      {a.mutating && <Badge tone="warn">writes</Badge>}
                    </span>
                    <span className="block text-[11px] leading-snug text-faint">{a.description}</span>
                  </span>
                </button>
              );
            })}
          </div>
        ))}
        {!groups.length && <div className="px-3 py-6 text-center text-xs text-faint">No actions match.</div>}
      </div>
    </Popover>
  );
}

export function useAction(full: string | undefined): IntegrationAction | undefined {
  const { data } = useIntegrations();
  return useMemo(() => {
    const [key, name] = (full ?? "").split(".");
    return data?.find((i) => i.key === key)?.actions.find((a) => a.name === name);
  }, [data, full]);
}

// --------------------------------------------------------------------------- expression params

function toText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

function fromText(text: string, type: string | undefined): unknown {
  if (text === "") return undefined;
  if (text.includes("{{")) return text;
  if ((type === "integer" || type === "number") && /^-?\d+(\.\d+)?$/.test(text.trim())) return Number(text);
  if (type === "boolean" && /^(true|false)$/i.test(text.trim())) return text.trim().toLowerCase() === "true";
  if ((type === "array" || type === "object") && /^[[{]/.test(text.trim())) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

function schemaType(s: JsonSchema, root: JsonSchema): string | undefined {
  const r = resolve(s, root);
  if (r.anyOf) return r.anyOf.map((x) => resolve(x, root).type).find((t) => t && t !== "null") as string | undefined;
  return Array.isArray(r.type) ? r.type.find((t) => t !== "null") : r.type;
}

/** Inputs for an action or sub-workflow: every value may be an expression. */
export function ParamValues({ schema, value, onChange }: {
  schema: JsonSchema | undefined;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const props = schema?.properties ?? {};
  const required = new Set(schema?.required ?? []);
  const known = Object.keys(props);
  const extra = Object.keys(value).filter((k) => !known.includes(k));
  const [adding, setAdding] = useState("");
  const set = (k: string, v: unknown) => {
    const next = { ...value };
    if (v === undefined) delete next[k];
    else next[k] = v;
    onChange(next);
  };
  return (
    <div className="space-y-3">
      {known.map((k) => {
        const s = resolve(props[k], schema!);
        const type = schemaType(props[k], schema!);
        const multiline = type === "string" && /body|description|message|text|content|prompt/.test(k);
        return (
          <div key={k}>
            <div className="mb-1 flex items-baseline gap-2 text-xs">
              <span className="font-medium">
                {s.title ?? humanize(k)}
                {required.has(k) && <span className="ml-0.5 text-bad">*</span>}
              </span>
              <span className="font-mono text-[10.5px] text-faint">{type ?? "any"}</span>
            </div>
            <ExpressionInput
              value={toText(value[k])}
              onChange={(t) => set(k, fromText(t, type))}
              multiline={multiline || type === "object" || type === "array"}
              placeholder={s.default != null ? `Default: ${toText(s.default)}` : s.enum ? (s.enum as unknown[]).join(" | ") : undefined}
            />
            {s.description && <p className="mt-1 text-[11px] leading-snug text-faint">{s.description}</p>}
          </div>
        );
      })}
      {extra.map((k) => (
        <div key={k}>
          <div className="mb-1 flex items-center gap-2 text-xs">
            <span className="font-mono font-medium">{k}</span>
            <Badge tone="warn">not in schema</Badge>
            <button type="button" className="ml-auto text-faint hover:text-bad" onClick={() => set(k, undefined)} aria-label={`Remove ${k}`}>
              <Trash2 className="size-3.5" />
            </button>
          </div>
          <ExpressionInput value={toText(value[k])} onChange={(t) => set(k, fromText(t, undefined))} />
        </div>
      ))}
      {!schema && (
        <div className="flex gap-1.5">
          <Input mono value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="parameter name" />
          <Button
            type="button"
            size="md"
            icon={<Plus className="size-3.5" />}
            disabled={!adding.trim()}
            onClick={() => {
              set(adding.trim(), "");
              setAdding("");
            }}
          >
            Add
          </Button>
        </div>
      )}
    </div>
  );
}

/** name -> expression map (set values, workflow outputs, sub-workflow inputs). */
export function ExpressionMap({ value, onChange, keyPlaceholder = "name", addLabel = "Add value" }: {
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  keyPlaceholder?: string;
  addLabel?: string;
}) {
  const entries = Object.entries(value ?? {});
  const rename = (from: string, to: string) => {
    if (!to || to === from || to in value) return;
    onChange(Object.fromEntries(entries.map(([k, v]) => [k === from ? to : k, v])));
  };
  return (
    <div className="space-y-2">
      {entries.map(([k, v]) => (
        <div key={k} className="rounded-lg border border-border bg-subtle/30 p-2">
          <div className="mb-1.5 flex items-center gap-1.5">
            <RenameInput value={k} onRename={(to) => rename(k, to)} placeholder={keyPlaceholder} />
            <Button type="button" variant="ghost" size="icon-sm" onClick={() => onChange(Object.fromEntries(entries.filter(([x]) => x !== k)))} aria-label={`Remove ${k}`}>
              <Trash2 className="size-3.5" />
            </Button>
          </div>
          <ExpressionInput value={toText(v)} onChange={(t) => onChange({ ...value, [k]: fromText(t, undefined) ?? "" })} />
        </div>
      ))}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        icon={<Plus className="size-3.5" />}
        onClick={() => {
          let n = 1;
          while (`${keyPlaceholder}_${n}` in (value ?? {})) n++;
          onChange({ ...value, [`${keyPlaceholder}_${n}`]: "" });
        }}
      >
        {addLabel}
      </Button>
    </div>
  );
}

export function RenameInput({ value, onRename, placeholder, className }: {
  value: string;
  onRename: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const [draft, setDraft] = useState(value);
  const [focused, setFocused] = useState(false);
  return (
    <input
      value={focused ? draft : value}
      placeholder={placeholder}
      onFocus={() => {
        setDraft(value);
        setFocused(true);
      }}
      onChange={(e) => setDraft(e.target.value.replace(/[^a-zA-Z0-9_]/g, "_"))}
      onBlur={() => {
        setFocused(false);
        if (draft !== value) onRename(draft);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        if (e.key === "Escape") {
          setDraft(value);
          (e.target as HTMLInputElement).blur();
        }
      }}
      className={cn("h-7 min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1.5 font-mono text-xs font-medium hover:border-border focus:border-ring focus:bg-panel focus:outline-none", className)}
    />
  );
}

// --------------------------------------------------------------------------- typed parameters (inputs/outputs)

const PARAM_TYPES = ["string", "integer", "number", "boolean", "object", "array", "any"] as const;

export function ParamsEditor({ value, onChange, kind }: {
  value: Record<string, ParamDoc>;
  onChange: (v: Record<string, ParamDoc>) => void;
  kind: "input" | "output";
}) {
  const entries = Object.entries(value ?? {});
  const [open, setOpen] = useState<string | null>(null);
  const update = (k: string, p: ParamDoc) => onChange({ ...value, [k]: compactParam(p) });
  const rename = (from: string, to: string) => {
    if (!to || to === from || to in value) return;
    onChange(Object.fromEntries(entries.map(([k, v]) => [k === from ? to : k, v])));
  };
  return (
    <div className="space-y-1.5">
      {entries.map(([k, param]) => {
        const p: ParamDoc = param ?? {};
        const required = p.required ?? p.default === undefined;
        return (
          <div key={k} className="rounded-lg border border-border bg-panel">
            <div className="flex items-center gap-1 px-1.5 py-1">
              <RenameInput value={k} onRename={(to) => rename(k, to)} />
              <NativeSelect value={p.type ?? "string"} onChange={(e) => update(k, { ...p, type: e.target.value as ParamDoc["type"] })} className="h-7 w-24 text-xs">
                {PARAM_TYPES.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </NativeSelect>
              <Button type="button" variant="ghost" size="icon-sm" onClick={() => setOpen(open === k ? null : k)} aria-label="More">
                <ChevronDown className={cn("size-3.5 transition-transform", open === k && "rotate-180")} />
              </Button>
              <Button type="button" variant="ghost" size="icon-sm" onClick={() => onChange(Object.fromEntries(entries.filter(([x]) => x !== k)))} aria-label={`Remove ${k}`}>
                <Trash2 className="size-3.5" />
              </Button>
            </div>
            {open === k && (
              <div className="space-y-2 border-t border-border p-2">
                <Input value={p.description ?? ""} placeholder="Description" onChange={(e) => update(k, { ...p, description: e.target.value || undefined })} />
                {kind === "input" && (
                  <Input
                    mono
                    value={p.default == null ? "" : typeof p.default === "string" ? p.default : JSON.stringify(p.default)}
                    placeholder="Default value"
                    onChange={(e) => update(k, { ...p, default: fromText(e.target.value, p.type) })}
                  />
                )}
                {kind === "input" && (p.type ?? "string") === "string" && (
                  <Input
                    value={(p.enum ?? []).join(", ")}
                    placeholder="Allowed values (comma separated, optional)"
                    onChange={(e) => {
                      const list = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
                      update(k, { ...p, enum: list.length ? list : undefined });
                    }}
                  />
                )}
                <Switch checked={required} onCheckedChange={(c) => update(k, { ...p, required: c === (p.default === undefined) ? undefined : c })} label={<span className="text-xs">Required</span>} />
              </div>
            )}
            {open !== k && p.description && <div className="truncate px-3 pb-1.5 text-[11px] text-faint">{p.description}</div>}
          </div>
        );
      })}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        icon={<Plus className="size-3.5" />}
        onClick={() => {
          let n = 1;
          const base = kind === "input" ? "input" : "result";
          while (`${base}_${n}` in (value ?? {})) n++;
          onChange({ ...value, [`${base}_${n}`]: { type: "string" } });
          setOpen(`${base}_${n}`);
        }}
      >
        Add {kind}
      </Button>
    </div>
  );
}

function compactParam(p: ParamDoc): ParamDoc {
  const out: ParamDoc = {};
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== "") (out as Record<string, unknown>)[k] = v;
  if (out.type === "string" && Object.keys(out).length > 1) {
    /* keep explicit type for clarity */
  }
  return out;
}

export function Section({ title, children, action, description }: { title: string; children: React.ReactNode; action?: React.ReactNode; description?: React.ReactNode }) {
  return (
    <section className="border-b border-border px-4 py-4 last:border-b-0">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold tracking-wider text-faint uppercase">{title}</h4>
        {action}
      </div>
      {description && <p className="-mt-1 mb-2.5 text-xs text-faint">{description}</p>}
      {children}
    </section>
  );
}
