import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";
import { client, rawJson, unwrap, type Resource, type Schemas } from "./client";

// --------------------------------------------------------------------------- resource kinds

export type Plural =
  | "workflows"
  | "agents"
  | "skills"
  | "sandbox-profiles"
  | "connections"
  | "triggers"
  | "campaigns"
  | "gateways"
  | "model-policies"
  | "evals"
  | "identities"
  | "cloud-identities"
  | "blueprints"
  | "skill-sources";

export const TENANT_SCOPED: Plural[] = ["identities", "blueprints", "gateways", "model-policies", "skill-sources"];

export function resourcePath(plural: Plural, ns: string, name?: string) {
  const base = TENANT_SCOPED.includes(plural) ? `/v1/${plural}` : `/v1/namespaces/${ns}/${plural}`;
  return name ? `${base}/${encodeURIComponent(name)}` : base;
}

export const keys = {
  me: ["me"] as const,
  perms: ["me", "permissions"] as const,
  info: ["info"] as const,
  namespaces: ["namespaces"] as const,
  resources: (plural: Plural, ns: string) => ["resources", plural, TENANT_SCOPED.includes(plural) ? "_" : ns] as const,
  resource: (plural: Plural, ns: string, name: string) =>
    ["resources", plural, TENANT_SCOPED.includes(plural) ? "_" : ns, name] as const,
  versions: (plural: Plural, ns: string, name: string) => ["versions", plural, ns, name] as const,
  runs: (ns: string, filters?: object) => ["runs", ns, filters ?? {}] as const,
  run: (id: string) => ["run", id] as const,
  integrations: ["integrations"] as const,
  schemas: ["schemas"] as const,
  stepSchema: ["schemas", "steps"] as const,
};

// --------------------------------------------------------------------------- the caller

export function useInfo() {
  return useQuery({ queryKey: keys.info, queryFn: () => unwrap(client.GET("/v1/info")), staleTime: 300_000 });
}

export function useMe(enabled = true) {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => unwrap(client.GET("/v1/me")),
    retry: false,
    staleTime: 60_000,
    enabled,
  });
}

export function usePermissions(enabled = true) {
  return useQuery({
    queryKey: keys.perms,
    queryFn: () => unwrap(client.GET("/v1/me/permissions")),
    retry: false,
    staleTime: 60_000,
    enabled,
  });
}

export function useNamespaces() {
  return useQuery({ queryKey: keys.namespaces, queryFn: () => unwrap(client.GET("/v1/namespaces")) });
}

// --------------------------------------------------------------------------- catalogues

export interface IntegrationAction {
  name: string;
  description: string;
  mutating: boolean;
  input_schema: JsonSchema;
  output_schema: JsonSchema | null;
}

export interface IntegrationCatalog {
  key: string;
  title: string;
  description: string;
  connection_schema: JsonSchema;
  secret_fields: string[];
  actions: IntegrationAction[];
  resources: { name: string; description: string; filter_schema: JsonSchema }[];
  events: { name: string; description: string; example: unknown }[];
  approvals?: boolean;
  install?: boolean;
  trigger_filter_schema?: JsonSchema | null;
}

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  additionalProperties?: boolean | JsonSchema;
  format?: string;
  minimum?: number;
  maximum?: number;
  pattern?: string;
  discriminator?: { propertyName: string; mapping?: Record<string, string> };
  [key: string]: unknown;
}

export function useIntegrations() {
  return useQuery({
    queryKey: keys.integrations,
    queryFn: () => rawJson<IntegrationCatalog[]>("/v1/integrations"),
    staleTime: 300_000,
  });
}

export type KindSchemas = Record<string, { kind: string; scope: "tenant" | "namespace"; spec_schema: JsonSchema }>;

export function useSchemas() {
  return useQuery({ queryKey: keys.schemas, queryFn: () => rawJson<KindSchemas>("/v1/schemas"), staleTime: Infinity });
}

export function useStepSchema() {
  return useQuery({
    queryKey: keys.stepSchema,
    queryFn: () => rawJson<JsonSchema>("/v1/schemas/steps"),
    staleTime: Infinity,
  });
}

