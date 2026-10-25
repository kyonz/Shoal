import { FlaskConical, Play, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { useResource } from "@/api/hooks";
import { useCan } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { nsPath } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CodeEditor,
  Dialog,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Skeleton,
  StatusBadge,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui";
import { JsonField, TagInput } from "@/components/forms/SchemaForm";
import { DecisionProviderSelect } from "../extensions";
import type { EditorContext, EditorExtension } from "../ResourceEditorPage";
import { ResourcePicker } from "../widgets";
import { PlatformCard } from "./Platforms";
import { isMissingEndpoint, pct, useEvalRuns, useEvalScores, useStartEvalRun } from "./api";

interface EvalCase {
  name: string;
  inputs?: Record<string, unknown>;
  expect?: Record<string, unknown>;
}

interface Question {
  type: "choice" | "score" | "boolean";
  instructions: string;
  options?: Record<string, string>;
  levels?: string[];
}

interface Grader {
  name: string;
  type?: "expression" | "decision";
  check?: string;
  question?: Question;
  state?: unknown;
  weight?: number;
}

export const evalExtension: EditorExtension = {
  widgets: ({ ns }) => ({
    workflow: ({ value, onChange }) => <ResourcePicker plural="workflows" ns={ns} value={value} onChange={onChange} placeholder="Choose a workflow…" />,
    decision_provider: ({ value, onChange }) => <DecisionProviderSelect value={value} onChange={onChange} />,
  }),
  sections: (ctx) => (
    <>
      <CasesEditor ctx={ctx} />
      <GradersEditor ctx={ctx} />
      <PlatformCard ctx={ctx} />
    </>
  ),
  actions: (ctx) => (ctx.isNew ? null : <RunEvalButton ctx={ctx} />),
  tabs: (ctx) => [{ value: "runs", label: <><FlaskConical className="size-3.5" /> Runs</>, content: <EvalRunsTab ctx={ctx} /> }],
};

// --------------------------------------------------------------------------- cases

function CasesEditor({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite, ns } = ctx;
  const cases = (spec.cases as EvalCase[]) ?? [];
  const workflow = useResource("workflows", ns, (spec.workflow as string) || undefined);
  const inputKeys = Object.keys(((workflow.data?.spec?.inputs as Record<string, unknown>) ?? {}));
  const set = (next: EvalCase[]) => setSpec({ ...spec, cases: next });
  const add = () =>
    set([...cases, { name: `case-${cases.length + 1}`, inputs: Object.fromEntries(inputKeys.map((k) => [k, ""])), expect: {} }]);
  return (
    <Card>
      <CardHeader
        title={`Cases (${cases.length})`}
        description={inputKeys.length ? `Workflow inputs: ${inputKeys.join(", ")}. "expect" is available to graders as \`expect\`.` : "Each case runs the workflow once with its inputs; graders see `expect`."}
        actions={canWrite && <Button size="sm" icon={<Plus className="size-3.5" />} onClick={add}>Add case</Button>}
      />
      {!cases.length ? (
        <EmptyState className="py-8" title="No cases" description="Add at least one case: the inputs to run and what you expect back." />
      ) : (
        <div className="divide-y divide-border">
          {cases.map((c, i) => (
            <div key={i} className="grid gap-3 px-4 py-3 md:grid-cols-[180px_minmax(0,1fr)_minmax(0,1fr)_32px]">
              <Field label="Name">
                <Input mono value={c.name} disabled={!canWrite} onChange={(e) => set(cases.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
              </Field>
              <Field label="Inputs">
                <JsonField key={`in-${i}-${cases.length}`} value={c.inputs ?? {}} onChange={(v) => set(cases.map((x, j) => (j === i ? { ...x, inputs: (v as Record<string, unknown>) ?? {} } : x)))} />
              </Field>
              <Field label="Expect">
                <JsonField key={`ex-${i}-${cases.length}`} value={c.expect ?? {}} onChange={(v) => set(cases.map((x, j) => (j === i ? { ...x, expect: (v as Record<string, unknown>) ?? {} } : x)))} />
              </Field>
              {canWrite && (
                <Button variant="ghost" size="icon-sm" className="mt-5" aria-label="Remove case" onClick={() => set(cases.filter((_, j) => j !== i))}>
                  <Trash2 className="size-3.5" />
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------- graders

function GradersEditor({ ctx }: { ctx: EditorContext }) {
  const { spec, setSpec, canWrite } = ctx;
  const graders = (spec.graders as Grader[]) ?? [];
  const set = (next: Grader[]) => setSpec({ ...spec, graders: next });
  const update = (i: number, g: Grader) => set(graders.map((x, j) => (j === i ? g : x)));
  return (
    <Card>
      <CardHeader
        title={`Graders (${graders.length})`}
        description="Expression checks over outputs, or a decision model scoring against a rubric. Scores are weighted into the case score."
        actions={
          canWrite && (
            <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => set([...graders, { name: `grader-${graders.length + 1}`, type: "expression", check: "{{ outputs is defined }}" }])}>
              Add grader
            </Button>
          )
        }
      />
      <fieldset disabled={!canWrite} className="divide-y divide-border">
        {graders.map((g, i) => (
          <div key={i} className="space-y-3 px-4 py-3">
            <div className="grid gap-3 sm:grid-cols-[1fr_160px_100px_32px]">
              <Field label="Name">
                <Input mono value={g.name} onChange={(e) => update(i, { ...g, name: e.target.value })} />
              </Field>
              <Field label="Type">
                <NativeSelect
                  value={g.type ?? "expression"}
                  onChange={(e) => {
                    const type = e.target.value as Grader["type"];
                    update(i, type === "decision"
                      ? { name: g.name, weight: g.weight, type, question: g.question ?? { type: "score", instructions: "How well do the outputs meet the expectation?", levels: ["poor", "fair", "good", "excellent"] } }
                      : { name: g.name, weight: g.weight, type, check: g.check ?? "{{ true }}" });
                  }}
                >
                  <option value="expression">Expression</option>
                  <option value="decision">Decision model</option>
                </NativeSelect>
              </Field>
              <Field label="Weight">
                <Input type="number" step="any" min={0} value={g.weight ?? ""} placeholder="1" onChange={(e) => update(i, { ...g, weight: e.target.value === "" ? undefined : Number(e.target.value) })} />
              </Field>
              <Button variant="ghost" size="icon-sm" className="mt-5" aria-label="Remove grader" onClick={() => set(graders.filter((_, j) => j !== i))}>
                <Trash2 className="size-3.5" />
              </Button>
            </div>
            {(g.type ?? "expression") === "expression" ? (
              <Field label="Check" description="Evaluated with `outputs`, `expect` and `run`; truthy passes.">
                <div className="rounded-lg border border-border bg-panel">
                  <CodeEditor value={g.check ?? ""} language="text" lineNumbers={false} minHeight="36px" onChange={(t) => update(i, { ...g, check: t })} placeholder="{{ outputs.tests_passed }}" />
                </div>
              </Field>
            ) : (
              <div className="grid gap-3 sm:grid-cols-[160px_minmax(0,1fr)]">
                <Field label="Question type">
                  <NativeSelect value={g.question?.type ?? "score"} onChange={(e) => update(i, { ...g, question: { ...(g.question ?? { instructions: "" }), type: e.target.value as Question["type"] } })}>
                    <option value="score">Score (rubric)</option>
                    <option value="boolean">Boolean</option>
                  </NativeSelect>
                </Field>
                <Field label="Instructions">
                  <Input value={g.question?.instructions ?? ""} onChange={(e) => update(i, { ...g, question: { ...(g.question ?? { type: "score" }), instructions: e.target.value } })} />
                </Field>
                {g.question?.type !== "boolean" && (
                  <Field label="Levels" description="Lowest first." className="sm:col-span-2">
                    <TagInput value={g.question?.levels ?? []} onChange={(levels) => update(i, { ...g, question: { ...(g.question ?? { type: "score", instructions: "" }), levels } })} />
                  </Field>
                )}
              </div>
            )}
          </div>
        ))}
        {!graders.length && <EmptyState className="py-8" title="No graders" description="Add at least one grader to score each case." />}
      </fieldset>
    </Card>
  );
}

// --------------------------------------------------------------------------- runs

function RunEvalButton({ ctx }: { ctx: EditorContext }) {
  const canRun = useCan("evals:run", ctx.ns);
  const navigate = useNavigate();
  const start = useStartEvalRun(ctx.ns, ctx.name!);
  const runs = useEvalRuns(ctx.ns, ctx.name);
  const [open, setOpen] = useState(false);
  const [baseline, setBaseline] = useState("previous");
  const [failUnder, setFailUnder] = useState("");
  if (!canRun) return null;
  const missing = isMissingEndpoint(runs.error);
  const go = async () => {
    try {
      const body: Record<string, unknown> = { baseline };
      if (failUnder !== "") body.fail_under = Number(failUnder) / 100;
      const run = await start.mutateAsync(body);
      toast.success("Eval started");
      setOpen(false);
      navigate(nsPath(ctx.ns, "eval-runs", run.id));
    } catch (e) {
      toast.error(isMissingEndpoint(e) ? "Eval runs need a newer server" : e instanceof Error ? e.message : "Could not start");
    }
  };
  const finished = (runs.data ?? []).filter((r) => r.status === "completed");
  const cases = ((ctx.spec.cases as unknown[]) ?? []).length;
  return (
    <>
      <Button icon={<Play className="size-4" />} disabled={missing || ctx.dirty} title={ctx.dirty ? "Save first" : missing ? "Eval runs need a newer server" : undefined} onClick={() => setOpen(true)}>
        Run eval
      </Button>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title={`Run ${ctx.name}`}
        description={`Runs ${cases ? `${cases} case${cases === 1 ? "" : "s"}` : "every case"} through ${String(ctx.spec.workflow || "the workflow")} and grades the outputs.`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" loading={start.isPending} onClick={() => void go()} icon={<Play className="size-4" />}>Start</Button>
          </>
        }
      >
        <div className="space-y-3.5">
          <Field label="Compare with" description="Case scores are diffed against this baseline.">
            <NativeSelect value={baseline} onChange={(e) => setBaseline(e.target.value)}>
              <option value="previous">Previous completed run</option>
              <option value="none">No baseline</option>
              {finished.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.id.slice(-8)} · v{r.workflow_version} · {pct(r.summary?.score)} · {relTime(r.created_at)}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Gate: minimum score" hint="optional, %" description="The run's gate fails when any variant scores below this.">
            <Input type="number" min={0} max={100} value={failUnder} placeholder="e.g. 85" onChange={(e) => setFailUnder(e.target.value)} />
          </Field>
        </div>
      </Dialog>
    </>
  );
}

export function NewerServerNotice({ what }: { what: string }) {
  return (
    <EmptyState
      icon={<FlaskConical className="size-5" />}
      title={`${what} need a newer server`}
      description="This Shoal API does not have the endpoints yet. Upgrade the server and this page lights up automatically."
    />
  );
}

function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 220, h = 44;
  const xs = points.map((_, i) => (i / (points.length - 1)) * (w - 8) + 4);
  const ys = points.map((p) => h - 4 - Math.max(0, Math.min(1, p)) * (h - 8));
  const d = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} className="overflow-visible" aria-label="Score trend">
      <path d={`${d} L${xs[xs.length - 1]},${h} L${xs[0]},${h} Z`} fill="var(--accent)" opacity={0.1} />
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {xs.map((x, i) => <circle key={i} cx={x} cy={ys[i]} r={2.5} fill="var(--panel)" stroke="var(--accent)" strokeWidth={1.5} />)}
    </svg>
  );
}

function ScoreTrend({ ctx }: { ctx: EditorContext }) {
  const scores = useEvalScores(ctx.ns, ctx.name);
  const versions = [...(scores.data?.versions ?? [])].sort((a, b) => a.workflow_version - b.workflow_version);
  if (!versions.length) return null;
  const last = versions[versions.length - 1];
  const prev = versions[versions.length - 2];
  return (
    <Card className="mb-4 flex flex-wrap items-center gap-6 p-4">
      <div>
        <div className="text-xs text-muted">Latest score · workflow v{last.workflow_version}</div>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="text-2xl font-semibold tabular-nums">{pct(last.latest?.score ?? last.mean_score)}</span>
          {prev && (
            <span className={(last.mean_score - prev.mean_score) >= 0 ? "text-xs text-ok" : "text-xs text-bad"}>
              {(last.mean_score - prev.mean_score) >= 0 ? "+" : ""}
              {Math.round((last.mean_score - prev.mean_score) * 1000) / 10} pts vs v{prev.workflow_version}
            </span>
          )}
        </div>
      </div>
      <Sparkline points={versions.map((v) => v.mean_score)} />
      <div className="ml-auto flex gap-1.5">
        {versions.slice(-6).map((v) => (
          <div key={v.workflow_version} className="rounded-lg border border-border px-2 py-1 text-center">
            <div className="text-[10px] text-faint">v{v.workflow_version}</div>
            <div className="text-xs font-medium tabular-nums">{pct(v.mean_score)}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function EvalRunsTab({ ctx }: { ctx: EditorContext }) {
  const runs = useEvalRuns(ctx.ns, ctx.name);
  const navigate = useNavigate();
  if (runs.isLoading) return <Skeleton className="h-48 w-full" />;
  if (isMissingEndpoint(runs.error)) return <Card><NewerServerNotice what="Eval runs" /></Card>;
  if (runs.error) return <Card className="p-4 text-[13px] text-bad">{runs.error.message}</Card>;
  const items = runs.data ?? [];
  return (
    <>
      <ScoreTrend ctx={ctx} />
      <Card>
        {!items.length ? (
          <EmptyState icon={<FlaskConical className="size-5" />} title="No runs yet" description="Run the eval to score the workflow against its cases." />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Run</Th>
                <Th>Status</Th>
                <Th>Workflow</Th>
                <Th className="text-right">Score</Th>
                <Th className="text-right">Pass rate</Th>
                <Th>Gate</Th>
                <Th className="text-right">Started</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <Tr key={r.id} onClick={() => navigate(nsPath(ctx.ns, "eval-runs", r.id))}>
                  <Td className="font-mono text-xs">{r.id.slice(-10)}</Td>
                  <Td><StatusBadge status={r.status} /></Td>
                  <Td className="text-xs text-muted">{(r.variants?.length ?? 0) > 1 ? `${r.variants!.length} variants` : `v${r.workflow_version ?? "?"}`}</Td>
                  <Td className="text-right font-medium tabular-nums">{pct(r.summary?.score)}</Td>
                  <Td className="text-right tabular-nums">{pct(r.summary?.pass_rate)}</Td>
                  <Td>{r.status !== "completed" || r.gate?.passed == null || (r.gate.fail_under == null && r.gate.max_regression == null) ? <span className="text-faint">—</span> : <Badge tone={r.gate.passed ? "ok" : "bad"}>{r.gate.passed ? "Passed" : "Failed"}</Badge>}</Td>
                  <Td className="text-right text-xs text-muted">{relTime(r.created_at)}{r.started_by && <div className="text-[11px] text-faint">{r.started_by}</div>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
