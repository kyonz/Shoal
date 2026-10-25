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
  Textarea,
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

type GraderType = "expression" | "json_schema" | "llm_judge" | "decision";

interface Grader {
  name: string;
  type?: GraderType;
  check?: string;
  question?: Question;
  state?: unknown;
  subject?: unknown;
  schema?: Record<string, unknown>;
  rubric?: string;
  levels?: string[];
  model?: string;
  gateway?: string;
  provider?: string;
  config?: Record<string, unknown>;
  weight?: number;
}

/** Fields that belong to each grader type; switching type keeps the new type's fields and drops the rest. */
const GRADER_FIELDS: Record<GraderType, (keyof Grader)[]> = {
  expression: ["check"],
  json_schema: ["subject", "schema"],
  llm_judge: ["subject", "rubric", "levels", "model", "gateway", "config"],
  decision: ["question", "state", "provider", "config"],
};

const GRADER_DEFAULTS: Record<GraderType, Partial<Grader>> = {
  expression: { check: "{{ true }}" },
  json_schema: { schema: { type: "object", required: [] } },
  llm_judge: { rubric: "Does the output meet the expectation?", levels: ["fail", "partial", "pass"] },
  decision: { question: { type: "score", instructions: "How well do the outputs meet the expectation?", levels: ["poor", "fair", "good", "excellent"] } },
};

function switchType(g: Grader, type: GraderType): Grader {
  const kept = Object.fromEntries(GRADER_FIELDS[type].filter((k) => g[k] !== undefined).map((k) => [k, g[k]]));
  return { name: g.name, weight: g.weight, type, ...GRADER_DEFAULTS[type], ...kept };
}

const asText = (v: unknown) => (v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

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
        description="Expression checks, JSON schema validation, an LLM judge or a decision model. Each scores 0 to 1; scores are weighted into the case score."
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
                <NativeSelect value={g.type ?? "expression"} onChange={(e) => update(i, switchType(g, e.target.value as GraderType))}>
                  <option value="expression">Expression</option>
                  <option value="json_schema">JSON schema</option>
                  <option value="llm_judge">LLM judge</option>
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
            <GraderFields key={`${i}-${g.type ?? "expression"}-${graders.length}`} g={g} onChange={(next) => update(i, next)} />
          </div>
        ))}
        {!graders.length && <EmptyState className="py-8" title="No graders" description="Add at least one grader to score each case." />}
      </fieldset>
    </Card>
  );
}

function SubjectField({ g, onChange, what }: { g: Grader; onChange: (g: Grader) => void; what: string }) {
  return (
    <Field label="Subject" hint="optional" description={`What ${what}; defaults to all outputs.`}>
      <Input mono value={asText(g.subject)} placeholder="{{ outputs }}" onChange={(e) => onChange({ ...g, subject: e.target.value || undefined })} />
    </Field>
  );
}

