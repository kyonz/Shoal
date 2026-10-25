import { ArrowUpCircle, Library, RefreshCw, Search, Settings2, Undo2 } from "lucide-react";
import { useDeferredValue, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Input,
  Page,
  PageHeader,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
  Tr,
  useConfirm,
} from "@/components/ui";
import { isMissingEndpoint } from "../evals/api";
import { KINDS, kindPath } from "../kinds";
import { useImports, useRollbackSkill, useSourceSkills, useVisibleSources, type CatalogSkill, type ImportedSkill } from "./api";
import { NoSources, SkillDialog, UpgradeDialog } from "./components";

export default function SkillMarketplacePage() {
  const ns = useNs();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const canManage = useCan("skill-sources:manage");
  const sources = useVisibleSources(ns);
  const [check, setCheck] = useState(false);
  const imports = useImports(ns, check);
  const tab = params.get("tab") ?? "browse";
  const selected = params.get("source") ?? sources.data?.[0]?.name;

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  return (
    <Page wide>
      <PageHeader
        title="Skill marketplace"
        icon={<Library className="size-4" />}
        description="Browse the skill sources your organisation has added and import skills into this namespace. Imports are ordinary skills, versioned and labelled with where they came from."
        actions={
          canManage && (
            <Button icon={<Settings2 className="size-4" />} onClick={() => navigate(kindPath("skill-sources", ns))}>
              Manage sources
            </Button>
          )
        }
      />
      <Tabs value={tab} onValueChange={(v) => setParam("tab", v === "browse" ? null : v)}>
        <TabsList className="mb-4">
          <TabsTrigger value="browse">Browse</TabsTrigger>
          <TabsTrigger value="imported" count={imports.data?.length}>Imported in {ns}</TabsTrigger>
        </TabsList>
        <TabsContent value="browse" className="outline-none">
          {isMissingEndpoint(sources.error) ? (
            <Card><EmptyState title="Skill sources need a newer server" description="This Shoal API cannot import skills from marketplaces yet." /></Card>
          ) : sources.error ? (
            <ErrorNotice error={sources.error} />
          ) : sources.isLoading ? (
            <Skeleton className="h-40" />
          ) : !sources.data?.length ? (
            <Card><NoSources canManage={canManage} ns={ns} /></Card>
          ) : (
            <Browse ns={ns} sources={sources.data} selected={selected!} onSelect={(s) => setParam("source", s)} />
          )}
        </TabsContent>
        <TabsContent value="imported" className="outline-none">
          <Imported ns={ns} imports={imports.data} loading={imports.isLoading || imports.isFetching} error={imports.error} check={check} onCheck={() => (check ? void imports.refetch() : setCheck(true))} />
        </TabsContent>
      </Tabs>
    </Page>
  );
}

function Browse({ ns, sources, selected, onSelect }: { ns: string; sources: { name: string; title: string; source: string; description?: string | null }[]; selected: string; onSelect: (s: string) => void }) {
  const [q, setQ] = useState("");
  const query = useDeferredValue(q.trim());
  const skills = useSourceSkills(ns, selected, query);
  const [open, setOpen] = useState<string | null>(null);
  const current = sources.find((s) => s.name === selected);

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative w-80">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={`Search ${current?.title ?? "skills"}…`} className="pl-8" />
        </div>
        <div className="flex flex-wrap gap-1.5">
          {sources.map((s) => (
            <button
              key={s.name}
              onClick={() => onSelect(s.name)}
              title={s.description ?? undefined}
              className={cn(
                "h-7 rounded-full border px-3 text-xs font-medium transition-colors",
                selected === s.name ? "border-accent/40 bg-accent-soft text-accent" : "border-border bg-panel text-muted hover:text-fg",
              )}
            >
              {s.title}
              <span className="ml-1.5 text-faint">{s.source}</span>
            </button>
          ))}
        </div>
      </div>
      {skills.error ? (
        <ErrorNotice error={skills.error} />
      ) : skills.isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => <Skeleton key={i} className="h-32" />)}
        </div>
      ) : !skills.data?.items.length ? (
        <Card><EmptyState title={query ? "No matches" : "This source has no skills"} description={query ? "Try another search." : "No SKILL.md files were found."} /></Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {skills.data.items.map((s) => <SkillCard key={s.id} skill={s} onOpen={() => setOpen(s.id)} />)}
          </div>
          {skills.data.next_cursor && <p className="mt-3 text-xs text-faint">Showing the first {skills.data.items.length}; search to narrow down.</p>}
        </>
      )}
      <SkillDialog ns={ns} source={selected} id={open} onOpenChange={(o) => !o && setOpen(null)} />
    </>
  );
}

