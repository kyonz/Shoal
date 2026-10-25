import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { rawJson } from "@/api/client";

export interface SourceSummary {
  name: string;
  source: string;
  title: string;
  description?: string | null;
}

export interface InstalledRef {
  name: string;
  version?: string | null;
  ref?: string | null;
}

export interface CatalogSkill {
  id: string;
  name: string;
  description: string;
  version?: string | null;
  tags: string[];
  homepage?: string | null;
  installed: InstalledRef[];
}

export interface CatalogPage {
  source: string;
  items: CatalogSkill[];
  next_cursor?: string | null;
}

export interface SkillDetail {
  id: string;
  name: string;
  description: string;
  version: string;
  ref?: string | null;
  instructions: string;
  files: Record<string, string>;
  metadata: Record<string, unknown>;
  skipped: string[];
  homepage?: string | null;
  source: string;
  installed: InstalledRef[];
}

export interface Change {
  op: "add" | "remove" | "replace" | "reorder";
  path: string;
  from?: unknown;
  to?: unknown;
}

export interface ImportResult {
  source: string;
  skill_id: string;
  name: string;
  namespace: string;
  version: string;
  ref?: string | null;
  previous_version?: string | null;
  outcome: "created" | "updated" | "unchanged";
  changes: Change[];
  skipped_files: string[];
  resource_version?: number | null;
  dry_run: boolean;
}

export interface ImportedSkill {
  name: string;
  namespace: string;
  source: string | null;
  skill_id: string | null;
  version: string | null;
  ref?: string | null;
  resource_version: number;
  updated_at: string;
  updated_by?: string | null;
  latest_version?: string | null;
  latest_ref?: string | null;
  update_available?: boolean | null;
  check_error?: string | null;
}

const enc = encodeURIComponent;
/** Skill ids may contain "/" (git paths); keep the slashes, encode the segments. */
const encPath = (id: string) => id.split("/").map(enc).join("/");

export function useVisibleSources(ns: string) {
  return useQuery({
    queryKey: ["skill-sources", ns],
    queryFn: () => rawJson<SourceSummary[]>(`/v1/namespaces/${ns}/skill-sources`),
    retry: false,
  });
}

export function useSourceSkills(ns: string, source: string | undefined, q: string) {
  return useQuery({
    queryKey: ["skill-source-skills", ns, source ?? "", q],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "200" });
      if (q) params.set("q", q);
      return rawJson<CatalogPage>(`/v1/namespaces/${ns}/skill-sources/${enc(source!)}/skills?${params}`);
    },
    enabled: !!source,
    retry: false,
    staleTime: 60_000,
  });
}

export function useSourceSkill(ns: string, source: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["skill-source-skill", ns, source ?? "", id ?? ""],
    queryFn: () => rawJson<SkillDetail>(`/v1/namespaces/${ns}/skill-sources/${enc(source!)}/skills/${encPath(id!)}`),
    enabled: !!source && !!id,
    retry: false,
  });
}

export function useImports(ns: string, check: boolean) {
  return useQuery({
    queryKey: ["skill-imports", ns, check],
    queryFn: () => rawJson<ImportedSkill[]>(`/v1/namespaces/${ns}/skill-imports${check ? "?check=true" : ""}`),
    retry: false,
  });
}

function useInvalidate(ns: string) {
  const qc = useQueryClient();
  return (r: ImportResult) => {
    if (r.dry_run) return;
    void qc.invalidateQueries({ queryKey: ["skill-imports", ns] });
    void qc.invalidateQueries({ queryKey: ["skill-source-skills", ns] });
    void qc.invalidateQueries({ queryKey: ["skill-source-skill", ns] });
    void qc.invalidateQueries({ queryKey: ["resources", "skills", ns] });
    void qc.invalidateQueries({ queryKey: ["versions", "skills", ns] });
  };
}

export function useImportSkill(ns: string) {
  const onSuccess = useInvalidate(ns);
  return useMutation({
    mutationFn: (body: { source: string; skill: string; name?: string; version?: string; dry_run: boolean }) =>
      rawJson<ImportResult>(`/v1/namespaces/${ns}/skill-imports`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess,
  });
}

export function useUpgradeSkill(ns: string) {
  const onSuccess = useInvalidate(ns);
  return useMutation({
    mutationFn: ({ name, ...body }: { name: string; version?: string; dry_run: boolean }) =>
      rawJson<ImportResult>(`/v1/namespaces/${ns}/skill-imports/${enc(name)}/upgrade`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess,
  });
}

export function useRollbackSkill(ns: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, version }: { name: string; version: number }) =>
      rawJson<unknown>(`/v1/namespaces/${ns}/skills/${enc(name)}/rollback`, { method: "POST", body: JSON.stringify({ version }) }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["skill-imports", ns] });
      void qc.invalidateQueries({ queryKey: ["resources", "skills", ns] });
      void qc.invalidateQueries({ queryKey: ["versions", "skills", ns] });
    },
  });
}

/** "shoal.dev~1skill-version" -> "shoal.dev/skill-version" (JSON pointer unescaping). */
export function readablePath(path: string) {
  return path.replace(/~1/g, "/").replace(/~0/g, "~");
}