function GraderFields({ g, onChange }: { g: Grader; onChange: (g: Grader) => void }) {
  switch (g.type ?? "expression") {
    case "expression":
      return (
        <Field label="Check" description="Evaluated with `outputs`, `expect`, `inputs`, `case` and `run`; true or a number from 0 to 1.">
          <div className="rounded-lg border border-border bg-panel">
            <CodeEditor value={g.check ?? ""} language="text" lineNumbers={false} minHeight="36px" onChange={(t) => onChange({ ...g, check: t })} placeholder="{{ outputs.tests_passed }}" />
          </div>
        </Field>
      );
    case "json_schema":
      return (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <SubjectField g={g} onChange={onChange} what="must validate" />
          <Field label="Schema" description="JSON Schema the subject must match (scores 1 or 0).">
            <JsonField value={g.schema ?? {}} onChange={(v) => onChange({ ...g, schema: (v as Record<string, unknown>) ?? {} })} />
          </Field>
        </div>
      );
    case "llm_judge":
      return (
        <div className="space-y-3">
          <Field label="Rubric" description="Grading instructions for the judge model. It also sees the case's inputs and `expect`.">
            <Textarea rows={3} value={g.rubric ?? ""} onChange={(e) => onChange({ ...g, rubric: e.target.value })} />
          </Field>
          <Field label="Levels" description="Worst first; the first scores 0 and the last 1. At least two.">
            <TagInput value={g.levels ?? []} onChange={(levels) => onChange({ ...g, levels })} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Judge model" hint="optional">
              <Input mono value={g.model ?? ""} placeholder="claude-sonnet-5-5" onChange={(e) => onChange({ ...g, model: e.target.value || undefined })} />
            </Field>
            <Field label="Gateway" hint="optional" description="Gateway resource or provider.">
              <Input mono value={g.gateway ?? ""} placeholder="anthropic" onChange={(e) => onChange({ ...g, gateway: e.target.value || undefined })} />
            </Field>
            <SubjectField g={g} onChange={onChange} what="the judge grades" />
          </div>
        </div>
      );
    default:
      return (
        <div className="grid gap-3 sm:grid-cols-[160px_minmax(0,1fr)]">
          <Field label="Question type">
            <NativeSelect value={g.question?.type ?? "score"} onChange={(e) => onChange({ ...g, question: { ...(g.question ?? { instructions: "" }), type: e.target.value as Question["type"] } })}>
              <option value="score">Score (rubric)</option>
              <option value="boolean">Boolean</option>
            </NativeSelect>
          </Field>
          <Field label="Instructions">
            <Input value={g.question?.instructions ?? ""} onChange={(e) => onChange({ ...g, question: { ...(g.question ?? { type: "score" }), instructions: e.target.value } })} />
          </Field>
          {g.question?.type !== "boolean" && (
            <Field label="Levels" description="Lowest first." className="sm:col-span-2">
              <TagInput value={g.question?.levels ?? []} onChange={(levels) => onChange({ ...g, question: { ...(g.question ?? { type: "score", instructions: "" }), levels } })} />
            </Field>
          )}
        </div>
      );
  }
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
  const [versions, setVersions] = useState<string[]>([]);
  const [only, setOnly] = useState<string[]>([]);
  if (!canRun) return null;
  const missing = isMissingEndpoint(runs.error);
  const caseNames = ((ctx.spec.cases as EvalCase[]) ?? []).map((c) => c.name);
  const specVariants = ((ctx.spec.variants as unknown[]) ?? []).length;
  const versionNumbers = [...new Set(versions.map((v) => Number(v.replace(/^v/i, ""))).filter((n) => Number.isInteger(n) && n > 0))];
  const go = async () => {
    try {
      const body: Record<string, unknown> = { baseline };
      if (failUnder !== "") body.fail_under = Number(failUnder) / 100;
      if (versionNumbers.length === 1) body.version = versionNumbers[0];
      if (versionNumbers.length > 1) body.variants = versionNumbers.map((n) => ({ name: `v${n}`, version: n }));
      if (only.length) body.cases = only;
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
        description={`Runs ${only.length ? `${only.length} of ${cases} cases` : cases ? `${cases} case${cases === 1 ? "" : "s"}` : "every case"} through ${String(ctx.spec.workflow || "the workflow")} and grades the outputs.`}
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
          <Field
            label="Workflow versions"
            hint="optional"
            description={
              versionNumbers.length > 1
                ? `Runs every case against each version as its own variant and compares them with v${versionNumbers[0]}.`
                : `Defaults to ${specVariants ? "the eval's variants" : "the current version"}. Add two or more to compare versions side by side.`
            }
          >
            <TagInput value={versions} onChange={setVersions} placeholder="e.g. 3, then Enter" />
          </Field>
          {caseNames.length > 1 && (
            <Field label="Only these cases" hint="optional" description="Leave empty to run every case.">
              <TagInput value={only} onChange={setOnly} options={caseNames} />
            </Field>
          )}
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
