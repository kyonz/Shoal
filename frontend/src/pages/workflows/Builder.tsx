/**
 * The visual workflow builder: palette | canvas (+ YAML) | inspector.
 */
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type NodeChange,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Code2,
  Columns2,
  History,
  Keyboard,
  LayoutGrid,
  Loader2,
  MousePointerClick,
  Play,
  Redo2,
  Save,
  Undo2,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { ApiError, applyYaml } from "@/api/client";
import { useIntegrations, usePutResource, useResource, useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, useNs } from "@/lib/ns";
import { Badge, Button, Dialog, EmptyState, Kbd, Popover, Segmented, Spinner, Tooltip, useConfirm } from "@/components/ui";
import { edgeTypes } from "@/features/workflow/DependencyEdge";
import { parseWorkflowYaml, toDocument, toYaml } from "@/features/workflow/document";
import { ExprVarsContext } from "@/features/workflow/ExpressionInput";
import { expressionVars } from "@/features/workflow/expressions";
import { parseEdgeId, toEdges, toNodes } from "@/features/workflow/flow";
import { decodeLayout, dependencies, encodeLayout, wouldCycle } from "@/features/workflow/graph";
import { RunDialog } from "@/features/workflow/RunDialog";
import { nodeTypes, type StepNodeType } from "@/features/workflow/StepNode";
import { LAYOUT_ANNOTATION, type ParamDoc, type WorkflowSpecDoc } from "@/features/workflow/types";
import { serverIssue, validateWorkflow, type Issue } from "@/features/workflow/validate";
import { Inspector } from "./builder/Inspector";
import { DND_MIME, Palette, type PaletteItem } from "./builder/Palette";
import { useBuilder, useDirty } from "./builder/store";
import { VersionsDialog } from "./builder/Versions";
import { WorkflowSettings } from "./builder/WorkflowSettings";
import { YamlPane } from "./builder/YamlPane";

type View = "canvas" | "split" | "yaml";

export default function BuilderPage() {
  return (
    <ReactFlowProvider>
      <Builder />
    </ReactFlowProvider>
  );
}

