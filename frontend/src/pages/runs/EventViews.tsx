import {
  Bot,
  ChevronRight,
  CircleCheck,
  CircleX,
  Clock,
  Cpu,
  Hand,
  KeyRound,
  Play,
  Radio,
  RotateCcw,
  ShieldAlert,
  SkipForward,
  SquareTerminal,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import type { RunEvent } from "@/api/client";
import { connectPath } from "@/pages/connect/PersonalCredentials";
import { cn } from "@/lib/cn";
import { CodeEditor, JsonBlock } from "@/components/ui";
import { actorName } from "@/lib/actor";

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));
const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

interface Look {
  icon: LucideIcon;
  tone: string;
  title: (e: RunEvent) => ReactNode;
  body?: (e: RunEvent) => ReactNode;
}

const LOOKS: Record<string, Look> = {
  "run.created": { icon: Play, tone: "text-muted", title: (e) => `Run created by ${str((e.data.initiator as { display_name?: string })?.display_name ?? actorName(e.data.started_by as string))}` },
  "run.started": { icon: Play, tone: "text-info", title: () => "Run started" },
  "run.succeeded": { icon: CircleCheck, tone: "text-ok", title: () => "Run succeeded" },
  "run.failed": { icon: CircleX, tone: "text-bad", title: (e) => `Run failed${e.data.error ? `: ${str((e.data.error as { message?: string }).message ?? e.data.error)}` : ""}` },
  "run.cancelled": { icon: CircleX, tone: "text-muted", title: () => "Run cancelled" },
  "run.waiting": { icon: Clock, tone: "text-warn", title: (e) => (e.data.next_wake_at ? `Waiting until ${new Date(str(e.data.next_wake_at)).toLocaleString()}` : "Waiting") },
  "run.cancel_requested": { icon: CircleX, tone: "text-muted", title: () => "Cancellation requested" },
  "step.started": { icon: Play, tone: "text-info", title: (e) => `Started${Number(e.data.attempt) > 1 ? ` (attempt ${e.data.attempt})` : ""}` },
  "step.succeeded": { icon: CircleCheck, tone: "text-ok", title: () => "Succeeded" },
  "step.failed": { icon: CircleX, tone: "text-bad", title: (e) => `Failed: ${str(e.data.error)}` },
  "step.retrying": { icon: RotateCcw, tone: "text-warn", title: (e) => `Retrying after error: ${str(e.data.error)}` },
  "step.skipped": { icon: SkipForward, tone: "text-muted", title: (e) => `Skipped (${str(e.data.reason)})` },
  "step.waiting": { icon: Clock, tone: "text-warn", title: (e) => `Waiting: ${str(e.data.reason)}${e.data.wake_at ? ` · next check ${new Date(str(e.data.wake_at)).toLocaleTimeString()}` : ""}` },
  "approval.requested": { icon: Hand, tone: "text-warn", title: (e) => `Approval requested: ${str(e.data.message)}` },
  "approval.decided": { icon: Hand, tone: "text-ok", title: (e) => `${e.data.approved ? "Approved" : "Rejected"} by ${actorName(e.data.by as string)}${e.data.comment ? ` — “${str(e.data.comment)}”` : ""}` },
  "signal.received": {
    icon: Radio,
    tone: "text-info",
    title: (e) => `Signal ${str(e.data.name)} received${e.data.source === "callback" ? " via callback URL" : e.data.source === "event" ? " from an integration event" : ""}${e.data.sender ? ` from ${str(e.data.sender)}` : ""}`,
  },
  "credential.required": {
    icon: KeyRound,
    tone: "text-warn",
    title: (e) => `Waiting for ${str(e.data.owner).replace(/^user:/, "")} to connect ${str(e.data.namespace)}/${str(e.data.connection)}`,
    body: (e) => (
      <p className="text-xs text-muted">
        {str(e.data.message)}{" "}
        <Link to={connectPath(str(e.data.namespace), str(e.data.connection))} className="text-accent hover:underline">
          Connected accounts
        </Link>
      </p>
    ),
  },
  "action.called": { icon: Zap, tone: "text-accent", title: (e) => `Called ${str(e.data.action)}` },
  "script.started": { icon: SquareTerminal, tone: "text-muted", title: () => "Script started" },
  "script.finished": {
    icon: SquareTerminal,
    tone: "text-muted",
    title: (e) => `Script exited with ${str(e.data.exit_code)}`,
    body: (e) => <Terminal stdout={str(e.data.stdout)} stderr={str(e.data.stderr)} />,
  },
  "wait.polled": { icon: Clock, tone: "text-muted", title: (e) => (e.data.satisfied ? "Condition met" : `Not yet${e.data.next_poll ? ` · next poll ${new Date(str(e.data.next_poll)).toLocaleTimeString()}` : ""}`) },
  "wait.poll_error": { icon: ShieldAlert, tone: "text-warn", title: (e) => `Poll failed: ${str(e.data.error)}` },
  "decision.made": { icon: Cpu, tone: "text-accent", title: () => "Decision made" },
  "model.routed": { icon: Cpu, tone: "text-muted", title: (e) => `Model ${str(e.data.model)}${e.data.rule ? ` (rule ${str(e.data.rule)})` : ""}` },
  "model.fallback": { icon: Cpu, tone: "text-warn", title: (e) => `Fell back: ${str(e.data.reason)}` },
  "agent.started": { icon: Bot, tone: "text-accent", title: (e) => `Agent ${str(e.data.agent)} started${e.data.model ? ` on ${str(e.data.model)}` : ""}` },
  "agent.finished": { icon: Bot, tone: "text-ok", title: (e) => `Agent ${str(e.data.agent)} finished` },
  "agent.tool_blocked": { icon: ShieldAlert, tone: "text-bad", title: (e) => `Blocked tool ${str(e.data.tool)}` },
  "agent.action": { icon: Zap, tone: "text-accent", title: (e) => `Agent called ${str(e.data.action)}` },
  "workflow.child_started": { icon: Play, tone: "text-info", title: (e) => `Started child run of ${str(e.data.workflow)}` },
};

