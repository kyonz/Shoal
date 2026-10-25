import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Lock, Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { JsonSchema } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { CopyButton, ErrorNotice, Spinner } from "@/components/ui";
import { slugify } from "../registry";
import { deref, FieldsTable, makeLookup, typeLabel, SectionAnchor, type RefLookup } from "./SchemaFields";

interface Parameter {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required?: boolean;
  description?: string;
  schema?: JsonSchema;
}

interface Operation {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  parameters?: Parameter[];
  requestBody?: { required?: boolean; content?: Record<string, { schema?: JsonSchema }> };
  responses?: Record<string, { description?: string; content?: Record<string, { schema?: JsonSchema }> }>;
}

interface OpenApi {
  info: { title: string; version: string; description?: string };
  paths: Record<string, Record<string, Operation>>;
  components?: { schemas?: Record<string, JsonSchema> };
}

interface Op extends Operation {
  method: string;
  path: string;
}

const METHOD_STYLE: Record<string, string> = {
  get: "bg-info-soft text-info border-info/20",
  post: "bg-ok-soft text-ok border-ok/20",
  put: "bg-warn-soft text-warn border-warn/20",
  patch: "bg-warn-soft text-warn border-warn/20",
  delete: "bg-bad-soft text-bad border-bad/20",
};

const PUBLIC = /^\/v1\/(info|auth\/|hooks\/|openapi)/;

function Method({ m }: { m: string }) {
  return (
    <span className={cn("inline-flex h-5 w-14 shrink-0 items-center justify-center rounded border font-mono text-[10.5px] font-semibold uppercase", METHOD_STYLE[m] ?? "border-border bg-subtle text-muted")}>
      {m}
    </span>
  );
}

function SchemaSummary({ schema, lookup }: { schema: JsonSchema | undefined; lookup: RefLookup }) {
  if (!schema) return <span className="text-faint">—</span>;
  const { schema: s } = deref(schema.type === "array" && schema.items ? schema.items : schema, lookup);
  const label = typeLabel(schema, lookup);
  return (
    <div>
      <code className="font-mono text-[12px] text-info">{label}</code>
      {s.properties && Object.keys(s.properties).length > 0 && (
        <div className="mt-2">
          <FieldsTable schema={s} lookup={lookup} />
        </div>
      )}
    </div>
  );
}