function isTyping(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

function Builder() {
  const ns = useNs();
  const { name: routeName } = useParams();
  const isNew = !routeName || routeName === "new";
  const navigate = useNavigate();
  const { can } = useAuth();
  const canWrite = can("workflows:write", ns);
  const canRun = can("runs:create", ns);
  const resource = useResource("workflows", ns, isNew ? undefined : routeName);
  const put = usePutResource("workflows", ns);
  const { data: integrations = [] } = useIntegrations();
  const agents = useResources("agents", ns);
  const workflows = useResources("workflows", ns);
  const flow = useReactFlow();
  const confirm = useConfirm();

  const spec = useBuilder((s) => s.spec);
  const layout = useBuilder((s) => s.layout);
  const meta = useBuilder((s) => s.meta);
  const selected = useBuilder((s) => s.selected);
  const canUndo = useBuilder((s) => s.past.length > 0);
  const canRedo = useBuilder((s) => s.future.length > 0);
  const dirty = useDirty();
  const store = useBuilder.getState;

  const [view, setView] = useState<View>(() => (localStorage.getItem("shoal.builderView") as View) || "canvas");
  const [runOpen, setRunOpen] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [serverIssues, setServerIssues] = useState<Issue[]>([]);
  const [checking, setChecking] = useState(false);
  const loadedFor = useRef<string | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem("shoal.builderView", view);
    } catch {
      /* ignore */
    }
  }, [view]);

  // ------------------------------------------------------------------ load
  useEffect(() => {
    const key = `${ns}/${routeName ?? "new"}`;
    if (loadedFor.current === key) return;
    if (isNew) {
      store().load({ spec: { steps: [] }, layout: {}, meta: { name: "", description: "", labels: {} } });
      loadedFor.current = key;
      return;
    }
    if (!resource.data) return;
    const r = resource.data;
    const annotations = (r.metadata as { annotations?: Record<string, string> }).annotations ?? {};
    store().load({
      spec: r.spec as WorkflowSpecDoc,
      layout: decodeLayout(annotations[LAYOUT_ANNOTATION]),
      meta: { name: r.metadata.name, description: r.metadata.description ?? "", labels: r.metadata.labels ?? {} },
    });
    loadedFor.current = key;
    requestAnimationFrame(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
  }, [ns, routeName, isNew, resource.data, store, flow]);

  // ------------------------------------------------------------------ validation
  const clientIssues = useMemo(
    () =>
      validateWorkflow(spec, {
        integrations,
        agents: agents.data?.map((a) => a.metadata.name),
        workflows: workflows.data?.map((w) => w.metadata.name),
      }),
    [spec, integrations, agents.data, workflows.data],
  );

  const yamlText = useMemo(() => toYaml(toDocument(meta.name, meta.description, meta.labels, spec)), [meta, spec]);

  // Server-side validation via a dry-run apply, debounced.
  useEffect(() => {
    if (!canWrite || !spec.steps.length) {
      setServerIssues([]);
      return;
    }
    const doc = toYaml(toDocument(meta.name || "draft-preview", meta.description, meta.labels, spec));
    const t = setTimeout(async () => {
      setChecking(true);
      try {
        await applyYaml(doc, { namespace: ns, dryRun: true });
        setServerIssues([]);
      } catch (e) {
        if (e instanceof ApiError) {
          const msgs = e.applyResults.filter((r) => r.error).map((r) => r.error!);
          setServerIssues((msgs.length ? msgs.flatMap((m) => m.split("; ")) : [e.detail]).map((m) => serverIssue(spec, m)));
        }
      } finally {
        setChecking(false);
      }
    }, 900);
    return () => clearTimeout(t);
  }, [spec, meta, ns, canWrite]);

  const issues = useMemo(() => [...clientIssues, ...serverIssues], [clientIssues, serverIssues]);
  const errors = issues.filter((i) => i.level === "error");
  const warnings = issues.filter((i) => i.level === "warning");

  // ------------------------------------------------------------------ canvas projection
  const derivedNodes = useMemo(
    () =>
      toNodes(
        spec.steps,
        layout,
        (s) => ({
          errors: issues.filter((i) => i.stepId === s.id && i.level === "error").map((i) => i.message),
          warnings: issues.filter((i) => i.stepId === s.id && i.level === "warning").map((i) => i.message),
        }),
        selected,
      ),
    [spec.steps, layout, issues, selected],
  );
  const [nodes, setNodes] = useState<StepNodeType[]>(derivedNodes);
  useEffect(() => setNodes(derivedNodes), [derivedNodes]);

  const onEdgeDelete = useCallback((id: string) => {
    const [from, to] = parseEdgeId(id);
    store().unlink(from, to);
  }, [store]);
  const edges = useMemo(() => toEdges(spec.steps, () => ({ onDelete: canWrite ? onEdgeDelete : undefined })), [spec.steps, onEdgeDelete, canWrite]);

  const onNodesChange = useCallback((changes: NodeChange<StepNodeType>[]) => {
    setNodes((ns) => applyNodeChanges(changes, ns));
  }, []);

  const onNodeDragStop = useCallback((_: unknown, __: unknown, dragged: StepNodeType[]) => {
    store().moveNodes(Object.fromEntries(dragged.map((n) => [n.id, { x: Math.round(n.position.x), y: Math.round(n.position.y) }])));
  }, [store]);

  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target) store().link(c.source, c.target);
  }, [store]);

  const isValidConnection = useCallback(
    (c: Connection | { source: string; target: string }) => {
      const deps = dependencies(store().spec.steps);
      return !!c.source && !!c.target && !wouldCycle(deps, c.source, c.target) && !(deps.get(c.target) ?? []).includes(c.source);
    },
    [store],
  );

  const onSelectionChange = useCallback(({ nodes: sel }: OnSelectionChangeParams) => {
    const id = sel.length === 1 ? sel[0].id : null;
    if (id !== store().selected) store().select(id);
  }, [store]);

  const onDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      const raw = e.dataTransfer.getData(DND_MIME);
      if (!raw || !canWrite) return;
      const item = JSON.parse(raw) as PaletteItem;
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      store().add(item.type, item.preset, { x: Math.round(pos.x - 132), y: Math.round(pos.y - 30) });
    },
    [flow, store, canWrite],
  );

  const addFromPalette = useCallback(
    (item: PaletteItem) => {
      if (!canWrite) return;
      const st = store();
      const id = st.add(item.type, item.preset, undefined, st.selected);
      requestAnimationFrame(() => {
        const p = store().layout[id];
        if (p) flow.setCenter(p.x + 132, p.y + 48, { zoom: flow.getZoom(), duration: 300 });
      });
    },
    [flow, store, canWrite],
  );

  // ------------------------------------------------------------------ save
  const save = useCallback(async () => {
    const st = store();
    if (!canWrite) return;
    if (!st.meta.name || !/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/.test(st.meta.name)) {
      toast.error("Give the workflow a valid name first", { description: "Open workflow settings (click the canvas background)." });
      st.select(null);
      return;
    }
    if (!st.spec.steps.length) {
      toast.error("Add at least one step before saving");
      return;
    }
    try {
      const res = await put.mutateAsync({
        name: st.meta.name,
        body: {
          metadata: {
            description: st.meta.description || null,
            labels: st.meta.labels,
            annotations: { [LAYOUT_ANNOTATION]: encodeLayout(st.layout) },
          },
          spec: st.spec as unknown as Record<string, unknown>,
        },
      });
      store().markSaved();
      toast.success(`Saved ${res.metadata.name}`, { description: `Version ${res.metadata.version}` });
      if (isNew) {
        loadedFor.current = `${ns}/${res.metadata.name}`;
        navigate(nsPath(ns, "workflows", res.metadata.name), { replace: true });
      }
    } catch (e) {
      const err = e as ApiError;
      toast.error("Could not save", { description: err.fieldErrors?.length ? err.fieldErrors.map((f) => `${f.loc.slice(1).join(".")}: ${f.msg}`).join("\n") : err.message });
    }
  }, [store, put, isNew, navigate, ns, canWrite]);

  // ------------------------------------------------------------------ keyboard
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const k = e.key.toLowerCase();
      if (mod && k === "s") {
        e.preventDefault();
        void save();
        return;
      }
      if (isTyping(e)) return;
      if (mod && k === "z") {
        e.preventDefault();
        if (e.shiftKey) store().redo();
        else store().undo();
      } else if (mod && k === "y") {
        e.preventDefault();
        store().redo();
      } else if (mod && k === "d") {
        e.preventDefault();
        const id = store().selected;
        if (id) store().duplicate(id);
      } else if (!mod && k === "l") {
        store().relayout();
        requestAnimationFrame(() => flow.fitView({ padding: 0.2, duration: 300, maxZoom: 1 }));
      } else if (!mod && k === "f") {
        flow.fitView({ padding: 0.2, duration: 300, maxZoom: 1 });
      } else if (e.key === "?") {
        setHelpOpen(true);
      } else if (e.key === "Escape") {
        store().select(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save, store, flow]);

  // ------------------------------------------------------------------ unsaved changes
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    void confirm({ title: "Discard unsaved changes?", description: "Your edits to this workflow have not been saved.", confirmLabel: "Discard", danger: true }).then((ok) =>
      ok ? blocker.proceed() : blocker.reset(),
    );
  }, [blocker, confirm]);
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [dirty]);

  // ------------------------------------------------------------------ expression context
  const exprVars = useCallback(
    () => expressionVars(store().spec, store().selected, { integrations }),
    [store, integrations],
  );

  if (!isNew && resource.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-5" />
      </div>
    );
  }
  if (!isNew && resource.error) {
    return <EmptyState className="h-full" icon={<WorkflowIcon className="size-5" />} title="Workflow not found" description={(resource.error as Error).message} action={<Link to={nsPath(ns, "workflows")}><Button>Back to workflows</Button></Link>} />;
  }

  const version = resource.data?.metadata.version;
  const showCanvas = view !== "yaml";
  const showYaml = view !== "canvas";

  return (
    <ExprVarsContext.Provider value={exprVars}>
      <div className="flex h-full flex-col">
        {/* ---------------------------------------------------------------- toolbar */}
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-panel px-3">
          <Link to={nsPath(ns, "workflows")} className="text-[13px] text-muted hover:text-fg">
            Workflows
          </Link>
          <ChevronRight className="size-3.5 text-faint" />
          <span className="max-w-64 truncate font-mono text-[13px] font-semibold">{meta.name || "untitled"}</span>
          {version != null && !isNew && <Badge>v{version}</Badge>}
          {isNew && <Badge tone="accent">new</Badge>}
          {dirty && (
            <Tooltip content="Unsaved changes">
              <span className="size-2 rounded-full bg-warn" />
            </Tooltip>
          )}
          <ProblemsButton errors={errors} warnings={warnings} checking={checking} onPick={(id) => {
            store().select(id);
            const p = store().layout[id];
            if (p) flow.setCenter(p.x + 132, p.y + 48, { zoom: 1, duration: 300 });
          }} />
          <div className="flex-1" />
          <Segmented<View>
            value={view}
            onChange={setView}
            options={[
              { value: "canvas", label: "Canvas", icon: <WorkflowIcon className="size-3.5" /> },
              { value: "split", label: "Split", icon: <Columns2 className="size-3.5" /> },
              { value: "yaml", label: "YAML", icon: <Code2 className="size-3.5" /> },
            ]}
          />
          <div className="mx-1 h-5 w-px bg-border" />
          <Tooltip content="Undo (⌘Z)">
            <Button variant="ghost" size="icon" disabled={!canUndo} onClick={() => store().undo()} aria-label="Undo">
              <Undo2 className="size-4" />
            </Button>
          </Tooltip>
          <Tooltip content="Redo (⌘⇧Z)">
            <Button variant="ghost" size="icon" disabled={!canRedo} onClick={() => store().redo()} aria-label="Redo">
              <Redo2 className="size-4" />
            </Button>
          </Tooltip>
          <Tooltip content="Auto-layout (L)">
            <Button
              variant="ghost"
              size="icon"
              disabled={!canWrite || !spec.steps.length}
              onClick={() => {
                store().relayout();
                requestAnimationFrame(() => flow.fitView({ padding: 0.2, duration: 300, maxZoom: 1 }));
              }}
              aria-label="Auto-layout"
            >
              <LayoutGrid className="size-4" />
            </Button>
          </Tooltip>
          {!isNew && (
            <Tooltip content="Version history">
              <Button variant="ghost" size="icon" onClick={() => setVersionsOpen(true)} aria-label="Version history">
                <History className="size-4" />
              </Button>
            </Tooltip>
          )}
          <Tooltip content="Keyboard shortcuts (?)">
            <Button variant="ghost" size="icon" onClick={() => setHelpOpen(true)} aria-label="Keyboard shortcuts">
              <Keyboard className="size-4" />
            </Button>
          </Tooltip>
          <div className="mx-1 h-5 w-px bg-border" />
          {canWrite && (
            <Button variant="secondary" onClick={() => void save()} loading={put.isPending} disabled={!dirty && !isNew} icon={<Save className="size-3.5" />}>
              Save
            </Button>
          )}
          {canRun && !isNew && (
            <Tooltip content={dirty ? "Save your changes before running" : undefined}>
              <span>
                <Button variant="primary" disabled={dirty} onClick={() => setRunOpen(true)} icon={<Play className="size-3.5" />}>
                  Run
                </Button>
              </span>
            </Tooltip>
          )}
        </div>

        {/* ---------------------------------------------------------------- body */}
        <div className="flex min-h-0 flex-1">
          {view === "canvas" && canWrite && (
            <aside className="w-60 shrink-0 border-r border-border bg-panel">
              <Palette onAdd={addFromPalette} />
            </aside>
          )}
          {showCanvas && (
            <div className="relative min-w-0 flex-1" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
              <ReactFlow<StepNodeType>
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                onNodesChange={onNodesChange}
                onNodeDragStop={onNodeDragStop}
                onConnect={onConnect}
                isValidConnection={isValidConnection}
                onSelectionChange={onSelectionChange}
                onNodesDelete={(del) => canWrite && store().remove(del.map((n) => n.id))}
                onEdgesDelete={(del) => del.forEach((e) => canWrite && store().unlink(e.source, e.target))}
                onPaneClick={() => store().select(null)}
                nodesDraggable={canWrite}
                nodesConnectable={canWrite}
                deleteKeyCode={canWrite ? ["Delete", "Backspace"] : null}
                multiSelectionKeyCode={["Meta", "Control", "Shift"]}
                snapToGrid
                snapGrid={[8, 8]}
                minZoom={0.2}
                maxZoom={1.6}
                fitView
                fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
                proOptions={{ hideAttribution: true }}
                defaultEdgeOptions={{ type: "dependency" }}
                connectionLineStyle={{ stroke: "var(--accent)", strokeWidth: 2 }}
              >
                <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
                <Controls showInteractive={false} position="bottom-left" />
                <MiniMap pannable zoomable position="bottom-right" style={{ width: 168, height: 112 }} nodeStrokeWidth={2} nodeColor={() => "var(--border-strong)"} maskColor="color-mix(in oklab, var(--bg) 65%, transparent)" />
              </ReactFlow>
              {spec.steps.length === 0 && (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                  <div className="max-w-sm rounded-2xl border border-dashed border-border-strong bg-panel/90 px-8 py-7 text-center shadow-card backdrop-blur">
                    <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-xl bg-accent-soft text-accent">
                      <MousePointerClick className="size-5" />
                    </div>
                    <div className="text-sm font-semibold">Start building</div>
                    <p className="mt-1 text-[13px] text-muted">
                      Drag a step from the left onto the canvas, or click one to add it. Connect steps by dragging from the bottom handle of one to the top of another.
                    </p>
                  </div>
                </div>
              )}
            </div>
          )}
          {showYaml && (
            <div className={cn("min-w-0 border-l border-border bg-panel", view === "yaml" ? "flex-1" : "w-[44%] max-w-[720px]")}>
              <YamlPane
                text={yamlText}
                readOnly={!canWrite}
                onApply={(text) => {
                  const parsed = parseWorkflowYaml(text);
                  if (!parsed.ok) return parsed;
                  const st = store();
                  st.setSpec(parsed.doc.spec, "yaml");
                  const nextMeta = {
                    description: parsed.doc.metadata.description ?? "",
                    labels: parsed.doc.metadata.labels ?? {},
                    ...(isNew && parsed.doc.metadata.name ? { name: parsed.doc.metadata.name } : {}),
                  };
                  if (JSON.stringify(nextMeta) !== JSON.stringify({ description: st.meta.description, labels: st.meta.labels, ...(isNew && parsed.doc.metadata.name ? { name: st.meta.name } : {}) })) {
                    st.setMeta(nextMeta, "yaml");
                  }
                  return parsed;
                }}
              />
            </div>
          )}
          {view !== "yaml" && (
            <aside className="w-[380px] shrink-0 border-l border-border bg-panel">
              {selected ? <Inspector key={selected} stepId={selected} issues={issues} /> : <WorkflowSettings isNew={isNew} issues={issues} />}
            </aside>
          )}
        </div>
      </div>

      {!isNew && meta.name && (
        <RunDialog open={runOpen} onOpenChange={setRunOpen} ns={ns} workflow={meta.name} inputs={(spec.inputs as Record<string, ParamDoc>) ?? {}} />
      )}
      {!isNew && routeName && <VersionsDialog open={versionsOpen} onOpenChange={setVersionsOpen} ns={ns} name={routeName} currentYaml={yamlText} />}
      <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} />
    </ExprVarsContext.Provider>
  );
}

