import {
  Activity,
  BookOpen,
  Bot,
  Boxes,
  Cable,
  Cloud,
  FlaskConical,
  GitBranch,
  Globe,
  KeyRound,
  LayoutDashboard,
  Gauge,
  Library,
  Network,
  Plug,
  Rocket,
  Route,
  Server,
  ShieldCheck,
  Sparkles,
  Store,
  UserRoundCheck,
  Workflow,
  Zap,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  label: string;
  /** Path under /ns/:ns/ (namespaced) or an absolute path. */
  to: string;
  icon: LucideIcon;
  namespaced: boolean;
  /** Hidden unless the caller holds this permission (in the current namespace). */
  permission?: string;
  keywords?: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  {
    label: "",
    items: [{ label: "Overview", to: "overview", icon: LayoutDashboard, namespaced: true, keywords: "dashboard home stats" }],
  },
  {
    label: "Build",
    items: [
      { label: "Workflows", to: "workflows", icon: Workflow, namespaced: true, permission: "workflows:read", keywords: "builder graph steps" },
      { label: "Agents", to: "agents", icon: Bot, namespaced: true, permission: "agents:read", keywords: "model prompt" },
      { label: "Skills", to: "skills", icon: Sparkles, namespaced: true, permission: "skills:read", keywords: "SKILL.md" },
      { label: "Skill marketplace", to: "skill-marketplace", icon: Library, namespaced: true, permission: "skills:read", keywords: "skills import git registry agent skills" },
      { label: "Evals", to: "evals", icon: FlaskConical, namespaced: true, permission: "evals:read", keywords: "evaluation graders quality" },
    ],
  },
  {
    label: "Operate",
    items: [
      { label: "Runs", to: "runs", icon: Activity, namespaced: true, permission: "runs:read", keywords: "executions history approvals" },
      { label: "Campaigns", to: "campaigns", icon: Rocket, namespaced: true, permission: "campaigns:read", keywords: "fan out targets" },
      { label: "Triggers", to: "triggers", icon: Zap, namespaced: true, permission: "triggers:read", keywords: "webhook schedule cron" },
    ],
  },
  {
    label: "Connect",
    items: [
      { label: "Integrations", to: "integrations", icon: Plug, namespaced: true, keywords: "github jira catalog sdk" },
      { label: "Connections", to: "connections", icon: Cable, namespaced: true, permission: "connections:read", keywords: "credentials" },
      { label: "Secrets", to: "secrets", icon: KeyRound, namespaced: true, permission: "secrets:read", keywords: "tokens passwords" },
      { label: "Connected accounts", to: "/settings/credentials", icon: UserRoundCheck, namespaced: false, keywords: "my credentials oauth personal token connect account" },
      { label: "Cloud identities", to: "cloud-identities", icon: Cloud, namespaced: true, permission: "cloud-identities:read", keywords: "aws iam role sts gcp azure" },
    ],
  },
  {
    label: "Organisation",
    items: [
      { label: "Blueprints", to: "blueprints", icon: Store, namespaced: true, permission: "blueprints:read", keywords: "marketplace templates adopt" },
      { label: "Sandboxes", to: "sandbox-profiles", icon: Boxes, namespaced: true, permission: "sandbox-profiles:read", keywords: "docker kubernetes e2b space" },
      { label: "Identities", to: "/admin/identities", icon: Network, namespaced: false, permission: "identities:read", keywords: "people github jira attribution" },
      { label: "Admin console", to: "/admin", icon: ShieldCheck, namespaced: false, keywords: "users groups roles oidc scim audit api keys" },
      { label: "Docs", to: "/docs", icon: BookOpen, namespaced: false, keywords: "help guide reference" },
    ],
  },
];

export const ADMIN_NAV: NavItem[] = [
  { label: "Overview", to: "/admin", icon: LayoutDashboard, namespaced: false },
  { label: "Users", to: "/admin/users", icon: Network, namespaced: false, permission: "users:read" },
  { label: "Groups", to: "/admin/groups", icon: Network, namespaced: false, permission: "users:read" },
  { label: "Service accounts", to: "/admin/service-accounts", icon: Bot, namespaced: false, permission: "users:manage" },
  { label: "Namespaces", to: "/admin/namespaces", icon: Boxes, namespaced: false },
  { label: "Roles", to: "/admin/roles", icon: ShieldCheck, namespaced: false, permission: "roles:manage" },
  { label: "Sign-in & SCIM", to: "/admin/auth", icon: KeyRound, namespaced: false, permission: "auth:manage" },
  { label: "API keys", to: "/admin/api-keys", icon: KeyRound, namespaced: false },
  { label: "Identities", to: "/admin/identities", icon: GitBranch, namespaced: false, permission: "identities:read" },
  { label: "Gateways", to: "/admin/gateways", icon: Server, namespaced: false, permission: "gateways:read", keywords: "llm provider litellm openrouter" },
  { label: "Model policies", to: "/admin/model-policies", icon: Route, namespaced: false, permission: "model-policies:read", keywords: "routing fallback" },
  { label: "Skill sources", to: "/admin/skill-sources", icon: Library, namespaced: false, permission: "skill-sources:read", keywords: "marketplace git registry" },
  { label: "Eval platforms", to: "/admin/eval-platforms", icon: Gauge, namespaced: false, permission: "eval-platforms:read", keywords: "braintrust langfuse evals experiments datasets" },
  { label: "Federation", to: "/admin/federation", icon: Globe, namespaced: false, permission: "federation:manage" },
  { label: "Audit log", to: "/admin/audit", icon: Activity, namespaced: false, permission: "audit:read" },
];
