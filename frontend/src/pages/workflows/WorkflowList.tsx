import { FileUp, MoreHorizontal, Play, Plus, Search, Trash2, Workflow as WorkflowIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError, applyYaml, type Resource } from "@/api/client";
import { useDeleteResource, useResources, useRuns } from "@/api/hooks";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CodeEditor,
  Dialog,
  EmptyState,
  ErrorNotice,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  Page,
  PageHeader,
  Skeleton,
  StatusIcon,
  Tooltip,
  useConfirm,
} from "@/components/ui";
import { RunDialog } from "@/features/workflow/RunDialog";
import { stepType } from "@/features/workflow/stepTypes";
import type { ParamDoc, StepDoc } from "@/features/workflow/types";

export default function WorkflowList() {
  const ns = useNs();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { data, isLoading, error } = useResources("workflows", ns);
  const runs = useRuns(ns, {}, 15_000, can("runs:read", ns));
  const del = useDeleteResource("workflows", ns);
  const confirm = useConfirm();
  const [q, setQ] = useState("");
  const [runFor, setRunFor] = useState<Resource | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const lastRun = useMemo(() => {
    const m = new Map<string, { status: string; created_at: string; count: number }>();
    for (const r of runs.data?.items ?? []) {
      const cur = m.get(r.workflow_name);
      if (!cur) m.set(r.workflow_name, { status: r.status, created_at: r.created_at, count: 1 });
      else cur.count++;
    }
    return m;
  }, [runs.data]);

  const items = (data ?? []).filter((w) => !q || `${w.metadata.name} ${w.metadata.description ?? ""}`.toLowerCase().includes(q.toLowerCase()));

  return (
    <Page>
      <PageHeader
        title="Workflows"
        description="Graphs of agent, action and human steps. Build them visually or as YAML, then run them by hand, from triggers or in campaigns."
        actions={
          can("workflows:write", ns) && (
            <>
              <Button icon={<FileUp className="size-3.5" />} onClick={() => setImportOpen(true)}>
                Import YAML
              </Button>
              <Button variant="primary" icon={<Plus className="size-3.5" />} onClick={() => navigate(nsPath(ns, "workflows", "new"))}>
                New workflow
              </Button>
            </>
          )
        }
      />
      {error && <ErrorNotice error={error} className="mb-4" />}
      {(data?.length ?? 0) > 4 && (
        <div className="mb-4 flex h-8 max-w-sm items-center gap-2 rounded-lg border border-border bg-panel px-2.5 shadow-card focus-within:border-ring">
          <Search className="size-3.5 text-faint" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter workflows" className="flex-1 bg-transparent text-[13px] outline-none" />
        </div>
      )}
      {isLoading ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      ) : !data?.length ? (
        <Card>
          <EmptyState
            icon={<WorkflowIcon className="size-5" />}
            title="No workflows yet"
            description="Drag steps onto a canvas to describe how agents, integrations and people work together."
            action={
              can("workflows:write", ns) && (
                <Button variant="primary" icon={<Plus className="size-3.5" />} onClick={() => navigate(nsPath(ns, "workflows", "new"))}>
                  Create your first workflow
                </Button>
              )
            }
          />
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {items.map((w) => {
            const steps = ((w.spec as { steps?: StepDoc[] }).steps ?? []) as StepDoc[];
            const last = lastRun.get(w.metadata.name);
            const types = [...new Set(steps.map((s) => s.type))];
            return (
              <Card key={w.metadata.name} className="group relative flex flex-col p-4 transition-shadow hover:shadow-pop">
                <Link to={nsPath(ns, "workflows", w.metadata.name)} className="absolute inset-0 rounded-xl" aria-label={`Open ${w.metadata.name}`} />
                <div className="flex items-start gap-3">
                  <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent">
                    <WorkflowIcon className="size-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-[13px] font-semibold">{w.metadata.name}</div>
                    <div className="mt-0.5 line-clamp-2 min-h-[2.5em] text-xs text-muted">{w.metadata.description || "No description"}</div>
                  </div>
                  <Menu>
                    <MenuTrigger asChild>
                      <Button variant="ghost" size="icon-sm" className="relative z-10 opacity-0 group-hover:opacity-100 data-[state=open]:opacity-100" aria-label="Workflow actions">
                        <MoreHorizontal className="size-4" />
                      </Button>
                    </MenuTrigger>
                    <MenuContent>
                      <MenuItem onSelect={() => navigate(nsPath(ns, "workflows", w.metadata.name))}>Open builder</MenuItem>
                      <MenuItem onSelect={() => navigate(`${nsPath(ns, "runs")}?workflow=${w.metadata.name}`)}>View runs</MenuItem>
                      {can("workflows:delete", ns) && (
                        <>
                          <MenuSeparator />
                          <MenuItem
                            danger
                            icon={<Trash2 />}
                            onSelect={async () => {
                              const ok = await confirm({ title: `Delete ${w.metadata.name}?`, description: "Past runs are kept. Triggers and campaigns that use it will fail.", danger: true, confirmLabel: "Delete", typeToConfirm: w.metadata.name });
                              if (!ok) return;
                              try {
                                await del.mutateAsync(w.metadata.name);
                                toast.success(`Deleted ${w.metadata.name}`);
                              } catch (e) {
                                toast.error((e as Error).message);
                              }
                            }}
                          >
                            Delete
                          </MenuItem>
                        </>
                      )}
                    </MenuContent>
                  </Menu>
                </div>
                <div className="mt-4 flex items-center gap-1">
                  {types.slice(0, 6).map((t) => {
                    const info = stepType(t);
                    const Icon = info.icon;
                    return (
                      <Tooltip key={t} content={`${steps.filter((s) => s.type === t).length} ${info.label.toLowerCase()} step(s)`}>
                        <span className="relative z-10 flex size-6 items-center justify-center rounded-md text-white" style={{ background: info.color }}>
                          <Icon className="size-3.5" />
                        </span>
                      </Tooltip>
                    );
                  })}
                  <span className="ml-1.5 text-xs text-faint">
                    {steps.length} step{steps.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="mt-3 flex items-center gap-2 border-t border-border pt-3 text-xs text-faint">
                  <Tooltip content={`Updated ${relTime(w.metadata.updated_at)} by ${w.metadata.updated_by ?? "unknown"}`}>
                    <span className="relative z-10">
                      <Badge>v{w.metadata.version}</Badge>
                    </span>
                  </Tooltip>
                  {last ? (
                    <span className="flex items-center gap-1">
                      <StatusIcon status={last.status} className="[&>svg]:size-3.5" /> Last run {relTime(last.created_at)}
                    </span>
                  ) : (
                    <span>Never run</span>
                  )}
                  <span className="flex-1" />
                  {can("runs:create", ns) && (
                    <Button
                      variant="secondary"
                      size="xs"
                      className="relative z-10"
                      icon={<Play className="size-3" />}
                      onClick={() => setRunFor(w)}
                    >
                      Run
                    </Button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}
      {runFor && (
        <RunDialog
          open={!!runFor}
          onOpenChange={(o) => !o && setRunFor(null)}
          ns={ns}
          workflow={runFor.metadata.name}
          inputs={((runFor.spec as { inputs?: Record<string, ParamDoc> }).inputs ?? {}) as Record<string, ParamDoc>}
        />
      )}
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} ns={ns} />
    </Page>
  );
}

const SAMPLE = `apiVersion: shoal.dev/v1
kind: Workflow
metadata:
  name: hello
spec:
  inputs:
    name: {type: string, default: world}
  steps:
    - id: greet
      type: set
      values: {message: "Hello {{ inputs.name }}"}
`;

function ImportDialog({ open, onOpenChange, ns }: { open: boolean; onOpenChange: (o: boolean) => void; ns: string }) {
  const [text, setText] = useState(SAMPLE);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      // Apply creates or replaces: say which existing resources this would replace before doing it.
      const plan = await applyYaml(text, { namespace: ns, dryRun: true });
      const replaced = plan.filter((r) => r.outcome === "updated");
      if (replaced.length) {
        const ok = await confirm({
          title: `Replace ${replaced.length === 1 ? `${replaced[0].kind.toLowerCase()} ${replaced[0].name}` : `${replaced.length} resources`}?`,
          description: `Applying replaces ${replaced.map((r) => `${r.kind} ${r.name}`).join(", ")} in ${ns} with what you pasted. Earlier versions stay in the version history.`,
          confirmLabel: "Replace",
          danger: true,
        });
        if (!ok) return;
      }
      const res = await applyYaml(text, { namespace: ns });
      void qc.invalidateQueries({ queryKey: ["resources"] });
      toast.success(`Applied ${res.length} resource${res.length === 1 ? "" : "s"}`, { description: res.map((r) => `${r.kind} ${r.name}: ${r.outcome}`).join("\n") });
      onOpenChange(false);
      const wf = res.find((r) => r.kind === "Workflow");
      if (wf) navigate(nsPath(ns, "workflows", wf.name));
    } catch (e) {
      const err = e as ApiError;
      setError(err.applyResults?.length ? new Error(err.applyResults.filter((r) => r.error).map((r) => `${r.kind} ${r.name}: ${r.error}`).join("\n")) : err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="Import YAML"
      description="Paste one or more documents (workflows, agents, triggers…). They are applied to this namespace."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Apply
          </Button>
        </>
      }
    >
      <div className="rounded-lg border border-border">
        <CodeEditor value={text} onChange={setText} language="yaml" height="340px" />
      </div>
      {error ? <ErrorNotice error={error} className="mt-3 whitespace-pre-wrap" /> : null}
    </Dialog>
  );
}