function ProblemsButton({ errors, warnings, checking, onPick }: { errors: Issue[]; warnings: Issue[]; checking: boolean; onPick: (stepId: string) => void }) {
  const all = [...errors, ...warnings];
  const tone = errors.length ? "text-bad bg-bad-soft border-bad/20" : warnings.length ? "text-warn bg-warn-soft border-warn/20" : "text-ok bg-ok-soft border-ok/20";
  return (
    <Popover
      align="start"
      className="w-96 p-0"
      trigger={
        <button className={cn("ml-1 flex h-6 items-center gap-1.5 rounded-md border px-2 text-[11.5px] font-medium", tone)}>
          {checking ? <Loader2 className="size-3 animate-spin" /> : errors.length ? <CircleAlert className="size-3" /> : warnings.length ? <AlertTriangle className="size-3" /> : <CheckCircle2 className="size-3" />}
          {errors.length ? `${errors.length} error${errors.length > 1 ? "s" : ""}` : warnings.length ? `${warnings.length} warning${warnings.length > 1 ? "s" : ""}` : "Valid"}
        </button>
      }
    >
      <div className="border-b border-border px-3 py-2 text-xs font-semibold">Problems</div>
      <div className="max-h-80 overflow-y-auto p-1">
        {!all.length && <div className="px-3 py-6 text-center text-xs text-muted">No problems. The server accepts this workflow.</div>}
        {all.map((i, n) => (
          <button
            key={n}
            disabled={!i.stepId}
            onClick={() => i.stepId && onPick(i.stepId)}
            className="flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-subtle disabled:hover:bg-transparent"
          >
            {i.level === "error" ? <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-bad" /> : <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" />}
            <span className="min-w-0 flex-1">
              {i.stepId && <span className="mr-1.5 font-mono font-semibold">{i.stepId}</span>}
              {i.message}
            </span>
            {i.source === "server" && <Badge>server</Badge>}
          </button>
        ))}
      </div>
    </Popover>
  );
}

const SHORTCUTS: [string[], string][] = [
  [["⌘", "S"], "Save"],
  [["⌘", "Z"], "Undo"],
  [["⌘", "⇧", "Z"], "Redo"],
  [["⌘", "D"], "Duplicate selected step"],
  [["Del"], "Delete selected steps or connection"],
  [["L"], "Auto-layout"],
  [["F"], "Fit to screen"],
  [["Esc"], "Deselect (show workflow settings)"],
  [["⇧", "drag"], "Select several steps"],
  [["⌘", "K"], "Command palette"],
  [["{{"], "Autocomplete an expression in any field"],
];

function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts" size="sm">
      <div className="space-y-2">
        {SHORTCUTS.map(([keys, label]) => (
          <div key={label} className="flex items-center justify-between text-[13px]">
            <span className="text-muted">{label}</span>
            <span className="flex gap-1">
              {keys.map((k) => (
                <Kbd key={k}>{k}</Kbd>
              ))}
            </span>
          </div>
        ))}
      </div>
    </Dialog>
  );
}
