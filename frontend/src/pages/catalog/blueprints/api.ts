import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, rawJson, type Resource } from "@/api/client";

export interface BlueprintParam {
  type?: "string" | "number" | "boolean" | "connection" | "secret" | "choice";
  description?: string;
  integration?: string;
  default?: unknown;
  options?: string[];
  required?: boolean;
}

export interface BlueprintSpec {
  title: string;
  summary: string;
  readme?: string;
  version: string;
  icon?: string;
  author?: string;
  tags?: string[];
  categories?: string[];
  status?: "draft" | "published" | "featured" | "promoted" | "deprecated";
  visibility?: string;
  namespaces?: string[];
  params?: Record<string, BlueprintParam>;
  resources: { kind?: string; metadata?: { name?: string; description?: string } }[];
}

export interface AppliedResource {
  kind: string;
  name: string;
  namespace?: string;
  outcome: string;
  version?: number | null;
}

export interface Adoption {
  id: string;
  namespace: string;
  blueprint: string;
  blueprint_version: string;
  prefix?: string | null;
  params: Record<string, unknown>;
  resources: AppliedResource[] | string[];
  created_by?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface AdoptResult {
  adoption?: Adoption;
  resources: AppliedResource[];
  removed?: AppliedResource[] | string[];
  dry_run: boolean;
}

export const spec = (r: Resource) => r.spec as unknown as BlueprintSpec;

export function isFeatured(s: BlueprintSpec) {
  return s.status === "featured" || s.status === "promoted";
}

/** Param errors from a 422 (`details.params`). */
export function paramErrors(e: unknown): Record<string, string> {
  if (!(e instanceof ApiError)) return {};
  const b = e.body as { details?: { params?: Record<string, string> }; errors?: { loc: string[]; msg: string }[] } | undefined;
  const out: Record<string, string> = { ...(b?.details?.params ?? {}) };
  for (const err of b?.errors ?? []) {
    const loc = err.loc.filter((x) => x !== "body");
    const key = loc[0] === "params" ? loc[1] : loc[0];
    if (key && !out[key]) out[key] = err.msg;
  }
  return out;
}

export function useAdoptions(ns: string, blueprint?: string) {
  return useQuery({
    queryKey: ["blueprint-adoptions", ns, blueprint ?? ""],
    queryFn: async () => {
      const q = blueprint ? `?blueprint=${encodeURIComponent(blueprint)}` : "";
      const res = await rawJson<Adoption[] | { items: Adoption[] }>(`/v1/namespaces/${ns}/blueprint-adoptions${q}`);
      return Array.isArray(res) ? res : res.items;
    },
    retry: false,
  });
}

export function useAdopt(ns: string, name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { params: Record<string, unknown>; prefix?: string; version?: string; dry_run: boolean }) =>
      rawJson<AdoptResult>(`/v1/namespaces/${ns}/blueprints/${encodeURIComponent(name)}/adopt`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (r) => {
      if (r.dry_run) return;
      void qc.invalidateQueries({ queryKey: ["blueprint-adoptions", ns] });
      void qc.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}

export function useUpdateAdoption(ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; version?: string; params?: Record<string, unknown>; dry_run: boolean }) =>
      rawJson<AdoptResult>(`/v1/namespaces/${ns}/blueprint-adoptions/${id}`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (r) => {
      if (r.dry_run) return;
      void qc.invalidateQueries({ queryKey: ["blueprint-adoptions", ns] });
      void qc.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}

export function useRemoveAdoption(ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rawJson<void>(`/v1/namespaces/${ns}/blueprint-adoptions/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["blueprint-adoptions", ns] });
      void qc.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}

export const PLURAL_BY_KIND: Record<string, string> = {
  Workflow: "workflows",
  Agent: "agents",
  Skill: "skills",
  SandboxProfile: "sandbox-profiles",
  Connection: "connections",
  Trigger: "triggers",
  Campaign: "campaigns",
  Gateway: "gateways",
  ModelPolicy: "model-policies",
  Eval: "evals",
};

export function resourceRef(r: AppliedResource | string): AppliedResource {
  if (typeof r !== "string") return r;
  const [kind, name] = r.includes("/") ? r.split("/", 2) : ["", r];
  return { kind, name, outcome: "" };
}

// --------------------------------------------------------------------------- save a workflow as a blueprint

export interface FindingChoice {
  enabled?: boolean;
  name?: string;
  description?: string;
  default?: unknown;
}

export interface SaveAsBlueprintBody {
  blueprint?: string;
  target: "new" | "existing";
  version?: string;
  title?: string;
  summary?: string;
  readme?: string;
  tags?: string[];
  categories?: string[];
  namespaces?: string[];
  status?: "draft" | "published";
  include?: { kind: string; name: string }[];
  params: Record<string, FindingChoice>;
  dry_run: boolean;
}

export interface PackagedResource {
  kind: string;
  name: string;
  namespace: string;
  reason: string;
  included: boolean;
  root: boolean;
}

export interface Finding {
  id: string;
  category: "connection" | "secret" | "credential" | "repository" | "organization" | "jira_project" | "model" | "sandbox" | "cloud_identity";
  value: string;
  param: string;
  type: NonNullable<BlueprintParam["type"]>;
  integration?: string | null;
  description: string;
  default?: unknown;
  required: boolean;
  enabled: boolean;
  locked: boolean;
  locations: string[];
}

export interface SaveAsBlueprintResult {
  blueprint: { apiVersion: string; kind: "Blueprint"; metadata: { name: string; labels?: Record<string, string> }; spec: BlueprintSpec };
  resources: PackagedResource[];
  findings: Finding[];
  stripped: string[];
  warnings: string[];
  exists: boolean;
  previous_version?: string | null;
  outcome?: string | null;
  resource_version?: number | null;
  dry_run: boolean;
}

const savePath = (ns: string, workflow: string) =>
  `/v1/namespaces/${ns}/workflows/${encodeURIComponent(workflow)}/save-as-blueprint`;

/** Dry runs as the wizard's choices change; the previous preview stays while the next loads. */
export function usePackagePreview(ns: string, workflow: string, body: Omit<SaveAsBlueprintBody, "dry_run">, enabled: boolean) {
  return useQuery({
    queryKey: ["save-as-blueprint", ns, workflow, body],
    queryFn: () => rawJson<SaveAsBlueprintResult>(savePath(ns, workflow), { method: "POST", body: JSON.stringify({ ...body, dry_run: true }) }),
    enabled,
    retry: false,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

export function useSaveAsBlueprint(ns: string, workflow: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Omit<SaveAsBlueprintBody, "dry_run">) =>
      rawJson<SaveAsBlueprintResult>(savePath(ns, workflow), { method: "POST", body: JSON.stringify({ ...body, dry_run: false }) }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["resources"] });
      void qc.invalidateQueries({ queryKey: ["save-as-blueprint"] });
    },
  });
}
