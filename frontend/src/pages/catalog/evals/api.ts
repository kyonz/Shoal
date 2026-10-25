/**
 * Eval runs (`/v1/.../eval-runs`). Local types mirror the server's EvalRunOut /
 * EvalResultOut until `schema.d.ts` is regenerated; readers tolerate missing
 * fields and a server without the endpoints (`isMissingEndpoint`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, rawJson } from "@/api/client";

export interface VariantSummary {
  workflow_version?: number | null;
  cases?: number;
  graded?: number;
  errors?: number;
  pending?: number;
  score?: number | null;
  pass_rate?: number | null;
  graders?: Record<string, number | null>;
  cost_usd?: number;
  mean_duration_ms?: number | null;
}

export interface CaseDiff {
  case: string;
  baseline: number;
  candidate: number;
  delta: number;
  graders: Record<string, number>;
}

export interface Comparison {
  baseline?: { workflow_version?: number; score?: number | null; pass_rate?: number | null };
  candidate?: { workflow_version?: number; score?: number | null; pass_rate?: number | null };
  shared_cases?: number;
  delta?: number | null;
  graders?: Record<string, number>;
  regressions?: string[];
  improvements?: string[];
  cases?: CaseDiff[];
}

export interface EvalRun {
  id: string;
  namespace: string;
  eval_name: string;
  eval_version?: number;
  workflow_name?: string;
  workflow_version?: number;
  variants?: { name: string; workflow_version?: number; inputs?: Record<string, unknown> }[];
  mode?: string;
  status: string;
  /** The Eval spec as it was when the run started. */
  spec?: { threshold?: number; [k: string]: unknown };
  summary?: VariantSummary & {
    variants?: Record<string, VariantSummary>;
    baseline_diff?: Record<string, Comparison>;
    variant_diff?: Record<string, Comparison>;
  };
  gate?: { passed?: boolean; reasons?: string[]; fail_under?: number | null; max_regression?: number | null };
  baseline_id?: string | null;
  error?: { message?: string } | null;
  started_by?: string | null;
  created_at: string;
  finished_at?: string | null;
}

export interface EvalResult {
  id: string;
  variant: string;
  workflow_version?: number;
  case_name: string;
  case?: Record<string, unknown>;
  run_id?: string | null;
  status: string;
  outputs?: Record<string, unknown> | null;
  scores?: Record<string, { score?: number; passed?: boolean; error?: string; detail?: Record<string, unknown>; [k: string]: unknown }>;
  score?: number | null;
  passed?: boolean | null;
  error?: { message?: string; [k: string]: unknown } | null;
  cost_usd?: number;
  duration_ms?: number | null;
}

export interface EvalRunDetail extends EvalRun {
  results: EvalResult[];
}

export interface ScoreHistory {
  versions: {
    workflow_version: number;
    runs: number;
    mean_score: number;
    latest?: { eval_run_id: string; variant: string; score: number; pass_rate?: number | null; gate_passed?: boolean | null; finished_at?: string | null } | null;
  }[];
}

/** True when the server does not have the route at all (older server). */
export function isMissingEndpoint(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 404 || e.status === 405) && (e.code === "http_404" || e.code === "http_405");
}

export function isTerminal(status: string) {
  return ["completed", "succeeded", "failed", "cancelled"].includes(status);
}

export function useEvalRuns(ns: string, evalName?: string) {
  return useQuery({
    queryKey: ["eval-runs", ns, evalName ?? ""],
    queryFn: async () => {
      const q = new URLSearchParams({ limit: "100" });
      if (evalName) q.set("eval", evalName);
      const res = await rawJson<{ items: EvalRun[] } | EvalRun[]>(`/v1/namespaces/${ns}/eval-runs?${q}`);
      return Array.isArray(res) ? res : res.items;
    },
    retry: false,
    refetchInterval: (q) => (q.state.data?.some((r) => !isTerminal(r.status)) ? 3000 : false),
  });
}

export function useEvalRun(id: string | undefined) {
  return useQuery({
    queryKey: ["eval-run", id],
    queryFn: () => rawJson<EvalRunDetail>(`/v1/eval-runs/${id}`),
    enabled: !!id,
    retry: false,
    refetchInterval: (q) => (q.state.data && !isTerminal(q.state.data.status) ? 2500 : false),
  });
}

export function useEvalScores(ns: string, name: string | undefined) {
  return useQuery({
    queryKey: ["eval-scores", ns, name],
    queryFn: () => rawJson<ScoreHistory>(`/v1/namespaces/${ns}/evals/${encodeURIComponent(name!)}/scores`),
    enabled: !!name,
    retry: false,
  });
}

export function useCompare(id: string | undefined, baseline: string | undefined) {
  return useQuery({
    queryKey: ["eval-compare", id, baseline],
    queryFn: () => rawJson<{ baseline_id: string; candidate_id: string; variants: Record<string, Comparison> }>(`/v1/eval-runs/${id}/compare?baseline=${encodeURIComponent(baseline!)}`),
    enabled: !!id && !!baseline,
    retry: false,
  });
}

export function useStartEvalRun(ns: string, name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      rawJson<EvalRunDetail>(`/v1/namespaces/${ns}/evals/${encodeURIComponent(name)}/runs`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["eval-runs", ns] }),
  });
}

export function useCancelEvalRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rawJson<EvalRun>(`/v1/eval-runs/${id}/cancel`, { method: "POST" }),
    onSuccess: (_r, id) => qc.invalidateQueries({ queryKey: ["eval-run", id] }),
  });
}

export function pct(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—";
  return `${Math.round(n * 1000) / 10}%`;
}
