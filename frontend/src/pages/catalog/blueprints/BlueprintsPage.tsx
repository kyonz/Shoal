import { Package, Plus, Search, Star } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useResources } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Page, PageHeader, Skeleton, Switch, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui";
import { isMissingEndpoint } from "../evals/api";
import { KINDS } from "../kinds";
import { isFeatured, spec, useAdoptions } from "./api";
import { AdoptionsTable, BlueprintIcon, BlueprintStatus } from "./components";

export default function BlueprintsPage() {
  const ns = useNs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const canPublish = useCan("blueprints:publish");
  const { data, isLoading, error } = useResources("blueprints", ns);
  const adoptions = useAdoptions(ns);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [showDeprecated, setShowDeprecated] = useState(false);
  const tab = params.get("tab") ?? "catalogue";

  const all = data ?? [];
  const visible = all.filter((r) => {
    const s = spec(r);
    if (s.status === "draft" && !canPublish) return false;
    if (s.namespaces?.length && !s.namespaces.includes(ns) && !canPublish) return false;
    return true;
  });
  const categories = useMemo(() => [...new Set(visible.flatMap((r) => [...(spec(r).categories ?? [])]))].sort(), [visible]);
  const filtered = visible
    .filter((r) => {
      const s = spec(r);
      if (!showDeprecated && s.status === "deprecated") return false;
      if (category && !(s.categories ?? []).includes(category)) return false;
      const needle = q.trim().toLowerCase();
      if (!needle) return true;
      return [r.metadata.name, s.title, s.summary, s.author, ...(s.tags ?? []), ...(s.categories ?? [])].filter(Boolean).join(" ").toLowerCase().includes(needle);
    })
    .sort((a, b) => Number(isFeatured(spec(b))) - Number(isFeatured(spec(a))) || spec(a).title.localeCompare(spec(b).title));
  const latest = Object.fromEntries(all.map((r) => [r.metadata.name, spec(r).version]));
  const adoptedCount = (bp: string) => (adoptions.data ?? []).filter((a) => a.blueprint === bp).length;

  return (
    <Page wide>
      <PageHeader
        title="Blueprints"
        icon={<Package className="size-4" />}
        description={KINDS.blueprints.blurb}
        actions={
          canPublish && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, "blueprints", "new"))}>
              Publish a blueprint
            </Button>
          )
        }
      />
      <Tabs value={tab} onValueChange={(v) => setParams(v === "catalogue" ? {} : { tab: v }, { replace: true })}>
        <TabsList className="mb-4">
          <TabsTrigger value="catalogue" count={visible.length}>Catalogue</TabsTrigger>
          <TabsTrigger value="adopted" count={adoptions.data?.length}>Adopted in {ns}</TabsTrigger>
        </TabsList>
        <TabsContent value="catalogue" className="outline-none">
          {visible.length > 0 && (
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <div className="relative w-80">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
                <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search blueprints, tags, authors…" className="pl-8" />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {[null, ...categories].map((c) => (
                  <button
                    key={c ?? "all"}
                    onClick={() => setCategory(c)}
                    className={cn(
                      "h-7 rounded-full border px-3 text-xs font-medium transition-colors",
                      category === c ? "border-accent/40 bg-accent-soft text-accent" : "border-border bg-panel text-muted hover:text-fg",
                    )}
                  >
                    {c ?? "All"}
                  </button>
                ))}
              </div>
              <div className="ml-auto">
                <Switch checked={showDeprecated} onCheckedChange={setShowDeprecated} label={<span className="text-xs text-muted">Show deprecated</span>} />
              </div>
            </div>
          )}
          {error ? (
            <ErrorNotice error={error} />
          ) : isLoading ? (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {[0, 1, 2].map((i) => <Skeleton key={i} className="h-44" />)}
            </div>
          ) : !visible.length ? (
            <Card>
              <EmptyState
                icon={<Package className="size-5" />}
                title="No blueprints published yet"
                description="Teams publish workflows here as blueprints so other namespaces can adopt them in a few clicks."
                action={canPublish && <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => navigate(nsPath(ns, "blueprints", "new"))}>Publish a blueprint</Button>}
              />
            </Card>
          ) : !filtered.length ? (
            <Card><EmptyState title="No matches" description="Try another search or category." /></Card>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {filtered.map((r) => {
                const s = spec(r);
                const n = adoptedCount(r.metadata.name);
                return (
                  <Link
                    key={r.metadata.name}
                    to={nsPath(ns, "blueprints", r.metadata.name)}
                    className={cn(
                      "group flex flex-col rounded-xl border bg-panel p-4 shadow-card transition-all hover:-translate-y-0.5 hover:border-border-strong hover:shadow-pop",
                      isFeatured(s) ? "border-accent/30" : "border-border",
                      s.status === "deprecated" && "opacity-70",
                    )}
                  >
                    <div className="flex items-start gap-3">
                      <BlueprintIcon spec={s} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate font-semibold group-hover:text-accent">{s.title}</span>
                          {isFeatured(s) && <Star className="size-3.5 shrink-0 fill-warn text-warn" />}
                        </div>
                        <div className="truncate text-xs text-faint">
                          v{s.version}
                          {s.author ? ` · ${s.author}` : ""}
                        </div>
                      </div>
                      {s.status !== "published" && s.status !== "featured" && s.status !== "promoted" && <BlueprintStatus status={s.status} />}
                    </div>
                    <p className="mt-3 line-clamp-2 min-h-[2.6em] text-[13px] text-muted">{s.summary}</p>
                    <div className="mt-3 flex flex-wrap items-center gap-1">
                      {(s.tags ?? []).slice(0, 4).map((t) => <Badge key={t}>{t}</Badge>)}
                      <span className="ml-auto text-[11px] text-faint">
                        {s.resources?.length ?? 0} resource{s.resources?.length === 1 ? "" : "s"}
                        {n > 0 && <span className="ml-1.5 text-ok">· adopted</span>}
                      </span>
                    </div>
                  </Link>
                );
              })}
            </div>
          )}
        </TabsContent>
        <TabsContent value="adopted" className="outline-none">
          <Card>
            {isMissingEndpoint(adoptions.error) ? (
              <EmptyState title="Adoptions need a newer server" description="This Shoal API cannot adopt blueprints yet." />
            ) : adoptions.error ? (
              <ErrorNotice className="m-4" error={adoptions.error} />
            ) : adoptions.isLoading ? (
              <Skeleton className="m-4 h-24" />
            ) : !adoptions.data?.length ? (
              <EmptyState icon={<Package className="size-5" />} title={`Nothing adopted in ${ns}`} description="Adopt a blueprint from the catalogue to get a ready-made workflow wired to your connections." />
            ) : (
              <AdoptionsTable ns={ns} adoptions={adoptions.data} latest={latest} />
            )}
          </Card>
        </TabsContent>
      </Tabs>
    </Page>
  );
}
