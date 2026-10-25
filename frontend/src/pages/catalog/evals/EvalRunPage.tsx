import { ArrowDownRight, ArrowUpRight, Ban, CheckCircle2, ChevronRight, FlaskConical, Minus, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { toast } from "sonner";
import { useResource } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { between, duration, relTime, usd } from "@/lib/format";
import { nsPath, useNs } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorNotice,
  JsonBlock,
  NativeSelect,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Stat,
  StatusBadge,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui";
import { isMissingEndpoint, isTerminal, pct, useCancelEvalRun, useEvalRun, type Comparison, type EvalResult } from "./api";
import { NewerServerNotice } from "./EvalExtension";
import { ExportRunButton } from "./Platforms";

function Delta({ value }: { value: number | null | undefined }) {
  if (value == null || !isFinite(value)) return <span className="text-faint">—</span>;
  const pts = Math.round(value * 1000) / 10;
  if (Math.abs(pts) < 0.05)
    return (
      <span className="inline-flex items-center gap-0.5 text-muted">
        <Minus className="size-3" />0
      </span>
    );
  const up = pts > 0;
  return (
    <span className={cn("inline-flex items-center gap-0.5 font-medium tabular-nums", up ? "text-ok" : "text-bad")}>
      {up ? <ArrowUpRight className="size-3.5" /> : <ArrowDownRight className="size-3.5" />}
      {up ? "+" : ""}
      {pts} pts
    </span>
  );
}

function ScoreBar({ value, threshold }: { value: number | null | undefined; threshold?: number }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const ok = threshold == null || v >= threshold;
  return (
    <div className="relative h-1.5 w-28 overflow-hidden rounded-full bg-subtle">
      <div className={cn("h-full rounded-full transition-[width]", ok ? "bg-ok" : "bg-bad")} style={{ width: `${v * 100}%` }} />
      {threshold != null && <div className="absolute top-0 h-full w-0.5 bg-fg/60" style={{ left: `${threshold * 100}%` }} />}
    </div>
  );
}

type Filter = "all" | "failed" | "passed" | "regressed" | "errors";

export default function EvalRunPage() {
  const ns = useNs();
  const { id } = useParams();
  const run = useEvalRun(id);
  const evalRes = useResource("evals", ns, run.data?.eval_name);
  const cancel = useCancelEvalRun();
  const canRun = useCan("evals:run", ns);
  const [filter, setFilter] = useState<Filter>("all");
  const [variant, setVariant] = useState<string>("");
  const [open, setOpen] = useState<string | null>(null);

  const r = run.data;
  const results = r?.results ?? [];
  const variantNames = useMemo(() => [...new Set(results.map((x) => x.variant))], [results]);
  const activeVariant = variant || variantNames[0] || "";
  const multi = variantNames.length > 1;
  const threshold = (evalRes.data?.spec?.threshold as number | undefined) ?? 0.8;
  const diff: Comparison | undefined = r?.summary?.baseline_diff?.[activeVariant] ?? (multi ? r?.summary?.variant_diff?.[activeVariant] : undefined);
  const caseDiff = useMemo(() => Object.fromEntries((diff?.cases ?? []).map((c) => [c.case, c])), [diff]);
  const regressed = new Set(diff?.regressions ?? []);
  const rows = results.filter((x) => !multi || x.variant === activeVariant);
  const graderNames = useMemo(() => [...new Set(rows.flatMap((x) => Object.keys(x.scores ?? {})))], [rows]);
  const shown = rows.filter((x) =>
    filter === "all" ? true : filter === "failed" ? x.passed === false : filter === "passed" ? x.passed === true : filter === "errors" ? !!x.error : regressed.has(x.case_name),
  );

  if (run.isLoading)
    return (
      <Page wide>
        <Skeleton className="mb-4 h-8 w-64" />
        <Skeleton className="h-96 w-full" />
      </Page>
    );
  if (isMissingEndpoint(run.error))
    return (
      <Page>
        <Card>
          <NewerServerNotice what="Eval runs" />
        </Card>
      </Page>
    );
  if (run.error || !r)
    return (
      <Page>
        <ErrorNotice error={run.error ?? "Not found"} />
      </Page>
    );

  const s = r.summary ?? {};
  const vs = multi ? s.variants?.[activeVariant] ?? s : s;
  const done = isTerminal(r.status);
  const progress = s.cases ? ((s.graded ?? 0) + (s.errors ?? 0)) / s.cases : 0;
  const gate = r.gate;
  const nPass = rows.filter((x) => x.passed).length;

  return (
    <Page wide>
      <PageHeader
        icon={<FlaskConical className="size-4" />}
        breadcrumb={
          <>
            <Link to={nsPath(ns, "evals")} className="hover:text-fg">Evals</Link>
            <ChevronRight className="size-3" />
            <Link to={nsPath(ns, "evals", r.eval_name)} className="hover:text-fg">{r.eval_name}</Link>
            <ChevronRight className="size-3" />
            <span className="font-mono text-fg">{r.id.slice(-10)}</span>
          </>
        }
        title={
          <span className="flex items-center gap-2">
            {r.eval_name} <StatusBadge status={r.status} />
          </span>
        }
        description={
          <span>
            {r.workflow_name} {multi ? `· ${variantNames.length} variants` : `v${r.workflow_version}`} · started {relTime(r.created_at)}
            {r.started_by ? ` by ${r.started_by}` : ""} · {between(r.created_at, r.finished_at)}
          </span>
        }
        actions={
          !done && canRun ? (
            <Button
              icon={<Ban className="size-4" />}
              loading={cancel.isPending}
              onClick={() => cancel.mutate(r.id, { onSuccess: () => toast.success("Cancelling eval run"), onError: (e) => toast.error(e.message) })}
            >
              Cancel
            </Button>
          ) : done && canRun ? (
            <ExportRunButton ns={ns} runId={r.id} evalSpec={evalRes.data?.spec} />
          ) : null
        }
      />

      {!done && (
        <Card className="mb-4 p-4">
          <div className="mb-2 flex justify-between text-xs text-muted">
            <span>Running cases…</span>
            <span className="tabular-nums">
              {(s.graded ?? 0) + (s.errors ?? 0)} / {s.cases ?? "?"}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-subtle">
            <div className="h-full rounded-full bg-info transition-[width]" style={{ width: `${progress * 100}%` }} />
          </div>
        </Card>
      )}

      {done && gate?.passed != null && (gate.fail_under != null || gate.max_regression != null || (gate.reasons?.length ?? 0) > 0) && (
        <div className={cn("mb-4 flex items-start gap-3 rounded-xl border px-4 py-3", gate.passed ? "border-ok/25 bg-ok-soft" : "border-bad/25 bg-bad-soft")}>
          {gate.passed ? <ShieldCheck className="mt-0.5 size-5 text-ok" /> : <ShieldAlert className="mt-0.5 size-5 text-bad" />}
          <div className="text-[13px]">
            <div className={cn("font-semibold", gate.passed ? "text-ok" : "text-bad")}>Gate {gate.passed ? "passed" : "failed"}</div>
            <div className="text-muted">
              {gate.fail_under != null && `Minimum score ${pct(gate.fail_under)}. `}
              {gate.max_regression != null && `Max regression ${pct(gate.max_regression)}. `}
            </div>
            {gate.reasons?.map((x) => <div key={x} className="font-mono text-xs text-bad">{x}</div>)}
          </div>
        </div>
      )}
      {r.error?.message && <ErrorNotice className="mb-4" error={r.error.message} />}

      {multi && (
        <Card className="mb-4">
          <CardHeader title="Variants" description="Each arm runs every case; pick one to see its cases." />
          <Table>
            <thead>
              <tr>
                <Th>Variant</Th>
                <Th>Workflow</Th>
                <Th className="text-right">Score</Th>
                <Th />
                <Th className="text-right">Pass rate</Th>
                <Th className="text-right">Δ</Th>
                <Th className="text-right">Cost</Th>
              </tr>
            </thead>
            <tbody>
              {variantNames.map((name) => {
                const v = s.variants?.[name] ?? {};
                const d = s.baseline_diff?.[name] ?? s.variant_diff?.[name];
                return (
                  <Tr key={name} onClick={() => setVariant(name)} className={name === activeVariant ? "bg-accent-soft/40" : undefined}>
                    <Td className="font-mono text-xs font-medium">{name}</Td>
                    <Td className="text-xs text-muted">v{v.workflow_version ?? "?"}</Td>
                    <Td className="text-right font-medium tabular-nums">{pct(v.score)}</Td>
                    <Td><ScoreBar value={v.score} threshold={threshold} /></Td>
                    <Td className="text-right tabular-nums">{pct(v.pass_rate)}</Td>
                    <Td className="text-right"><Delta value={d?.delta} /></Td>
                    <Td className="text-right text-xs text-muted">{usd(v.cost_usd)}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </Card>
      )}

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label={multi ? `Score · ${activeVariant}` : "Score"} value={pct(vs.score)} sub={`case threshold ${pct(threshold)}`} tone={vs.score != null && vs.score < threshold ? "bad" : "ok"} />
        <Stat label="Cases passed" value={rows.length ? `${nPass}/${rows.length}` : "—"} sub={`pass rate ${pct(vs.pass_rate)}${vs.errors ? ` · ${vs.errors} errors` : ""}`} />
        <Stat
          label={r.baseline_id ? "vs baseline" : multi ? "vs first variant" : "vs baseline"}
          value={<Delta value={diff?.delta} />}
          sub={
            r.baseline_id ? (
              <Link className="hover:text-fg" to={nsPath(ns, "eval-runs", r.baseline_id)}>
                {r.baseline_id.slice(-10)}
                {diff?.baseline?.workflow_version ? ` · v${diff.baseline.workflow_version}` : ""}
              </Link>
            ) : (
              "No baseline"
            )
          }
        />
        <Stat
          label="Changes"
          value={diff ? <span><span className="text-bad">{diff.regressions?.length ?? 0}↓</span> <span className="text-ok">{diff.improvements?.length ?? 0}↑</span></span> : "—"}
          sub={`${usd(vs.cost_usd)}${vs.mean_duration_ms ? ` · ${duration(vs.mean_duration_ms)} avg` : ""}`}
        />
      </div>

      {Object.keys(vs.graders ?? {}).length > 0 && (
        <Card className="mb-4 p-4">
          <div className="mb-3 text-[11px] font-semibold tracking-wider text-faint uppercase">Graders</div>
          <div className="grid gap-x-8 gap-y-2.5 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(vs.graders ?? {}).map(([g, score]) => (
              <div key={g} className="flex items-center gap-3">
                <span className="w-32 truncate font-mono text-xs">{g}</span>
                <ScoreBar value={score} />
                <span className="w-12 text-right text-xs tabular-nums">{pct(score)}</span>
                {diff?.graders?.[g] != null && <span className="text-xs"><Delta value={diff.graders[g]} /></span>}
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card>
        <CardHeader
          title="Cases"
          actions={
            <div className="flex items-center gap-2">
              {multi && (
                <NativeSelect className="h-8 w-40" value={activeVariant} onChange={(e) => setVariant(e.target.value)}>
                  {variantNames.map((v) => <option key={v} value={v}>{v}</option>)}
                </NativeSelect>
              )}
              <Segmented
                value={filter}
                onChange={setFilter}
                options={[
                  { value: "all" as Filter, label: `All ${rows.length}` },
                  { value: "failed" as Filter, label: `Failed ${rows.filter((x) => x.passed === false).length}` },
                  { value: "passed" as Filter, label: `Passed ${nPass}` },
                  ...(rows.some((x) => x.error) ? [{ value: "errors" as Filter, label: "Errors" }] : []),
                  ...(diff ? [{ value: "regressed" as Filter, label: `Regressed ${regressed.size}` }] : []),
                ]}
              />
            </div>
          }
        />
        {!rows.length ? (
          <EmptyState title="No case results yet" description="Results appear here as each case finishes." />
        ) : !shown.length ? (
          <EmptyState title="No cases match this filter" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Case</Th>
                <Th>Result</Th>
                <Th className="text-right">Score</Th>
                {graderNames.map((g) => <Th key={g} className="text-right">{g}</Th>)}
                {diff && <Th className="text-right">Δ baseline</Th>}
                <Th className="text-right">Time</Th>
                <Th className="text-right">Run</Th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => (
                <Fragment key={c.id}>
                  <Tr onClick={() => setOpen(open === c.id ? null : c.id)}>
                    <Td className="font-mono text-xs">
                      {c.case_name}
                      {regressed.has(c.case_name) && <Badge tone="bad" className="ml-1.5">regressed</Badge>}
                    </Td>
                    <Td><CaseResult c={c} /></Td>
                    <Td className="text-right tabular-nums">{pct(c.score)}</Td>
                    {graderNames.map((n) => {
                      const g = c.scores?.[n];
                      return (
                        <Td key={n} className={cn("text-right tabular-nums", g?.passed === false && "text-bad")} title={graderNote(g)}>
                          {g ? (g.error ? "error" : pct(g.score)) : "—"}
                        </Td>
                      );
                    })}
                    {diff && <Td className="text-right"><Delta value={caseDiff[c.case_name]?.delta} /></Td>}
                    <Td className="text-right text-xs text-muted">{c.duration_ms != null ? duration(c.duration_ms) : "—"}</Td>
                    <Td className="text-right">
                      {c.run_id ? (
                        <Link onClick={(e) => e.stopPropagation()} to={nsPath(ns, "runs", c.run_id)} className="font-mono text-xs text-accent hover:underline">
                          {c.run_id.slice(-8)}
                        </Link>
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </Td>
                  </Tr>
                  {open === c.id && (
                    <tr>
                      <td colSpan={6 + graderNames.length + (diff ? 1 : 0)} className="border-b border-border bg-subtle/40 px-4 py-3">
                        <div className="grid gap-3 lg:grid-cols-3">
                          <div>
                            <div className="mb-1 text-xs font-medium">Inputs & expectation</div>
                            <JsonBlock value={c.case ?? {}} maxHeight="260px" />
                          </div>
                          <div>
                            <div className="mb-1 text-xs font-medium">Outputs</div>
                            <JsonBlock value={c.outputs ?? null} maxHeight="260px" />
                          </div>
                          <div>
                            <div className={cn("mb-1 text-xs font-medium", c.error && "text-bad")}>{c.error ? "Error" : "Grades"}</div>
                            <JsonBlock value={c.error ?? c.scores ?? {}} maxHeight="260px" />
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </Page>
  );
}

/** Hover text for a grader cell: its error, or the judge's level and reasoning. */
function graderNote(g: NonNullable<EvalResult["scores"]>[string] | undefined): string | undefined {
  if (!g) return undefined;
  if (typeof g.error === "string") return g.error;
  const d = (g.detail ?? {}) as { level?: string; reasoning?: string; errors?: string[] };
  if (d.reasoning) return d.level ? `${d.level}: ${d.reasoning}` : d.reasoning;
  if (d.errors?.length) return d.errors.join("\n");
  return undefined;
}

function CaseResult({ c }: { c: EvalResult }) {
  if (c.status === "pending" || c.status === "running" || c.status === "cancelled") return <StatusBadge status={c.status} />;
  if (c.passed)
    return (
      <span className="inline-flex items-center gap-1 text-ok">
        <CheckCircle2 className="size-4" />
        Pass
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1 text-bad">
      <XCircle className="size-4" />
      {c.error ? "Error" : "Fail"}
    </span>
  );
}
