import { Ban, CheckCircle2, CircleDashed, CircleDot, Clock, Loader2, Pause, SkipForward, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Badge } from "./primitives";

type Tone = "neutral" | "accent" | "ok" | "warn" | "bad" | "info";

export const STATUS: Record<string, { tone: Tone; label: string; icon: ReactNode }> = {
  pending: { tone: "neutral", label: "Pending", icon: <CircleDashed /> },
  draft: { tone: "neutral", label: "Draft", icon: <CircleDashed /> },
  running: { tone: "info", label: "Running", icon: <Loader2 className="animate-spin" /> },
  waiting: { tone: "warn", label: "Waiting", icon: <Clock /> },
  paused: { tone: "warn", label: "Paused", icon: <Pause /> },
  succeeded: { tone: "ok", label: "Succeeded", icon: <CheckCircle2 /> },
  completed: { tone: "ok", label: "Completed", icon: <CheckCircle2 /> },
  failed: { tone: "bad", label: "Failed", icon: <XCircle /> },
  cancelled: { tone: "neutral", label: "Cancelled", icon: <Ban /> },
  skipped: { tone: "neutral", label: "Skipped", icon: <SkipForward /> },
};

export function statusTone(status: string | null | undefined): Tone {
  return STATUS[status ?? ""]?.tone ?? "neutral";
}

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const s = STATUS[status] ?? { tone: "neutral" as Tone, label: status, icon: <CircleDot /> };
  return (
    <Badge tone={s.tone} className={cn("[&>svg]:size-3", className)}>
      {s.icon}
      {s.label}
    </Badge>
  );
}

export function StatusIcon({ status, className }: { status: string; className?: string }) {
  const s = STATUS[status] ?? { tone: "neutral" as Tone, icon: <CircleDot /> };
  const color = { neutral: "text-idle", accent: "text-accent", ok: "text-ok", warn: "text-warn", bad: "text-bad", info: "text-info" }[s.tone];
  return <span className={cn("inline-flex [&>svg]:size-4", color, className)}>{s.icon}</span>;
}
