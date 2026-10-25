/** A workflow step as authored (the YAML/JSON document, not a pydantic model). */
export interface StepDoc {
  id: string;
  type: string;
  name?: string;
  needs?: string[];
  if?: string;
  [key: string]: unknown;
}

export interface ParamDoc {
  type?: "string" | "integer" | "number" | "boolean" | "object" | "array" | "any";
  description?: string;
  default?: unknown;
  required?: boolean;
  enum?: unknown[];
}

export interface WorkflowSpecDoc {
  description?: string;
  inputs?: Record<string, ParamDoc>;
  outputs?: Record<string, unknown>;
  connections?: Record<string, string>;
  defaults?: { agent?: string; sandbox?: string };
  timeout?: string;
  steps: StepDoc[];
  [key: string]: unknown;
}

export type Position = { x: number; y: number };
export type Layout = Record<string, Position>;

export const LAYOUT_ANNOTATION = "ui.shoal.dev/layout";
export const STEP_ID = /^[a-zA-Z_][a-zA-Z0-9_]{0,63}$/;
