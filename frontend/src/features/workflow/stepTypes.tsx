import {
  Bot,
  CheckCheck,
  Clock,
  GitFork,
  Gauge,
  Hand,
  Puzzle,
  SquareFunction,
  SquareTerminal,
  Zap,
  type LucideIcon,
} from "lucide-react";
import type { StepDoc } from "./types";

export interface StepTypeInfo {
  type: string;
  label: string;
  description: string;
  icon: LucideIcon;
  /** CSS colour used for the node's icon chip and accent. */
  color: string;
  /** Fields a new step starts with (besides id/type). */
  defaults: () => Record<string, unknown>;
  /** One-line summary shown on the node. */
  summary: (step: StepDoc) => string;
  /** Fields edited elsewhere in the inspector (common section). */
}

const first = (v: unknown, n = 80) => (typeof v === "string" ? v.split("\n").find((l) => l.trim())?.trim().slice(0, n) ?? "" : "");

export const STEP_TYPES: StepTypeInfo[] = [
  {
    type: "agent",
    label: "Agent",
    description: "Run an AI agent in a sandbox until it returns structured outputs.",
    icon: Bot,
    color: "#7c3aed",
    defaults: () => ({ prompt: "", outputs: { summary: { type: "string" } } }),
    summary: (s) => (s.agent ? `${s.agent} · ` : "") + (first(s.prompt) || "No prompt yet"),
  },
  {
    type: "action",
    label: "Action",
    description: "Call an integration action such as github.create_pull_request.",
    icon: Zap,
    color: "#0e7490",
    defaults: () => ({ action: "", with: {} }),
    summary: (s) => (s.action as string) || "Choose an action",
  },
  {
    type: "script",
    label: "Script",
    description: "Run a shell script in the sandbox (tests, glue, builds).",
    icon: SquareTerminal,
    color: "#334155",
    defaults: () => ({ run: "echo hello" }),
    summary: (s) => first(s.run) || "Empty script",
  },
  {
    type: "wait",
    label: "Wait",
    description: "Durably wait for a duration, a time, or until a condition holds.",
    icon: Clock,
    color: "#b45309",
    defaults: () => ({ duration: "1h" }),
    summary: (s) => {
      if (s.condition && typeof s.condition === "object") {
        const c = s.condition as { action?: string };
        return `Until ${c.action ?? "condition"}${s.max_wait ? ` · max ${s.max_wait}` : ""}`;
      }
      if (s.until) return `Until ${first(s.until, 40)}`;
      return s.duration ? `For ${s.duration}` : "Configure the wait";
    },
  },
  {
    type: "approval",
    label: "Approval",
    description: "Pause until a person approves or rejects in the UI or API.",
    icon: Hand,
    color: "#be185d",
    defaults: () => ({ message: "Approve to continue?" }),
    summary: (s) => first(s.message) || "Approval",
  },
  {
    type: "decide",
    label: "Decide",
    description: "Ask a decision model typed questions and branch on the answers.",
    icon: GitFork,
    color: "#4338ca",
    defaults: () => ({
      state: "",
      questions: { suitable: { type: "boolean", instructions: "Is this suitable for automation?" } },
    }),
    summary: (s) => {
      const q = s.questions && typeof s.questions === "object" ? Object.keys(s.questions as object) : [];
      return q.length ? `Asks ${q.join(", ")}` : "Add questions";
    },
  },
  {
    type: "grade",
    label: "Grade",
    description: "Score an output against a rubric; stop or flag below a threshold.",
    icon: Gauge,
    color: "#15803d",
    defaults: () => ({ subject: "", instructions: "", levels: ["poor", "ok", "good"], threshold: 0.7 }),
    summary: (s) => `Threshold ${s.threshold ?? 0.7}` + (first(s.instructions, 50) ? ` · ${first(s.instructions, 50)}` : ""),
  },
  {
    type: "set",
    label: "Set values",
    description: "Compute values from expressions; they become the step output.",
    icon: SquareFunction,
    color: "#0369a1",
    defaults: () => ({ values: { result: "" } }),
    summary: (s) => {
      const keys = s.values && typeof s.values === "object" ? Object.keys(s.values as object) : [];
      return keys.length ? `Sets ${keys.join(", ")}` : "No values";
    },
  },
  {
    type: "workflow",
    label: "Sub-workflow",
    description: "Run another workflow as a child run, optionally once per item.",
    icon: Puzzle,
    color: "#a16207",
    defaults: () => ({ workflow: "", with: {} }),
    summary: (s) => ((s.workflow as string) || "Choose a workflow") + (s.for_each ? " · for each" : ""),
  },
];

const BY_TYPE = new Map(STEP_TYPES.map((t) => [t.type, t]));

/** Unknown (plugin) step types still render, with a generic look. */
export function stepType(type: string): StepTypeInfo {
  return (
    BY_TYPE.get(type) ?? {
      type,
      label: type,
      description: "Plugin step type",
      icon: CheckCheck,
      color: "#64748b",
      defaults: () => ({}),
      summary: () => type,
    }
  );
}

/** Fields shared by every step, edited in the inspector's common section. */
export const COMMON_FIELDS = ["id", "name", "type", "needs", "if", "retry", "timeout", "on_error"];
