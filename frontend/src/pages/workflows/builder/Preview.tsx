/**
 * A read-only view of a workflow document with the builder's own canvas and
 * inspector, for places that show a workflow before it exists in a namespace
 * (a blueprint's detail page). It drives the shared builder store, so only one
 * preview (or builder) should be mounted at a time.
 */
import { Background, BackgroundVariant, Controls, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { useCallback, useEffect, useMemo } from "react";
import { useIntegrations } from "@/api/hooks";
import { edgeTypes } from "@/features/workflow/DependencyEdge";
import { ExprVarsContext } from "@/features/workflow/ExpressionInput";
import { expressionVars } from "@/features/workflow/expressions";
import { toEdges, toNodes } from "@/features/workflow/flow";
import { decodeLayout } from "@/features/workflow/graph";
import { nodeTypes, type StepNodeType } from "@/features/workflow/StepNode";
import type { WorkflowSpecDoc } from "@/features/workflow/types";
import { Inspector } from "./Inspector";
import { useBuilder } from "./store";
import { WorkflowSettings } from "./WorkflowSettings";

export interface PreviewDoc {
  name: string;
  description?: string;
  labels?: Record<string, string>;
  spec: WorkflowSpecDoc;
  /** The encoded layout annotation, when the document carries one. */
  layout?: string;
}

export function WorkflowPreview(props: { doc: PreviewDoc; hint?: string; className?: string }) {
  return (
    <ReactFlowProvider>
      <Preview {...props} />
    </ReactFlowProvider>
  );
}

function Preview({ doc, hint, className }: { doc: PreviewDoc; hint?: string; className?: string }) {
  const flow = useReactFlow();
  const store = useBuilder.getState;
  const spec = useBuilder((s) => s.spec);
  const layout = useBuilder((s) => s.layout);
  const selected = useBuilder((s) => s.selected);
  const loaded = useBuilder((s) => s.meta.name === doc.name && s.readOnly);
  const { data: integrations = [] } = useIntegrations();

  useEffect(() => {
    store().setReadOnly(true);
    store().load({
      spec: doc.spec,
      layout: decodeLayout(doc.layout),
      meta: { name: doc.name, description: doc.description ?? "", labels: doc.labels ?? {} },
    });
    requestAnimationFrame(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
  }, [doc, store, flow]);

  const nodes = useMemo(() => toNodes(spec.steps, layout, () => ({ errors: [], warnings: [] }), selected), [spec.steps, layout, selected]);
  const edges = useMemo(() => toEdges(spec.steps, () => ({})), [spec.steps]);
  const exprVars = useCallback(() => expressionVars(store().spec, store().selected, { integrations }), [store, integrations]);

  return (
    <ExprVarsContext.Provider value={exprVars}>
      <div className={className ?? "flex h-[560px] min-h-0"}>
        <div className="relative min-w-0 flex-1">
          {loaded && (
            <ReactFlow<StepNodeType>
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodeClick={(_, n) => store().select(n.id)}
              onPaneClick={() => store().select(null)}
              nodesDraggable={false}
              nodesConnectable={false}
              deleteKeyCode={null}
              minZoom={0.2}
              maxZoom={1.6}
              fitView
              fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
              proOptions={{ hideAttribution: true }}
              defaultEdgeOptions={{ type: "dependency" }}
            >
              <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
              <Controls showInteractive={false} position="bottom-left" />
            </ReactFlow>
          )}
        </div>
        <aside className="w-[360px] shrink-0 border-l border-border bg-panel">
          {loaded && (selected ? <Inspector key={selected} stepId={selected} issues={[]} readOnly /> : <WorkflowSettings isNew={false} issues={[]} readOnly hint={hint} />)}
        </aside>
      </div>
    </ExprVarsContext.Provider>
  );
}
