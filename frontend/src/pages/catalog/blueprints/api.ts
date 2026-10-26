import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  resources: {
    kind?: string;
    metadata?: { name?: string; description?: string; labels?: Record<string, string>; annotations?: Record<string, string> };
    spec?: Record<string, unknown>;
  }[];
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

export interface RemoveResult {
  removed: { kind: string; name: string }[];
  kept?: { kind: string; name: string; reason: string }[];
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
    mutationFn: (id: string) => rawJson<RemoveResult | undefined>(`/v1/namespaces/${ns}/blueprint-adoptions/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["blueprint-adoptions", ns] });
      void qc.invalidateQueries({ queryKey: ["resources"] });
    },
  });
}

export interface DefaultBlueprint {
  name: string;
  version: string;
  outcome: "created" | "updated" | "unchanged" | "kept";
}

/** Add the blueprints that ship with Shoal (new tenants already have them). */
export function useInstallDefaults() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => rawJson<DefaultBlueprint[]>("/v1/blueprints/defaults", { method: "POST", body: JSON.stringify({}) }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["resources"] }),
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
  TargetList: "target-lists",
  Gateway: "gateways",
  ModelPolicy: "model-policies",
  Eval: "evals",
  CloudIdentity: "cloud-identities",
};

export function resourceRef(r: AppliedResource | string): AppliedResource {
  if (typeof r !== "string") return r;
  const [kind, name] = r.includes("/") ? r.split("/", 2) : ["", r];
  return { kind, name, outcome: "" };
}