export function EventRow({ event, showStep, onStep }: { event: RunEvent; showStep?: boolean; onStep?: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const look = LOOKS[event.type] ?? { icon: Wrench, tone: "text-muted", title: () => event.type };
  const Icon = look.icon;
  const hasData = Object.keys(event.data ?? {}).length > 0;
  return (
    <div className="group relative pl-7">
      <span className={cn("absolute top-1 left-0 flex size-5 items-center justify-center rounded-full border border-border bg-panel", look.tone)}>
        <Icon className="size-3" />
      </span>
      <button onClick={() => hasData && setOpen(!open)} className={cn("flex w-full items-start gap-2 text-left", hasData && "cursor-pointer")}>
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] leading-snug">
            {showStep && event.step_id && (
              <span
                role="link"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation();
                  onStep?.(event.step_id!);
                }}
                className="mr-1.5 rounded bg-subtle px-1 font-mono text-[11px] font-medium text-fg hover:text-accent"
              >
                {event.step_id}
              </span>
            )}
            <span className="break-words">{look.title(event)}</span>
          </div>
          <div className="mt-0.5 font-mono text-[10.5px] text-faint">
            {time(event.created_at)} · {event.type}
          </div>
        </div>
        {hasData && <ChevronRight className={cn("mt-0.5 size-3.5 shrink-0 text-faint transition-transform", open && "rotate-90")} />}
      </button>
      {look.body && <div className="mt-1.5">{look.body(event)}</div>}
      {open && <JsonBlock value={event.data} className="mt-1.5" maxHeight="260px" />}
    </div>
  );
}

export function Terminal({ stdout, stderr }: { stdout?: string; stderr?: string }) {
  if (!stdout && !stderr) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-[#0b0f15] font-mono text-[11.5px] leading-relaxed text-[#d6dde8]">
      <pre className="max-h-64 overflow-auto px-3 py-2 whitespace-pre-wrap">
        {stdout}
        {stderr && <span className="text-[#f87171]">{stderr}</span>}
      </pre>
    </div>
  );
}

/** Agent transcript: model turns with their tool calls and results, as a conversation. */
export function Transcript({ events }: { events: RunEvent[] }) {
  const items = events.filter((e) => e.type.startsWith("agent.") || e.type.startsWith("model."));
  if (!items.length) return null;
  return (
    <div className="space-y-3">
      {items.map((e) => {
        if (e.type === "agent.turn") {
          const calls = (e.data.tool_calls as { name: string; input: unknown }[]) ?? [];
          const usage = e.data.usage as { input_tokens?: number; output_tokens?: number } | undefined;
          return (
            <div key={e.id} className="rounded-xl border border-border bg-panel">
              <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-[11px] text-faint">
                <Bot className="size-3.5 text-accent" />
                <span className="font-medium text-muted">{str(e.data.agent)}</span>
                <span>turn {str(e.data.turn)}</span>
                {Number(e.data.depth) > 0 && <span>· sub-agent depth {str(e.data.depth)}</span>}
                {usage && (
                  <span className="ml-auto tabular-nums">
                    {usage.input_tokens ?? 0} in · {usage.output_tokens ?? 0} out
                  </span>
                )}
              </div>
              {str(e.data.text) && <div className="px-3 py-2 text-[12.5px] leading-relaxed whitespace-pre-wrap">{str(e.data.text)}</div>}
              {calls.length > 0 && (
                <div className="space-y-1.5 px-3 pb-2.5">
                  {calls.map((c, i) => (
                    <ToolCall key={i} name={c.name} input={c.input} />
                  ))}
                </div>
              )}
            </div>
          );
        }
        if (e.type === "agent.tool_result") {
          return (
            <div key={e.id} className={cn("ml-6 rounded-lg border px-3 py-2", e.data.is_error ? "border-bad/30 bg-bad-soft" : "border-border bg-subtle/50")}>
              <div className="mb-1 flex items-center gap-1.5 text-[11px] text-faint">
                <Wrench className="size-3" /> result of <span className="font-mono text-muted">{str(e.data.tool)}</span>
                {e.data.is_error ? <span className="text-bad">error</span> : null}
              </div>
              <pre className="max-h-48 overflow-auto font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap">{str(e.data.output)}</pre>
            </div>
          );
        }
        return <EventRow key={e.id} event={e} />;
      })}
    </div>
  );
}

function ToolCall({ name, input }: { name: string; input: unknown }) {
  const [open, setOpen] = useState(false);
  const text = typeof input === "string" ? input : JSON.stringify(input, null, 2);
  const preview = (typeof input === "string" ? input : JSON.stringify(input))?.slice(0, 90);
  return (
    <div className="rounded-lg border border-border bg-subtle/40">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left">
        <Wrench className="size-3 text-accent" />
        <span className="font-mono text-[11.5px] font-medium">{name}</span>
        {!open && <span className="truncate font-mono text-[11px] text-faint">{preview}</span>}
        <ChevronRight className={cn("ml-auto size-3.5 shrink-0 text-faint transition-transform", open && "rotate-90")} />
      </button>
      {open && (
        <div className="border-t border-border">
          <CodeEditor value={text ?? ""} language="json" readOnly lineNumbers={false} maxHeight="220px" />
        </div>
      )}
    </div>
  );
}
