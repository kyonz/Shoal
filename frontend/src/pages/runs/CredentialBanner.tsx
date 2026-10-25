/** Steps waiting for a person to connect their own account to a connection (`credential.required`). */
import { KeyRound } from "lucide-react";
import { Link } from "react-router";
import type { RunDetail, RunEvent } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui";
import { connectPath } from "@/pages/connect/PersonalCredentials";

export interface CredentialWait {
  stepId: string;
  owner: string | null;
  namespace: string;
  connection: string;
  message: string;
}

/** Waiting steps whose latest event asks for a credential. */
export function credentialWaits(run: RunDetail, events: RunEvent[]): CredentialWait[] {
  const waiting = new Set(run.steps.filter((s) => s.status === "waiting").map((s) => s.step_id));
  const latest = new Map<string, RunEvent>();
  for (const e of events) if (e.step_id && waiting.has(e.step_id) && e.type === "credential.required") latest.set(e.step_id, e);
  return [...latest.entries()].map(([stepId, e]) => ({
    stepId,
    owner: (e.data.owner as string | null) ?? null,
    namespace: String(e.data.namespace ?? ""),
    connection: String(e.data.connection ?? ""),
    message: String(e.data.message ?? ""),
  }));
}

const who = (owner: string | null) => (owner ?? "").replace(/^user:/, "");

export function CredentialBanner({ run, events }: { run: RunDetail; events: RunEvent[] }) {
  const { me } = useAuth();
  const waits = credentialWaits(run, events);
  if (!waits.length) return null;
  return (
    <div className="shrink-0 space-y-3 border-b border-warn/30 bg-warn-soft px-4 py-3">
      {waits.map((w) => {
        const mine = !!me?.actor && me.actor === w.owner;
        return (
          <div key={w.stepId} className="flex flex-wrap items-center gap-3">
            <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-warn text-white">
              <KeyRound className="size-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-semibold text-fg">
                {mine ? "Connect your account to continue" : `Waiting for ${who(w.owner)} to connect their account`}{" "}
                <span className="font-mono font-normal text-muted">· {w.stepId}</span>
              </div>
              <div className="text-[12.5px] text-muted">
                <span className="font-mono">
                  {w.namespace}/{w.connection}
                </span>{" "}
                uses each person's own credential.{" "}
                {mine ? "The step carries on as soon as you connect." : "The step carries on once they connect it under Connected accounts."}
              </div>
            </div>
            {mine && (
              <Link to={connectPath(w.namespace, w.connection)}>
                <Button variant="primary" icon={<KeyRound className="size-3.5" />}>
                  Connect your account
                </Button>
              </Link>
            )}
          </div>
        );
      })}
    </div>
  );
}
