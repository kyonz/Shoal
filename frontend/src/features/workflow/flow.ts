import { MarkerType } from "@xyflow/react";
import type { DependencyEdgeType } from "./DependencyEdge";
import { dependencies } from "./graph";
import type { StepNodeData, StepNodeType } from "./StepNode";
import type { Layout, StepDoc } from "./types";

export const edgeId = (from: string, to: string) => `${from}->${to}`;

export function parseEdgeId(id: string): [string, string] {
  const [a, b] = id.split("->");
  return [a, b];
}

export function toNodes(
  steps: StepDoc[],
  layout: Layout,
  extra: (step: StepDoc) => Partial<StepNodeData> = () => ({}),
  selected?: string | null,
): StepNodeType[] {
  return steps.map((step) => ({
    id: step.id,
    type: "step",
    position: layout[step.id] ?? { x: 0, y: 0 },
    data: { step, ...extra(step) },
    selected: selected === step.id,
  }));
}

export function toEdges(
  steps: StepDoc[],
  data: (from: string, to: string) => DependencyEdgeType["data"] = () => ({}),
): DependencyEdgeType[] {
  const out: DependencyEdgeType[] = [];
  for (const [to, deps] of dependencies(steps)) {
    for (const from of deps) {
      out.push({
        id: edgeId(from, to),
        source: from,
        target: to,
        type: "dependency",
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: "var(--border-strong)" },
        data: data(from, to),
      });
    }
  }
  return out;
}
