/** Per-kind presentation for the generic resource pages. */
import { Bot, Boxes, Cloud, FlaskConical, Gauge, Library, ListChecks, Puzzle, Route, Server, Sparkles, Store, type LucideIcon } from "lucide-react";
import type { Plural } from "@/api/hooks";
import { nsPath } from "@/lib/ns";

export interface KindConfig {
  plural: Plural;
  kind: string;
  label: string;
  singular: string;
  icon: LucideIcon;
  /** One line explaining the concept (empty states, headers). */
  blurb: string;
  /** Spec fields rendered first. */
  order?: string[];
  /** Spec fields to omit from the generic form (handled by a custom section). */
  hidden?: string[];
  /** Starter spec for "Create". */
  template: Record<string, unknown>;
  /** Short subtitle per row. */
  summary?: (spec: Record<string, unknown>) => string | undefined;
  /** Tenant-wide and managed in the admin console with `<plural>:manage`. */
  admin?: boolean;
}

export const KINDS: Record<string, KindConfig> = {
  agents: {
    plural: "agents",
    kind: "Agent",
    label: "Agents",
    singular: "agent",
    icon: Bot,
    blurb: "A model, instructions, skills and tools that run inside a sandbox for a workflow's agent steps.",
    order: ["description", "model", "provider", "runtime", "instructions", "skills", "tools", "subagents", "sandbox", "credentials", "limits"],
    template: {
      description: "",
      provider: "anthropic",
      model: "claude-opus-5-5",
      instructions: "You are a careful senior engineer.",
      tools: ["shell", "read_file", "write_file", "list_files"],
    },
    summary: (s) => [s.model, s.runtime && s.runtime !== "tool-loop" ? s.runtime : null].filter(Boolean).join(" · ") || undefined,
  },
  skills: {
    plural: "skills",
    kind: "Skill",
    label: "Skills",
    singular: "skill",
    icon: Sparkles,
    blurb: "Reusable instructions and files (SKILL.md) that agents load on demand.",
    order: ["description", "instructions", "files"],
    hidden: ["files"],
    template: { description: "What this skill helps with.", instructions: "# Skill\n\n1. …" },
  },
  "sandbox-profiles": {
    plural: "sandbox-profiles",
    kind: "SandboxProfile",
    label: "Sandboxes",
    singular: "sandbox profile",
    icon: Boxes,
    blurb: "Where agent and script steps run: backend, image, resources, network policy and lifetime.",
    order: ["description", "backend", "image", "cpu", "memory_mb", "disk_mb", "scope", "ttl", "network", "suspend_on_wait", "config"],
    hidden: ["config"],
    template: { backend: "local", scope: "run" },
    summary: (s) => [s.backend, s.image].filter(Boolean).join(" · ") || undefined,
  },
  "cloud-identities": {
    plural: "cloud-identities",
    kind: "CloudIdentity",
    label: "Cloud identities",
    singular: "cloud identity",
    icon: Cloud,
    blurb: "A cloud role (AWS IAM) your namespace brings, and which workflows and agents may use it. Steps get short-lived credentials.",
    order: ["description", "provider", "trust", "aws", "allow", "config"],
    template: {
      provider: "aws",
      trust: "web_identity",
      aws: { role_arn: "arn:aws:iam::123456789012:role/shoal-deploy", region: "us-east-1", duration: "1h" },
      allow: { workflows: [], agents: [] },
    },
    summary: (s) => {
      const aws = s.aws as { role_arn?: string } | undefined;
      return [s.provider, aws?.role_arn, s.trust].filter(Boolean).join(" · ") || undefined;
    },
  },
  "target-lists": {
    plural: "target-lists",
    kind: "TargetList",
    label: "Target lists",
    singular: "target list",
    icon: ListChecks,
    blurb: "A named set of targets, such as the repositories a team owns, that campaigns run over and trigger filters check against.",
    order: ["description", "key"],
    hidden: ["items", "connection", "resource", "filter"],
    template: { key: "{{ target.full_name }}", items: [{ full_name: "acme/api" }, { full_name: "acme/web" }] },
    summary: (s) => {
      const items = Array.isArray(s.items) ? `${s.items.length} listed` : null;
      const lookup = s.connection ? `${items ? "looked up in" : "all"} ${s.resource} from ${s.connection}` : null;
      return [items, lookup].filter(Boolean).join(" · ") || undefined;
    },
  },
  gateways: {
    plural: "gateways",
    kind: "Gateway",
    label: "Gateways",
    singular: "gateway",
    icon: Server,
    blurb: "How model requests reach a provider: direct API, LiteLLM, Portkey, OpenRouter, Bedrock and more. Shared by the whole organisation; keys come from secrets in the shared namespace.",
    order: ["description", "type", "base_url", "api_key_secret", "config"],
    hidden: ["headers", "secret_headers"],
    template: { type: "anthropic" },
    admin: true,
    summary: (s) => [s.type, s.base_url, s.api_key_secret ? `key from ${s.api_key_secret}` : null].filter(Boolean).join(" · ") || undefined,
  },
  "model-policies": {
    plural: "model-policies",
    kind: "ModelPolicy",
    label: "Model policies",
    singular: "model policy",
    icon: Route,
    blurb: "Rules that choose which model and gateway run, with allow-lists, fallbacks and A/B splits. They apply across the organisation.",
    order: ["description", "rules", "allow_models", "deny_models"],
    template: { rules: [{ name: "default", candidates: [{ model: "claude-opus-5-5" }] }] },
    admin: true,
    summary: (s) => (Array.isArray(s.rules) ? `${s.rules.length} rule${s.rules.length === 1 ? "" : "s"}` : undefined),
  },
  evals: {
    plural: "evals",
    kind: "Eval",
    label: "Evals",
    singular: "eval",
    icon: FlaskConical,
    blurb: "Datasets and graders that measure a workflow's quality so changes are scored, not assumed.",
    order: ["description", "workflow", "threshold", "concurrency", "cases", "graders"],
    hidden: ["cases", "graders", "sinks", "dataset"],
    template: {
      workflow: "",
      threshold: 0.8,
      cases: [{ name: "case-1", inputs: {}, expect: {} }],
      graders: [{ name: "succeeded", type: "expression", check: "{{ run.status == 'succeeded' }}" }],
    },
    summary: (s) =>
      [s.workflow, Array.isArray(s.cases) ? `${s.cases.length} cases` : null].filter(Boolean).join(" · ") || undefined,
  },
  "eval-platforms": {
    plural: "eval-platforms",
    kind: "EvalPlatform",
    label: "Eval platforms",
    singular: "eval platform",
    icon: Gauge,
    blurb: "Braintrust, Langfuse and other eval platforms, connected once for the whole organisation. Evals pick one by name to export results and import datasets; keys come from secrets in the shared namespace.",
    order: ["description"],
    hidden: ["sink", "config"],
    template: { sink: "braintrust", config: { project: "", api_key_secret: "braintrust-api-key" } },
    admin: true,
    summary: (s) => {
      const c = (s.config as { project?: string } | undefined) ?? {};
      return [s.sink, c.project ? `project ${c.project}` : null].filter(Boolean).join(" · ") || undefined;
    },
  },
  blueprints: {
    plural: "blueprints",
    kind: "Blueprint",
    label: "Blueprints",
    singular: "blueprint",
    icon: Store,
    blurb: "Packaged workflows other teams publish so you can adopt and wire them to your own systems.",
    order: ["title", "summary", "version", "status", "author", "icon", "tags", "categories", "namespaces", "readme"],
    hidden: ["params", "resources"],
    template: {
      title: "My blueprint",
      summary: "What it does in one line.",
      version: "0.1.0",
      status: "draft",
      params: {},
      resources: [{ apiVersion: "shoal.dev/v1", kind: "Workflow", metadata: { name: "{{ prefix }}example" }, spec: { steps: [{ id: "hello", type: "set", values: { ok: true } }] } }],
    },
  },
  "skill-sources": {
    plural: "skill-sources",
    kind: "SkillSource",
    label: "Skill sources",
    singular: "skill source",
    icon: Library,
    blurb: "Marketplaces that teams import skills from: git repositories in the Agent Skills layout, skill registries or plugins.",
    order: ["title", "source", "description", "config", "secrets", "namespaces", "enabled"],
    admin: true,
    template: { source: "git", title: "Public skills", config: { url: "https://github.com/anthropics/skills", ref: "main", path: "skills" } },
    summary: (s) => [s.source, (s.config as { url?: string } | undefined)?.url].filter(Boolean).join(" · ") || undefined,
  },
  "custom-integrations": {
    plural: "custom-integrations",
    kind: "CustomIntegration",
    label: "Custom integrations",
    singular: "custom integration",
    icon: Puzzle,
    blurb: "Integrations your organisation built: a remote service speaking the Shoal integration protocol, or an installed Python package. Shoal pulls their settings so teams connect them like a built-in.",
    order: ["title", "source", "url", "token_secret", "package", "description", "connection_scopes", "namespaces", "enabled", "timeout_seconds", "allow_insecure"],
    admin: true,
    template: { source: "remote", url: "https://my-integration.example.com", token_secret: "my-integration-token" },
    summary: (s) => [s.source, s.url ?? s.package, Array.isArray(s.connection_scopes) && s.connection_scopes.length === 1 ? `${s.connection_scopes[0]} only` : null].filter(Boolean).join(" · ") || undefined,
  },
};

/** Where a resource's pages live: the admin console for tenant-wide kinds, else the namespace. */
export function kindPath(plural: string, ns: string, name?: string) {
  const parts = name ? [plural, name] : [plural];
  return KINDS[plural]?.admin ? `/admin/${parts.map(encodeURIComponent).join("/")}` : nsPath(ns, ...parts);
}

/** The permissions that edit and delete a kind. */
export function kindPermissions(plural: string) {
  if (plural === "blueprints") return { write: "blueprints:publish", delete: "blueprints:publish", tenant: true };
  if (KINDS[plural]?.admin) return { write: `${plural}:manage`, delete: `${plural}:manage`, tenant: true };
  return { write: `${plural}:write`, delete: `${plural}:delete`, tenant: false };
}

// Mirrors the engine's check: header and config names that carry credentials must come from secrets.
const CREDENTIAL_NAME = /(^|[-_])(authorization|auth|api[-_]?key|key|token|secret|password|passwd|cookie|credentials?)($|[-_])/i;

export const looksLikeCredential = (name: string) => CREDENTIAL_NAME.test(name);

export const NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

export const CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"];