function SkillCard({ skill, onOpen }: { skill: CatalogSkill; onOpen: () => void }) {
  const installed = skill.installed[0];
  const outdated = installed && skill.version && installed.version !== skill.version;
  return (
    <button
      onClick={onOpen}
      className="group flex flex-col rounded-xl border border-border bg-panel p-4 text-left shadow-card transition-all hover:-translate-y-0.5 hover:border-border-strong hover:shadow-pop"
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate font-semibold group-hover:text-accent">{skill.name}</div>
          <div className="truncate font-mono text-[11px] text-faint">{skill.id}</div>
        </div>
        {skill.version && <Badge className="font-mono">{skill.version}</Badge>}
      </div>
      <p className="mt-2 line-clamp-2 min-h-[2.6em] text-[13px] text-muted">{skill.description || "No description."}</p>
      <div className="mt-3 flex flex-wrap items-center gap-1">
        {skill.tags.slice(0, 4).map((t) => <Badge key={t}>{t}</Badge>)}
        {installed && (
          <span className={cn("ml-auto text-[11px]", outdated ? "text-warn" : "text-ok")}>
            {outdated ? `${installed.name} ${installed.version} · update available` : `installed as ${installed.name}`}
          </span>
        )}
      </div>
    </button>
  );
}

function Imported({ ns, imports, loading, error, check, onCheck }: {
  ns: string;
  imports: ImportedSkill[] | undefined;
  loading: boolean;
  error: unknown;
  check: boolean;
  onCheck: () => void;
}) {
  const mayImport = useCan("skills:import", ns);
  const mayWrite = useCan("skills:write", ns);
  const canChange = mayImport && mayWrite;
  const rollback = useRollbackSkill(ns);
  const confirm = useConfirm();
  const [upgrading, setUpgrading] = useState<string | null>(null);

  const undo = async (s: ImportedSkill) => {
    const ok = await confirm({
      title: `Roll back ${s.name}?`,
      description: `Restores version ${s.resource_version - 1} of the skill as a new version. Its labels go back too, so it reports the source version it had then.`,
      confirmLabel: "Roll back",
    });
    if (!ok) return;
    try {
      await rollback.mutateAsync({ name: s.name, version: s.resource_version - 1 });
      toast.success(`Rolled back ${s.name}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Rollback failed");
    }
  };

  return (
    <Card>
      {isMissingEndpoint(error) ? (
        <EmptyState title="Skill imports need a newer server" description="This Shoal API cannot import skills from marketplaces yet." />
      ) : error ? (
        <ErrorNotice className="m-4" error={error} />
      ) : !imports && loading ? (
        <Skeleton className="m-4 h-24" />
      ) : !imports?.length ? (
        <EmptyState icon={<Library className="size-5" />} title={`No imported skills in ${ns}`} description={KINDS.skills.blurb} />
      ) : (
        <>
          <div className="flex items-center justify-end border-b border-border px-4 py-2">
            <Button size="sm" icon={<RefreshCw className={cn("size-3.5", loading && "animate-spin")} />} onClick={onCheck} disabled={loading}>
              {check ? "Check again" : "Check for updates"}
            </Button>
          </div>
          <Table>
            <thead>
              <tr>
                <Th>Skill</Th>
                <Th>Source</Th>
                <Th>Version</Th>
                {check && <Th>Latest</Th>}
                <Th>Updated</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {imports.map((s) => (
                <Tr key={s.name}>
                  <Td><Link to={nsPath(ns, "skills", s.name)} className="font-mono text-xs text-accent hover:underline">{s.name}</Link></Td>
                  <Td className="text-xs"><span className="text-fg">{s.source}</span> <span className="font-mono text-faint">{s.skill_id}</span></Td>
                  <Td className="font-mono text-xs">{s.version}{s.ref && <span className="ml-1.5 text-faint">{s.ref.slice(0, 7)}</span>}</Td>
                  {check && (
                    <Td className="text-xs">
                      {s.check_error ? (
                        <span className="text-bad" title={s.check_error}>unreachable</span>
                      ) : s.update_available ? (
                        <Badge tone="warn">{s.latest_version}</Badge>
                      ) : (
                        <span className="text-ok">up to date</span>
                      )}
                    </Td>
                  )}
                  <Td className="text-xs text-muted">{relTime(s.updated_at)}{s.updated_by ? ` · ${s.updated_by}` : ""}</Td>
                  <Td className="text-right whitespace-nowrap">
                    {canChange && (
                      <>
                        <Button size="xs" variant="ghost" icon={<ArrowUpCircle className="size-3.5" />} onClick={() => setUpgrading(s.name)}>Upgrade</Button>
                        {s.resource_version > 1 && (
                          <Button size="xs" variant="ghost" icon={<Undo2 className="size-3.5" />} onClick={() => void undo(s)}>Roll back</Button>
                        )}
                      </>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </>
      )}
      <UpgradeDialog ns={ns} name={upgrading} onOpenChange={(o) => !o && setUpgrading(null)} />
    </Card>
  );
}
