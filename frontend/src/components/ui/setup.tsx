/**
 * Errors, and the special case of missing configuration: say what is missing
 * and either link to where to set it up (for people who can) or say who to ask.
 */
import { CircleCheck, CloudOff, Lock, Settings2, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { FEDERATION_DISCONNECTED, federationCode, federationMessage } from "@/lib/federation";
import { askAdmin, problemFrom, problemTitle, sentence, setupProblem, useSetupStatus, whoFixes, type SetupProblem } from "@/lib/setup";
import { Button, Card, EmptyState } from "./primitives";

/** The fix for a setup problem: a button for people who can make it, otherwise who to ask. */
export function SetupAction({ problem, size = "sm", onNavigate }: { problem: SetupProblem; size?: "xs" | "sm" | "md"; onNavigate?: () => void }) {
  const { can } = useAuth();
  const who = whoFixes(problem, can);
  if (who === "admin" || !problem.fix) return <p className="text-[12.5px] text-muted">{askAdmin(problem)}</p>;
  return (
    <Link to={problem.fix.href} onClick={onNavigate}>
      <Button size={size} variant="primary" icon={who === "self" ? <UserRound className="size-3.5" /> : <Settings2 className="size-3.5" />}>
        {problem.fix.label}
      </Button>
    </Link>
  );
}

/** Inline: a failed call or step that needs something set up. */
export function SetupNotice({ problem, className, onNavigate }: { problem: SetupProblem; className?: string; onNavigate?: () => void }) {
  return (
    <div role="alert" className={cn("rounded-lg border border-warn/30 bg-warn-soft px-3 py-2.5 text-[13px]", className)}>
      <div className="flex items-center gap-1.5 font-semibold text-warn">
        <Settings2 className="size-3.5 shrink-0" /> {problemTitle(problem)}
      </div>
      <p className="mt-0.5 mb-2 break-words text-fg/80">{sentence(problem.detail)}</p>
      <SetupAction problem={problem} size="xs" onNavigate={onNavigate} />
    </div>
  );
}

/** Full-size, for a page or panel that cannot work until it is set up. */
export function SetupEmptyState({ problem, title, description, className, onNavigate }: {
  problem: SetupProblem;
  title?: ReactNode;
  description?: ReactNode;
  className?: string;
  onNavigate?: () => void;
}) {
  return (
    <EmptyState
      className={className}
      icon={<Settings2 className="size-5" />}
      title={title ?? problemTitle(problem)}
      description={
        <>
          {description ?? sentence(problem.detail)}
        </>
      }
      action={<SetupAction problem={problem} onNavigate={onNavigate} />}
    />
  );
}

export function ErrorNotice({ error, className }: { error: unknown; className?: string }) {
  if (!error) return null;
  const setup = setupProblem(error);
  if (setup) return <SetupNotice problem={setup} className={className} />;
  const fed = federationCode(error);
  if (fed)
    return (
      <div className={cn("flex items-start gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn", className)}>
        {fed === FEDERATION_DISCONNECTED ? <CloudOff className="mt-0.5 size-3.5 shrink-0" /> : <Lock className="mt-0.5 size-3.5 shrink-0" />}
        <span>{federationMessage(fed)}</span>
      </div>
    );
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className={cn("rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad", className)}>{msg}</div>
  );
}

/** "Finish setting up": the checks from `GET /v1/setup` that are not ready yet, each with its fix. Renders nothing when all are. */
export function SetupChecklist({ ns, className }: { ns: string; className?: string }) {
  const status = useSetupStatus(ns);
  if (!status.data || status.data.ready) return null;
  return (
    <Card className={cn("border-warn/30 p-4", className)}>
      <div className="mb-3 flex items-center gap-2">
        <Settings2 className="size-4 text-warn" />
        <h2 className="text-sm font-semibold">Finish setting up</h2>
      </div>
      <ul className="space-y-3">
        {status.data.checks.map((check, i, all) => {
          const problem = check.ready ? null : problemFrom(check.problem);
          // The assistant usually needs the same gateway as model access: one button is enough.
          const same = all.slice(0, i).some((c) => !c.ready && problemFrom(c.problem)?.fix?.href === problem?.fix?.href);
          return (
            <li key={check.id} className="flex items-start gap-3">
              {check.ready ? (
                <CircleCheck className="mt-0.5 size-4 shrink-0 text-ok" />
              ) : (
                <span className="mt-1 size-3 shrink-0 rounded-full border-2 border-warn" aria-hidden />
              )}
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium">{check.title}</div>
                {problem ? (
                  <>
                    <p className="mt-0.5 mb-1.5 text-[12.5px] text-muted">{sentence(problem.detail)}</p>
                    {same ? <p className="text-[12.5px] text-faint">Fixed by the step above.</p> : <SetupAction problem={problem} size="xs" />}
                  </>
                ) : (
                  <p className="text-[12.5px] text-faint">Ready</p>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