function OperationCard({ op, lookup }: { op: Op; lookup: RefLookup }) {
  const [open, setOpen] = useState(false);
  const params = op.parameters ?? [];
  const body = op.requestBody?.content ? Object.entries(op.requestBody.content)[0] : undefined;
  const responses = Object.entries(op.responses ?? {});
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-panel shadow-card">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-subtle/60">
        <Method m={op.method} />
        <code className="min-w-0 truncate font-mono text-[12.5px] text-fg">{op.path}</code>
        <span className="min-w-0 flex-1 truncate text-right text-[12.5px] text-muted">{op.summary}</span>
        {!PUBLIC.test(op.path) && <Lock className="size-3 shrink-0 text-faint" aria-label="Needs authentication" />}
        <ChevronRight className={cn("size-3.5 shrink-0 text-faint transition-transform", open && "rotate-90")} />
      </button>
      {open && (
        <div className="space-y-4 border-t border-border bg-bg/40 px-4 py-4 text-[13px]">
          <div className="flex items-center gap-2">
            <code className="flex-1 truncate rounded-md border border-border bg-subtle px-2 py-1 font-mono text-[12px]">
              {op.method.toUpperCase()} {op.path}
            </code>
            <CopyButton value={op.path} label="Copy path" />
          </div>
          {op.description && <p className="whitespace-pre-line text-muted">{op.description}</p>}
          {params.length > 0 && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Parameters</h4>
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-[13px]">
                  <tbody>
                    {params.map((p) => (
                      <tr key={`${p.in}:${p.name}`} className="border-t border-border first:border-t-0 align-top">
                        <td className="w-1/3 px-3 py-2">
                          <code className="font-mono text-[12.5px] font-medium">{p.name}</code>
                          {p.required && <span className="ml-1.5 rounded bg-bad-soft px-1 text-[10px] font-semibold text-bad uppercase">required</span>}
                        </td>
                        <td className="w-24 px-3 py-2 text-xs text-faint">{p.in}</td>
                        <td className="px-3 py-2">
                          <code className="font-mono text-[12px] text-info">{typeLabel(p.schema, lookup)}</code>
                          {(p.description ?? p.schema?.description) && <div className="mt-0.5 text-muted">{p.description ?? p.schema?.description}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {body && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">
                Request body <span className="ml-1 font-mono font-normal tracking-normal normal-case">{body[0]}</span>
              </h4>
              <SchemaSummary schema={body[1].schema} lookup={lookup} />
            </div>
          )}
          {responses.length > 0 && (
            <div>
              <h4 className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">Responses</h4>
              <div className="space-y-2">
                {responses.map(([status, r]) => {
                  const content = r.content ? Object.values(r.content)[0] : undefined;
                  const schema = content?.schema;
                  const empty = !schema || Object.keys(schema).length === 0;
                  return (
                    <div key={status} className="flex gap-3">
                      <span className={cn("w-10 shrink-0 font-mono text-[12px] font-semibold", status.startsWith("2") ? "text-ok" : "text-warn")}>{status}</span>
                      <div className="min-w-0 flex-1">
                        <div className="text-muted">{r.description}</div>
                        {!empty && status.startsWith("2") && (
                          <div className="mt-1">
                            <SchemaSummary schema={schema} lookup={lookup} />
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ApiReference() {
  const spec = useQuery({
    queryKey: ["openapi"],
    queryFn: async () => {
      const r = await fetch("/v1/openapi.json");
      if (!r.ok) throw new Error(`Could not load /v1/openapi.json (HTTP ${r.status})`);
      return (await r.json()) as OpenApi;
    },
    staleTime: Infinity,
  });
  const [q, setQ] = useState("");

  const groups = useMemo(() => {
    const out = new Map<string, Op[]>();
    for (const [path, methods] of Object.entries(spec.data?.paths ?? {})) {
      for (const [method, op] of Object.entries(methods)) {
        if (!METHOD_STYLE[method]) continue;
        const tag = op.tags?.[0] ?? "other";
        const hay = `${method} ${path} ${op.summary ?? ""} ${op.description ?? ""} ${tag}`.toLowerCase();
        if (q && !q.toLowerCase().split(/\s+/).every((t) => hay.includes(t))) continue;
        out.set(tag, [...(out.get(tag) ?? []), { ...op, method, path }]);
      }
    }
    return [...out.entries()];
  }, [spec.data, q]);

  if (spec.isLoading) return <Spinner />;
  if (spec.error) return <ErrorNotice error={spec.error} />;
  const lookup = makeLookup(spec.data as unknown as JsonSchema);
  const count = groups.reduce((n, [, ops]) => n + ops.length, 0);

  return (
    <div>
      <p className="mb-4 text-[13px] leading-relaxed text-muted">
        {spec.data?.info.title} <code className="font-mono text-[12px]">v{spec.data?.info.version}</code>. Authenticate with{" "}
        <code className="font-mono text-[12px]">Authorization: Bearer &lt;api key&gt;</code>; errors are RFC 9457 problem documents with a stable{" "}
        <code className="font-mono text-[12px]">code</code>. The raw document is at{" "}
        <a className="text-accent underline underline-offset-2" href="/v1/openapi.json" target="_blank" rel="noreferrer">
          /v1/openapi.json
        </a>
        .
      </p>
      <div className="sticky top-0 z-10 -mx-1 mb-2 bg-bg/90 px-1 py-2 backdrop-blur">
        <div className="flex h-9 items-center gap-2 rounded-lg border border-border bg-panel px-3 shadow-card focus-within:border-ring">
          <Search className="size-3.5 text-faint" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter operations: path, method, summary…"
            className="flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint"
          />
          <span className="text-xs text-faint tabular-nums">{count} operations</span>
        </div>
      </div>
      {groups.length === 0 && <p className="py-8 text-center text-[13px] text-muted">No operations match “{q}”.</p>}
      {groups.map(([tag, ops]) => (
        <section key={tag}>
          <SectionAnchor id={`tag-${slugify(tag)}`} label={tag.charAt(0).toUpperCase() + tag.slice(1)} sub={`${ops.length} operation${ops.length === 1 ? "" : "s"}`}>
            <span className="capitalize">{tag}</span>
          </SectionAnchor>
          <div className="space-y-1.5">
            {ops.map((op) => (
              <OperationCard key={`${op.method} ${op.path}`} op={op} lookup={lookup} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
