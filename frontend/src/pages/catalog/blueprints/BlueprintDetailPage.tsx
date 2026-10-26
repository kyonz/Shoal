import { ChevronRight, Download, Pencil, Star } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "@/api/client";
import { useResource } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, CardHeader, EmptyState, Page, Segmented, Skeleton } from "@/components/ui";
import { LAYOUT_ANNOTATION, type WorkflowSpecDoc } from "@/features/workflow/types";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { isRecord } from "@/lib/shape";
import { WorkflowPreview, type PreviewDoc } from "../../workflows/builder/Preview";
import { Markdown } from "../widgets";
import { isFeatured, spec, useAdoptions, type BlueprintSpec } from "./api";
import { AdoptWizard, AdoptionsTable, BlueprintIcon, BlueprintStatus } from "./components";

export default function BlueprintDetailPage() {
  const ns = useNs();
  const { name } = useParams();
  const navigate = useNavigate();
  const res = useResource("blueprints", ns, name);
  const adoptions = useAdoptions(ns, name);
  const canAdopt = useCan("blueprints:adopt", ns);
  const canPublish = useCan("blueprints:publish");
  const [adopting, setAdopting] = useState(false);

  if (res.isLoading)
    return (
      <Page>
        <Skeleton className="mb-4 h-24 w-full" />
        <Skeleton className="h-96 w-full" />
      </Page>
    );
  if (res.error || !res.data)
    return (
      <Page>
        <EmptyState
          title={res.error instanceof ApiError && res.error.status === 404 ? "Blueprint not found" : "Could not load blueprint"}
          description={res.error?.message}
          action={<Button onClick={() => navigate(nsPath(ns, "blueprints"))}>Back to blueprints</Button>}
        />
      </Page>
    );

  const s = spec(res.data);
  const params = Object.entries(s.params ?? {});
  const restricted = !!s.namespaces?.length && !s.namespaces.includes(ns);
  const blocked =
    s.status === "draft" && !canPublish
      ? "Drafts cannot be adopted until published."
      : restricted
        ? `Only ${s.namespaces!.join(", ")} can adopt this.`
        : s.status === "deprecated"
          ? "This blueprint is deprecated and closed to new adoptions. Existing adoptions keep working and can still be upgraded."
          : null;
  const mine = adoptions.data ?? [];

  return (
    <Page wide>
      <div className="mb-1.5 flex items-center gap-1 text-xs text-muted">
        <Link to={nsPath(ns, "blueprints")} className="hover:text-fg">Blueprints</Link>
        <ChevronRight className="size-3" />
        <span className="text-fg">{s.title}</span>
      </div>
      <Card className="mb-5 overflow-hidden">
        <div className="relative flex flex-wrap items-start gap-4 bg-gradient-to-br from-accent-soft/60 via-transparent to-transparent p-5">
          <BlueprintIcon spec={s} className="size-16 text-3xl" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight">{s.title}</h1>
              {isFeatured(s) && (
                <Badge tone="warn">
                  <Star className="size-3 fill-current" /> Featured
                </Badge>
              )}
              {!isFeatured(s) && <BlueprintStatus status={s.status} />}
              <Badge className="font-mono">v{s.version}</Badge>
            </div>
            <p className="mt-1 max-w-3xl text-[13px] text-muted">{s.summary}</p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-faint">
              {s.author && <span>by {s.author}</span>}
              <span>· updated {relTime(res.data.metadata.updated_at)}</span>
              {(s.tags ?? []).map((t) => <Badge key={t}>{t}</Badge>)}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {canPublish && (
              <Button icon={<Pencil className="size-4" />} onClick={() => navigate(nsPath(ns, "blueprints", name!, "edit"))}>
                Edit
              </Button>
            )}
            {canAdopt && (
              <Button variant="primary" icon={<Download className="size-4" />} disabled={!!blocked} title={blocked ?? undefined} onClick={() => setAdopting(true)}>
                {mine.length ? "Adopt again" : `Adopt into ${ns}`}
              </Button>
            )}
          </div>
        </div>
        {blocked && <div className="border-t border-border bg-warn-soft px-5 py-2 text-[13px] text-warn">{blocked}</div>}
      </Card>

      <WorkflowsPreview spec={s} />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          {mine.length > 0 && (
            <Card>
              <CardHeader title={`Adopted in ${ns}`} />
              <AdoptionsTable ns={ns} adoptions={mine} specs={{ [name!]: s }} />
            </Card>
          )}
          <Card className="p-6">
            {s.readme?.trim() ? <Markdown>{s.readme}</Markdown> : <p className="text-[13px] text-faint">This blueprint has no README.</p>}
          </Card>
        </div>
        <div className="space-y-5">
          <Card>
            <CardHeader title="You'll provide" description="Parameters asked for when adopting." />
            {params.length ? (
              <ul className="divide-y divide-border">
                {params.map(([k, p]) => (
                  <li key={k} className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-medium">{k}</span>
                      <Badge tone={p.type === "connection" ? "accent" : p.type === "secret" ? "warn" : "neutral"}>
                        {p.type === "connection" ? `${p.integration ?? ""} connection` : p.type ?? "string"}
                      </Badge>
                      {p.required === false && <span className="text-[11px] text-faint">optional</span>}
                    </div>
                    {p.description && <p className="mt-0.5 text-xs text-muted">{p.description}</p>}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-4 py-3 text-[13px] text-faint">No parameters.</p>
            )}
          </Card>
          <Card>
            <CardHeader title="What's inside" description="Resources created in your namespace." />
            <ul className="divide-y divide-border">
              {(s.resources ?? []).map((r, i) => (
                <li key={i} className="flex items-center gap-2 px-4 py-2">
                  <Badge className="w-28 justify-center">{r.kind ?? "?"}</Badge>
                  <span className="truncate font-mono text-xs">{r.metadata?.name ?? "—"}</span>
                </li>
              ))}
            </ul>
          </Card>
          {(s.categories?.length ?? 0) > 0 && (
            <Card className="p-4 text-xs">
              <div className="mb-1.5 text-faint">Categories</div>
              <div className="flex flex-wrap gap-1">{s.categories!.map((c) => <Badge key={c} tone="info">{c}</Badge>)}</div>
            </Card>
          )}
        </div>
      </div>
      <AdoptWizard open={adopting} onOpenChange={setAdopting} name={name!} spec={s} ns={ns} />
    </Page>
  );
}

/** `{{ prefix }}` is filled in on adoption; a preview shows the names without it. */
const PREFIX = /\{\{\s*prefix\s*\}\}/g;

function WorkflowsPreview({ spec: s }: { spec: BlueprintSpec }) {
  const docs = useMemo<PreviewDoc[]>(
    () =>
      (s.resources ?? [])
        .filter((r) => isRecord(r) && r.kind === "Workflow" && isRecord(r.spec))
        .map((r) => JSON.parse(JSON.stringify(r).replace(PREFIX, "")) as typeof r)
        .map((r) => ({
          name: r.metadata?.name ?? "workflow",
          description: r.metadata?.description,
          labels: r.metadata?.labels,
          spec: { ...(r.spec as unknown as WorkflowSpecDoc), steps: Array.isArray(r.spec?.steps) ? (r.spec.steps as WorkflowSpecDoc["steps"]).filter(isRecord) : [] },
          layout: r.metadata?.annotations?.[LAYOUT_ANNOTATION],
        })),
    [s.resources],
  );
  const [current, setCurrent] = useState(0);
  const doc = docs[Math.min(current, docs.length - 1)];
  if (!doc) return null;
  return (
    <Card className="mb-5 overflow-hidden">
      <CardHeader
        title={docs.length > 1 ? "Workflows" : "Workflow"}
        description="A read-only look at what you'll get. Click a step to see how it is set up."
        actions={
          docs.length > 1 ? (
            <Segmented value={String(current)} onChange={(v) => setCurrent(Number(v))} options={docs.map((d, i) => ({ value: String(i), label: d.name }))} />
          ) : undefined
        }
      />
      <ErrorBoundary label="this workflow" resetKeys={[doc]}>
        <WorkflowPreview key={doc.name} doc={doc} hint="Adopt this blueprint to get your own copy to edit." />
      </ErrorBoundary>
    </Card>
  );
}
