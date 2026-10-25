import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type Edge, type EdgeProps } from "@xyflow/react";
import { X } from "lucide-react";
import { memo, useState } from "react";
import { cn } from "@/lib/cn";

export type DependencyEdgeData = {
  onDelete?: (id: string) => void;
  /** Run viewer: the upstream step finished, the downstream one is active. */
  state?: "active" | "done" | "idle";
};

export type DependencyEdgeType = Edge<DependencyEdgeData, "dependency">;

export const DependencyEdge = memo(function DependencyEdge(props: EdgeProps<DependencyEdgeType>) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, data, selected } = props;
  const [hover, setHover] = useState(false);
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 14 });
  const stroke =
    data?.state === "active" ? "var(--info)" : data?.state === "done" ? "var(--ok)" : selected ? "var(--accent)" : "var(--border-strong)";
  return (
    <>
      <path d={path} fill="none" stroke="transparent" strokeWidth={18} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)} />
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={{ stroke, strokeWidth: selected || hover ? 2.25 : 1.6, strokeDasharray: data?.state === "active" ? "6 4" : undefined }}
        className={cn(data?.state === "active" && "[animation:dash_0.6s_linear_infinite]")}
      />
      {data?.onDelete && (hover || selected) && (
        <EdgeLabelRenderer>
          <button
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
            onClick={(e) => {
              e.stopPropagation();
              data.onDelete?.(id);
            }}
            title="Remove dependency"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`, pointerEvents: "all" }}
            className="nodrag nopan absolute flex size-5 items-center justify-center rounded-full border border-border bg-panel text-muted shadow-pop hover:border-bad hover:text-bad"
          >
            <X className="size-3" />
          </button>
        </EdgeLabelRenderer>
      )}
    </>
  );
});

export const edgeTypes = { dependency: DependencyEdge };