// --------------------------------------------------------------------------- declarative resources

export function useResources(
  plural: Plural,
  ns: string,
  opts: Partial<UseQueryOptions<Resource[]>> = {},
) {
  return useQuery({
    queryKey: keys.resources(plural, ns),
    queryFn: () => rawJson<Resource[]>(resourcePath(plural, ns)),
    ...opts,
  });
}

/** The tenant namespace every other namespace falls back to when a name is not found locally. */
export const SHARED_NAMESPACE = "shared";

export interface VisibleResource extends Resource {
  /** Comes from the shared namespace (not shadowed by one of the same name here). */
  shared: boolean;
}

/**
 * What a reference from `ns` can resolve to: the namespace's own resources, then the
 * shared namespace's (the engine falls back to `shared` for any name not found locally).
 * Shared resources the caller cannot read are left out.
 */
export function useVisibleResources(plural: Plural, ns: string) {
  const local = useResources(plural, ns);
  const withShared = ns !== SHARED_NAMESPACE && !TENANT_SCOPED.includes(plural);
  const shared = useResources(plural, SHARED_NAMESPACE, { enabled: withShared, retry: false });
  const data = visibleResources(local.data ?? [], withShared ? (shared.data ?? []) : []);
  return { data, isLoading: local.isLoading || (withShared && shared.isLoading), error: local.error };
}

/** A namespace's resources followed by the shared ones it does not shadow. */
export function visibleResources(own: Resource[], shared: Resource[]): VisibleResource[] {
  const names = new Set(own.map((r) => r.metadata.name));
  return [
    ...own.map((r) => ({ ...r, shared: false })),
    ...shared.filter((r) => !names.has(r.metadata.name)).map((r) => ({ ...r, shared: true })),
  ];
}

export function useResource(plural: Plural, ns: string, name: string | undefined) {
  return useQuery({
    queryKey: keys.resource(plural, ns, name ?? ""),
    queryFn: () => rawJson<Resource>(resourcePath(plural, ns, name)),
    enabled: !!name,
  });
}

export function useResourceVersions(plural: Plural, ns: string, name: string | undefined) {
  return useQuery({
    queryKey: keys.versions(plural, ns, name ?? ""),
    queryFn: () => rawJson<Schemas["ResourceVersionOut"][]>(`${resourcePath(plural, ns, name)}/versions`),
    enabled: !!name && !TENANT_SCOPED.includes(plural),
  });
}

export interface ResourceBody {
  metadata?: { description?: string | null; labels?: Record<string, string>; annotations?: Record<string, string> };
  spec: Record<string, unknown>;
}

export function usePutResource(plural: Plural, ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: ResourceBody }) =>
      rawJson<Resource>(resourcePath(plural, ns, name), { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (res, { name }) => {
      qc.setQueryData(keys.resource(plural, ns, name), res);
      void qc.invalidateQueries({ queryKey: keys.resources(plural, ns) });
      void qc.invalidateQueries({ queryKey: keys.versions(plural, ns, name) });
    },
  });
}

export function useDeleteResource(plural: Plural, ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => rawJson<void>(resourcePath(plural, ns, name), { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.resources(plural, ns) }),
  });
}

// --------------------------------------------------------------------------- runs

export function useRuns(
  ns: string,
  filters: { workflow?: string; status?: string; campaign_id?: string; trigger?: string } = {},
  refetchInterval?: number,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryKey: keys.runs(ns, filters),
    queryFn: () =>
      unwrap(
        client.GET("/v1/namespaces/{namespace}/runs", {
          params: { path: { namespace: ns }, query: { ...filters, limit: 100 } },
        }),
      ),
    refetchInterval,
  });
}

export function useRun(id: string | undefined) {
  return useQuery({
    queryKey: keys.run(id ?? ""),
    queryFn: () => unwrap(client.GET("/v1/runs/{run_id}", { params: { path: { run_id: id! } } })),
    enabled: !!id,
  });
}
