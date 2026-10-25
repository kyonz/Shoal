import { useQueries, useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useId, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { rawJson } from "@/api/client";
import { useIntegrations, useVisibleResources, type JsonSchema, type Plural, type VisibleResource } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { CodeEditor, Input, NativeSelect, Segmented } from "@/components/ui";
import { CLAUDE_MODELS } from "./kinds";

export function useSandboxBackends() {
  return useQuery({
    queryKey: ["catalog", "sandbox-backends"],
    queryFn: () => rawJson<{ name: string; description: string; config_schema: JsonSchema | null }[]>("/v1/sandbox-backends"),
    staleTime: 300_000,
  });
}

export function useProviders() {
  return useQuery({
    queryKey: ["catalog", "providers"],
    queryFn: () => rawJson<{ name: string; description: string }[]>("/v1/providers"),
    staleTime: 300_000,
  });
}

/** Free text with suggestions (datalist), e.g. model names. */
export function Combo({ value, onChange, options, placeholder, mono }: {
  value: string | undefined;
  onChange: (v: string | undefined) => void;
  options: { value: string; label?: string }[];
  placeholder?: string;
  mono?: boolean;
}) {
  const id = useId();
  return (
    <>
      <Input mono={mono} list={id} value={value ?? ""} placeholder={placeholder} onChange={(e) => onChange(e.target.value || undefined)} />
      <datalist id={id}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </datalist>
    </>
  );
}

export function ModelCombo({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  return (
    <Combo
      mono
      value={value as string | undefined}
      onChange={onChange}
      placeholder="claude-opus-5-5 or policy:coding"
      options={[...CLAUDE_MODELS.map((m) => ({ value: m })), { value: "policy:", label: "Use a model policy" }]}
    />
  );
}

function resourceLabel(r: VisibleResource) {
  return `${r.metadata.name}${r.metadata.description ? ` — ${r.metadata.description.slice(0, 60)}` : ""}`;
}

/** Options for a native select: the namespace's own resources, then a "Shared" group. */
export function ResourceOptions({ items }: { items: VisibleResource[] }) {
  const own = items.filter((r) => !r.shared);
  const shared = items.filter((r) => r.shared);
  const options = (list: VisibleResource[]) =>
    list.map((r) => (
      <option key={r.metadata.name} value={r.metadata.name}>
        {resourceLabel(r)}
      </option>
    ));
  if (!shared.length) return <>{options(own)}</>;
  return (
    <>
      {own.length > 0 && <optgroup label="This namespace">{options(own)}</optgroup>}
      <optgroup label="Shared">{options(shared)}</optgroup>
    </>
  );
}

/** Pick one resource of a kind visible from the namespace (its own, then the shared namespace's). */
export function ResourcePicker({ plural, ns, value, onChange, placeholder = "None" }: {
  plural: Plural;
  ns: string;
  value: unknown;
  onChange: (v: unknown) => void;
  placeholder?: string;
}) {
  const { data, isLoading } = useVisibleResources(plural, ns);
  const v = (value as string | undefined) ?? "";
  const missing = v && !isLoading && !data.some((r) => r.metadata.name === v);
  return (
    <NativeSelect value={v} onChange={(e) => onChange(e.target.value || undefined)}>
      <option value="">{placeholder}</option>
      {missing && <option value={v}>{v} (not found)</option>}
      <ResourceOptions items={data} />
    </NativeSelect>
  );
}

/** Pick several resources of a kind, shown as removable chips. */
export function ResourceMultiPicker({ plural, ns, value, onChange, exclude }: {
  plural: Plural;
  ns: string;
  value: unknown;
  onChange: (v: unknown) => void;
  exclude?: string;
}) {
  const { data } = useVisibleResources(plural, ns);
  const list = (value as string[] | undefined) ?? [];
  const options = data.filter((r) => !list.includes(r.metadata.name) && r.metadata.name !== exclude);
  const [draft, setDraft] = useState("");
  const add = (n: string) => {
    if (!n || list.includes(n)) return;
    onChange([...list, n]);
    setDraft("");
  };
  return (
    <div className="flex min-h-8 flex-wrap items-center gap-1 rounded-lg border border-border bg-panel px-1.5 py-1 shadow-card">
      {list.map((n) => (
        <span key={n} className="inline-flex h-5 items-center gap-1 rounded bg-accent-soft pr-0.5 pl-1.5 font-mono text-[11px] text-accent">
          {n}
          <button type="button" aria-label={`Remove ${n}`} className="rounded p-0.5 hover:text-fg" onClick={() => {
            const next = list.filter((x) => x !== n);
            onChange(next.length ? next : undefined);
          }}>
            <X className="size-3" />
          </button>
        </span>
      ))}
      <select
        value={draft}
        onChange={(e) => add(e.target.value)}
        className="h-5 min-w-28 flex-1 cursor-pointer bg-transparent text-[13px] text-muted outline-none"
      >
        <option value="">{options.length ? "+ Add…" : list.length ? "" : "None available"}</option>
        <ResourceOptions items={options} />
      </select>
    </div>
  );
}

/** Tall Markdown editor with Edit / Preview / Split modes. */
export function MarkdownEditor({ value, onChange, minHeight = "260px" }: {
  value: unknown;
  onChange: (v: unknown) => void;
  minHeight?: string;
}) {
  const [mode, setMode] = useState<"edit" | "preview" | "split">("edit");
  const text = (value as string | undefined) ?? "";
  const editor = (
    <div className="min-w-0 flex-1 rounded-lg border border-border bg-panel">
      <CodeEditor value={text} language="text" lineNumbers={false} minHeight={minHeight} maxHeight="640px" onChange={(t) => onChange(t || undefined)} placeholder="Markdown…" />
    </div>
  );
  const preview = (
    <div className={cn("min-w-0 flex-1 overflow-y-auto rounded-lg border border-border bg-subtle/40 px-4 py-3")} style={{ minHeight, maxHeight: 640 }}>
      {text.trim() ? (
        <div className="prose-shoal text-[13px]">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
      ) : (
        <p className="text-[13px] text-faint">Nothing to preview.</p>
      )}
    </div>
  );
  return (
    <div>
      <div className="mb-1.5 flex justify-end">
        <Segmented
          value={mode}
          onChange={setMode}
          options={[
            { value: "edit", label: "Edit" },
            { value: "split", label: "Split" },
            { value: "preview", label: "Preview" },
          ]}
        />
      </div>
      <div className="flex gap-2">
        {mode !== "preview" && editor}
        {mode !== "edit" && preview}
      </div>
    </div>
  );
}

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("prose-shoal", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

// --------------------------------------------------------------------------- agent tools

export const BUILTIN_TOOLS = ["shell", "read_file", "write_file", "edit_file", "list_files"];

export interface ToolGroup {
  label: string;
  options: { value: string; label: string }[];
}

/** What an agent can be given: built-in tools, MCP servers (whole or per tool) and integration actions. */
export function useToolOptions(ns: string): ToolGroup[] {
  const integrations = useIntegrations();
  // MCP servers are connections; the engine also finds the shared namespace's.
  const connections = useVisibleResources("connections", ns);
  const servers = connections.data.filter((c) => (c.spec as { integration?: string }).integration === "mcp");
  const mcp = servers.map((c) => c.metadata.name);
  const tools = useQueries({
    queries: mcp.map((name) => ({
      queryKey: ["mcp-tools", ns, name],
      // Asked through `ns` so a shared server is listed with the credentials a run here would use.
      queryFn: () => rawJson<{ actions: { name: string; description: string }[] }>(`/v1/namespaces/${ns}/connections/${encodeURIComponent(name)}/actions`),
      retry: false,
      staleTime: 60_000,
    })),
  });
  const groups: ToolGroup[] = [{ label: "Built-in", options: BUILTIN_TOOLS.map((t) => ({ value: t, label: t })) }];
  mcp.forEach((name, i) => {
    const list = tools[i]?.data?.actions ?? [];
    groups.push({
      label: `MCP server: ${name}${servers[i].shared ? " (shared)" : ""}`,
      options: [
        { value: `mcp:${name}`, label: `mcp:${name} (all ${list.length ? `${list.length} ` : ""}tools)` },
        ...list.map((a) => ({ value: `mcp:${name}.${a.name}`, label: `mcp:${name}.${a.name}${a.description ? ` — ${a.description.slice(0, 60)}` : ""}` })),
      ],
    });
  });
  for (const it of integrations.data ?? []) {
    if (it.key === "mcp" || !it.actions.length) continue;
    groups.push({
      label: it.title,
      options: [{ value: `${it.key}.*`, label: `${it.key}.* (all actions)` }, ...it.actions.map((a) => ({ value: `${it.key}.${a.name}`, label: `${it.key}.${a.name}` }))],
    });
  }
  return groups;
}

function toolTone(t: string) {
  if (BUILTIN_TOOLS.includes(t)) return "bg-subtle text-fg";
  if (t.startsWith("mcp:")) return "bg-info-soft text-info";
  return "bg-accent-soft text-accent";
}

export function ToolsPicker({ ns, value, onChange }: { ns: string; value: unknown; onChange: (v: unknown) => void }) {
  const list = (value as string[] | undefined) ?? [];
  const groups = useToolOptions(ns);
  const [custom, setCustom] = useState("");
  const add = (t: string) => {
    const v = t.trim();
    if (!v || list.includes(v)) return;
    onChange([...list, v]);
  };
  const remove = (t: string) => {
    const next = list.filter((x) => x !== t);
    onChange(next.length ? next : undefined);
  };
  return (
    <div className="space-y-1.5">
      <div className="flex min-h-8 flex-wrap items-center gap-1 rounded-lg border border-border bg-panel px-1.5 py-1 shadow-card">
        {list.map((t) => (
          <span key={t} className={cn("inline-flex h-5 items-center gap-1 rounded pr-0.5 pl-1.5 font-mono text-[11px]", toolTone(t))}>
            {t}
            <button type="button" aria-label={`Remove ${t}`} className="rounded p-0.5 hover:text-fg" onClick={() => remove(t)}>
              <X className="size-3" />
            </button>
          </span>
        ))}
        <select value="" onChange={(e) => add(e.target.value)} className="h-5 min-w-28 flex-1 cursor-pointer bg-transparent text-[13px] text-muted outline-none" aria-label="Add a tool">
          <option value="">+ Add tool…</option>
          {groups.map((g) => {
            const opts = g.options.filter((o) => !list.includes(o.value));
            return opts.length ? (
              <optgroup key={g.label} label={g.label}>
                {opts.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </optgroup>
            ) : null;
          })}
        </select>
      </div>
      <Input
        mono
        className="h-7 text-[11px]"
        value={custom}
        placeholder="Or type one: github.create_pull_request, mcp:linear.create_issue…"
        onChange={(e) => setCustom(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add(custom);
            setCustom("");
          }
        }}
      />
    </div>
  );
}
