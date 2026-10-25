import { ChevronRight, Download, Megaphone, Pencil, Star, Workflow as WorkflowIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "@/api/client";
import { usePutResource, useResource } from "@/api/hooks";
import { toast } from "sonner";
import { useCan } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, CardHeader, EmptyState, Page, Skeleton } from "@/components/ui";
import { Markdown } from "../widgets";
import { isFeatured, spec, useAdoptions } from "./api";
import { AdoptWizard, AdoptionsTable, BlueprintIcon, BlueprintStatus } from "./components";

export default function BlueprintDetailPage() {
  const ns = useNs();
  const { name } = useParams();
  const navigate = useNavigate();
  const res = useResource("blueprints", ns, name);
  const adoptions = useAdoptions(ns, name);
  const canAdopt = useCan("blueprints:adopt", ns);
  const canPublish = useCan("blueprints:publish");
  const canFeature = useCan("blueprints:feature");
  const put = usePutResource("blueprints", ns);
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
  const blocked = s.status === "draft" && !canPublish ? "Drafts cannot be adopted until published." : restricted ? `Only ${s.namespaces!.join(", ")} can adopt this.` : s.status === "deprecated" ? null : null;
  const mine = adoptions.data ?? [];
  const labels = res.data.metadata.labels ?? {};
  const from = labels["shoal.dev/from-workflow"] ? { ns: labels["shoal.dev/from-namespace"] ?? ns, name: labels["shoal.dev/from-workflow"] } : null;
  const setStatus = async (status: "published" | "featured") => {
    try {
      await put.mutateAsync({ name: name!, body: { metadata: { description: res.data!.metadata.description, labels }, spec: { ...res.data!.spec, status } } });
      toast.success(status === "published" ? `Published ${s.title}: every allowed namespace can adopt it` : `Featured ${s.title} in the catalogue`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not change the status");
    }
  };

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
              {from && (
                <Link to={nsPath(from.ns, "workflows", from.name)} className="inline-flex items-center gap-1 hover:text-fg">
                  · <WorkflowIcon className="size-3" /> from {from.ns}/{from.name}
                </Link>
              )}
              {(s.tags ?? []).map((t) => <Badge key={t}>{t}</Badge>)}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {canPublish && s.status === "draft" && (
              <Button icon={<Megaphone className="size-4" />} loading={put.isPending} onClick={() => void setStatus("published")} title="Let every allowed namespace adopt it">
                Publish
              </Button>
            )}
            {canFeature && s.status === "published" && (
              <Button icon={<Star className="size-4" />} loading={put.isPending} onClick={() => void setStatus("featured")} title="Promote it at the top of the catalogue">
                Feature
              </Button>
            )}
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
        {(blocked || s.status === "deprecated") && (
          <div className="border-t border-border bg-warn-soft px-5 py-2 text-[13px] text-warn">
            {blocked ?? "This blueprint is deprecated. Existing adoptions keep working; consider an alternative for new work."}
          </div>
        )}
      </Card>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          {mine.length > 0 && (
            <Card>
              <CardHeader title={`Adopted in ${ns}`} />
              <AdoptionsTable ns={ns} adoptions={mine} latest={{ [name!]: s.version }} />
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
