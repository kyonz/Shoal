import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { AlertTriangle, CircleAlert, GitBranch, RotateCcw, ShieldOff, Timer } from "lucide-react";
import { memo } from "react";
import { cn } from "@/lib/cn";
import { between } from "@/lib/format";
import { StatusIcon, Tooltip } from "@/components/ui";
import { NODE_W } from "./graph";
import { stepType } from "./stepTypes";
import type { StepDoc } from "./types";

export interface StepRunState {
  status: string;
  attempt?: number;
  started_at?: string | null;
  finished_at?: string | null;
  wake_at?: string | null;
}

export type StepNodeData = {
  step: StepDoc;
  errors?: string[];
  warnings?: string[];
  run?: StepRunState;
  /** Highlight as a valid drop/connect target, or dim when filtered out. */
  dimmed?: boolean;
};

export type StepNodeType = Node<StepNodeData, "step">;

const RUN_RING: Record<string, string> = {
  running: "border-info ring-4 ring-info/15 animate-pulse-ring",
  waiting: "border-warn ring-4 ring-warn/15",
  succeeded: "border-ok/60",
  failed: "border-bad ring-4 ring-bad/15",
  skipped: "border-dashed opacity-60",
  cancelled: "border-dashed opacity-70",
  pending: "opacity-70",
};

function Chip({ icon, children, title }: { icon: React.ReactNode; children?: React.ReactNode; title: string }) {
  return (
    <Tooltip content={title}>
      <span className="inline-flex h-[18px] items-center gap-1 rounded bg-subtle px-1 text-[10.5px] font-medium text-muted [&>svg]:size-3">
        {icon}
        {children}
      </span>
    </Tooltip>
  );
}

export const StepNode = memo(function StepNode({ data, selected }: NodeProps<StepNodeType>) {
  const { step, errors = [], warnings = [], run, dimmed } = data;
  const info = stepType(step.type);
  const Icon = info.icon;
  const retry = (step.retry as { max_attempts?: number } | undefined)?.max_attempts;
  const runRing = run ? RUN_RING[run.status] ?? "" : "";
  return (
    <div
      style={{ width: NODE_W }}
      className={cn(
        "group relative rounded-xl border bg-panel shadow-card transition-[box-shadow,border-color,opacity] duration-150",
        "hover:shadow-pop",
        selected ? "border-accent ring-4 ring-accent/15" : errors.length && !run ? "border-bad/60" : "border-border",
        runRing,
        dimmed && "opacity-40",
      )}
    >
      <Handle type="target" position={Position.Top} className="!h-2.5 !w-2.5 !border-2 !transition-transform hover:!scale-150" />
      <div className="flex items-start gap-2.5 p-3">
        <div
          className="flex size-8 shrink-0 items-center justify-center rounded-lg text-white shadow-sm"
          style={{ background: info.color }}
        >
          <Icon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-[13px] font-semibold">{step.name || step.id}</span>
            {run ? (
              <StatusIcon status={run.status} className="ml-auto [&>svg]:size-3.5" />
            ) : errors.length ? (
              <Tooltip content={errors.join(" · ")}>
                <CircleAlert className="ml-auto size-3.5 shrink-0 text-bad" />
              </Tooltip>
            ) : warnings.length ? (
              <Tooltip content={warnings.join(" · ")}>
                <AlertTriangle className="ml-auto size-3.5 shrink-0 text-warn" />
              </Tooltip>
            ) : null}
          </div>
          <div className="mt-0.5 flex items-center gap-1 text-[11px] text-faint">
            <span className="font-medium" style={{ color: info.color }}>
              {info.label}
            </span>
            {step.name && <span className="truncate font-mono">· {step.id}</span>}
          </div>
          <div className="mt-1 line-clamp-2 text-[12px] leading-snug text-muted">{info.summary(step)}</div>
        </div>
      </div>
      {(step.if || (retry && retry > 1) || step.on_error === "continue" || step.timeout || run) && (
        <div className="flex items-center gap-1 border-t border-border px-3 py-1.5">
          {step.if && <Chip icon={<GitBranch />} title={`Runs only if ${step.if}`}>if</Chip>}
          {retry && retry > 1 && <Chip icon={<RotateCcw />} title={`Up to ${retry} attempts`}>{retry}×</Chip>}
          {step.on_error === "continue" && <Chip icon={<ShieldOff />} title="Failure does not stop the run">continue</Chip>}
          {step.timeout ? <Chip icon={<Timer />} title={`Times out after ${String(step.timeout)}`}>{String(step.timeout)}</Chip> : null}
          {run && (
            <span className="ml-auto text-[10.5px] text-faint tabular-nums">
              {run.attempt && run.attempt > 1 ? `attempt ${run.attempt} · ` : ""}
              {run.started_at ? between(run.started_at, run.finished_at) : run.status}
            </span>
          )}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!h-2.5 !w-2.5 !border-2 !transition-transform hover:!scale-150" />
    </div>
  );
});

export const nodeTypes = { step: StepNode };
