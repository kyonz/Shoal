/** Helpers shared by the connect and campaign pages. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { diffLines } from "diff";
import { Blocks, GitPullRequest, History, SquareKanban, Webhook } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";
import YAML from "yaml";
import { ApiError, rawJson, type Resource, type Schemas } from "@/api/client";
import { useResourceVersions, type Plural } from "@/api/hooks";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, CodeEditor, CopyButton, Dialog, EmptyState, Field, Input, Skeleton } from "@/components/ui";

// --------------------------------------------------------------------------- errors

/** Human-readable error lines from an ApiError (problem detail + field errors). */
export function errorLines(err: unknown): string[] {
  if (!err) return [];
  if (err instanceof ApiError) {
    const body = err.body as { errors?: { loc: string | string[]; msg: string }[]; details?: { errors?: { loc: string | string[]; msg: string }[] } } | undefined;
    const fields = body?.errors ?? body?.details?.errors ?? [];
    const lines = fields.map((e) => {
      const loc = Array.isArray(e.loc) ? e.loc.filter((p) => p !== "body").join(".") : e.loc;
      return loc ? `${loc}: ${e.msg}` : e.msg;
    });
    return [err.detail, ...lines];
  }
  return [err instanceof Error ? err.message : String(err)];
}

export function ErrorList({ error, className }: { error: unknown; className?: string }) {
  const lines = errorLines(error);
  if (!lines.length) return null;
  return (
    <div className={cn("rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad", className)}>
      <div className="font-medium">{lines[0]}</div>
      {lines.length > 1 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 font-mono text-xs">
          {lines.slice(1).map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- integration marks

const MARKS: Record<string, { icon: ReactNode; className: string }> = {
  github: { icon: <GitPullRequest />, className: "bg-[#24292f] text-white dark:bg-[#f0f3f6] dark:text-[#24292f]" },
  jira: { icon: <SquareKanban />, className: "bg-[#1868db] text-white" },
  webhook: { icon: <Webhook />, className: "bg-[#c2410c] text-white" },
};

export function IntegrationMark({ integrationKey, size = "md" }: { integrationKey: string; size?: "sm" | "md" | "lg" }) {
  const m = MARKS[integrationKey];
  const box = { sm: "size-6 rounded-md [&>svg]:size-3.5 text-[10px]", md: "size-9 rounded-lg [&>svg]:size-4.5 text-xs", lg: "size-12 rounded-xl [&>svg]:size-6 text-sm" }[size];
  return (
    <div className={cn("flex shrink-0 items-center justify-center font-bold uppercase shadow-card", box, m?.className ?? "bg-accent-soft text-accent")}>
      {m?.icon ?? (integrationKey ? integrationKey.slice(0, 2) : <Blocks />)}
    </div>
  );
}

// --------------------------------------------------------------------------- secrets

export function useSecrets(ns: string, enabled = true) {
  return useQuery({
    queryKey: ["secrets", ns],
    queryFn: () => rawJson<Schemas["SecretOut"][]>(`/v1/namespaces/${ns}/secrets`),
    enabled,
  });
}

export function usePutSecret(ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) =>
      rawJson<Schemas["SecretOut"]>(`/v1/namespaces/${ns}/secrets/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["secrets", ns] }),
  });
}

/** Write-only secret value dialog (create or rotate). */
export function SecretDialog({ ns, open, onOpenChange, initialName, lockName, onSaved }: {
  ns: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  initialName?: string;
  lockName?: boolean;
  onSaved?: (name: string) => void;
}) {
  const put = usePutSecret(ns);
  const [name, setName] = useState(initialName ?? "");
  const [value, setValue] = useState("");
  const [lastOpen, setLastOpen] = useState(false);
  if (open !== lastOpen) {
    setLastOpen(open);
    if (open) {
      setName(initialName ?? "");
      setValue("");
      put.reset();
    }
  }
  const valid = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(name) && value.length > 0;
  const save = async () => {
    await put.mutateAsync({ name, value });
    onSaved?.(name);
    onOpenChange(false);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={lockName ? `Set value of ${name}` : "New secret"}
      description="Values are encrypted at rest and can never be read back, only replaced."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid} loading={put.isPending} onClick={() => void save()}>
            Save secret
          </Button>
        </>
      }
    >
      <form
        className="space-y-3.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) void save();
        }}
      >
        <Field label="Name" description="Lowercase letters, digits and dashes." required>
          <Input mono value={name} disabled={lockName} autoFocus={!lockName} onChange={(e) => setName(e.target.value)} placeholder="github-token" />
        </Field>
        <Field label="Value" required>
          <Input mono type="password" autoFocus={lockName} autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="••••••••" />
        </Field>
        <ErrorList error={put.error} />
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- yaml

export function toYaml(kind: string, name: string, metadata: { description?: string | null; labels?: Record<string, string> } | undefined, spec: unknown) {
  const md: Record<string, unknown> = { name };
  if (metadata?.description) md.description = metadata.description;
  if (metadata?.labels && Object.keys(metadata.labels).length) md.labels = metadata.labels;
  return YAML.stringify({ apiVersion: "awm.dev/v1", kind, metadata: md, spec }, { lineWidth: 0 });
}

/** Parse a resource YAML document back to {description, spec}; throws on bad YAML. */
export function fromYaml(text: string): { description?: string; spec: Record<string, unknown> } {
  const doc = YAML.parse(text) as { metadata?: { description?: string }; spec?: Record<string, unknown> } | null;
  if (!doc || typeof doc !== "object") throw new Error("Document is empty");
  if (!doc.spec || typeof doc.spec !== "object") throw new Error("Missing `spec`");
  return { description: doc.metadata?.description, spec: doc.spec };
}

/** Editable YAML of a resource (kept in sync by the caller). */
export function YamlPanel({ value, onChange, error, readOnly }: { value: string; onChange?: (v: string) => void; error?: string | null; readOnly?: boolean }) {
  return (
    <div>
      <div className={cn("relative rounded-xl border bg-panel shadow-card", error ? "border-bad" : "border-border")}>
        <div className="absolute top-1.5 right-1.5 z-10">
          <CopyButton value={value} />
        </div>
        <CodeEditor value={value} onChange={onChange} readOnly={readOnly} language="yaml" minHeight="320px" maxHeight="70vh" />
      </div>
      {error && <p className="mt-1.5 text-xs text-bad">{error}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- versions

export function VersionsPanel({ plural, ns, name, current }: { plural: Plural; ns: string; name: string; current?: Resource }) {
  const { data, isLoading } = useResourceVersions(plural, ns, name);
  const [selected, setSelected] = useState<number | null>(null);
  const versions = useMemo(() => [...(data ?? [])].sort((a, b) => b.version - a.version), [data]);
  const sel = versions.find((v) => v.version === selected) ?? versions[1] ?? versions[0];
  const latest = versions[0];
  const diff = useMemo(() => {
    if (!sel || !latest) return [];
    const a = YAML.stringify((sel.document as { spec?: unknown }).spec ?? sel.document, { lineWidth: 0 });
    const b = YAML.stringify((latest.document as { spec?: unknown }).spec ?? current?.spec ?? latest.document, { lineWidth: 0 });
    return diffLines(a, b);
  }, [sel, latest, current]);
  if (isLoading) return <Skeleton className="h-40" />;
  if (!versions.length) return <EmptyState icon={<History className="size-5" />} title="No history yet" description="Every save creates a new version." />;
  return (
    <div className="grid gap-4 md:grid-cols-[240px_1fr]">
      <Card className="overflow-hidden p-1">
        {versions.map((v) => (
          <button
            key={v.version}
            onClick={() => setSelected(v.version)}
            className={cn("flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] hover:bg-subtle", sel?.version === v.version && "bg-subtle")}
          >
            <Badge tone={v.version === latest?.version ? "accent" : "neutral"}>v{v.version}</Badge>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs">{v.created_by ?? "unknown"}</span>
              <span className="block text-[11px] text-faint">{relTime(v.created_at)}</span>
            </span>
          </button>
        ))}
      </Card>
      <Card className="min-w-0 overflow-hidden">
        <CardHeader
          title={sel && latest && sel.version !== latest.version ? `Changes from v${sel.version} to v${latest.version}` : `v${latest?.version} (current)`}
          description="Spec diff; green lines were added, red lines removed."
        />
        <pre className="max-h-[60vh] overflow-auto py-2 font-mono text-[12px] leading-5">
          {diff.map((part, i) =>
            part.value
              .replace(/\n$/, "")
              .split("\n")
              .map((line, j) => (
                <div key={`${i}-${j}`} className={cn("px-3 whitespace-pre-wrap", part.added && "bg-ok-soft text-ok", part.removed && "bg-bad-soft text-bad")}>
                  <span className="mr-2 inline-block w-3 text-faint select-none">{part.added ? "+" : part.removed ? "−" : " "}</span>
                  {line}
                </div>
              )),
          )}
        </pre>
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------------- small bits

/** Single-line expression input with `{{ }}` highlighting. */
export function ExprInput({ value, onChange, placeholder, multiline }: { value: string; onChange: (v: string) => void; placeholder?: string; multiline?: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-panel shadow-card transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/25 hover:border-border-strong [&_.cm-content]:!py-[5px]">
      <CodeEditor value={value} onChange={onChange} language="text" lineNumbers={false} placeholder={placeholder} minHeight={multiline ? "64px" : undefined} maxHeight="200px" />
    </div>
  );
}

export function RowSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9" />
      ))}
    </div>
  );
}

export function Crumb({ to, children }: { to: string; children: ReactNode }) {
  return (
    <>
      <Link to={to} className="hover:text-fg">
        {children}
      </Link>
      <span className="text-faint">/</span>
    </>
  );
}

/** A labelled radio-card choice. */
export function ChoiceCard({ selected, onClick, icon, title, description }: { selected: boolean; onClick: () => void; icon: ReactNode; title: string; description: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex items-start gap-3 rounded-xl border p-3 text-left transition-colors",
        selected ? "border-accent bg-accent-soft/60 ring-2 ring-accent/20" : "border-border bg-panel hover:border-border-strong",
      )}
    >
      <span className={cn("mt-0.5 [&>svg]:size-4", selected ? "text-accent" : "text-muted")}>{icon}</span>
      <span>
        <span className="block text-[13px] font-medium">{title}</span>
        <span className="mt-0.5 block text-xs text-muted">{description}</span>
      </span>
    </button>
  );
}

export const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * Two-way YAML for an editor: `open()` regenerates the text from form state
 * (call it when the YAML tab is shown); edits that parse are pushed back.
 */
export function useYamlSync(
  kind: string,
  name: string,
  description: string,
  spec: Record<string, unknown>,
  onParsed: (p: { description?: string; spec: Record<string, unknown> }) => void,
) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  return {
    text,
    error,
    open: () => {
      setText(toYaml(kind, name || "unnamed", { description }, spec));
      setError(null);
    },
    change: (t: string) => {
      setText(t);
      try {
        onParsed(fromYaml(t));
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
  };
}

export interface ConnectionCapabilities {
  integration: string;
  actions: { name: string; description: string; mutating: boolean; input_schema: import("@/api/hooks").JsonSchema; output_schema: import("@/api/hooks").JsonSchema | null }[];
  resources: { name: string; description: string; filter_schema: import("@/api/hooks").JsonSchema }[];
}

/**
 * Per-connection actions and resources (remote integrations only expose them
 * here). Resolves to null when the server does not have the endpoint yet.
 */
export function useConnectionCapabilities(ns: string, name: string | undefined) {
  return useQuery({
    queryKey: ["connection-actions", ns, name],
    queryFn: async () => {
      try {
        return await rawJson<ConnectionCapabilities>(`/v1/namespaces/${ns}/connections/${encodeURIComponent(name!)}/actions`);
      } catch (e) {
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) return null;
        throw e;
      }
    },
    enabled: !!name,
    retry: false,
    staleTime: 60_000,
  });
}
