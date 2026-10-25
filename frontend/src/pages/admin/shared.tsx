/** Queries and small widgets shared by the admin console pages. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search, UserRound, Users as UsersIcon } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { ApiError, client, unwrap, type Schemas } from "@/api/client";
import { cn } from "@/lib/cn";
import { Badge, Input, NativeSelect } from "@/components/ui";

// The API always returns these lists; the generated types mark them optional.
export type User = Schemas["UserOut"] & { groups: string[] };
export type Group = Schemas["GroupOut"] & { members: string[] };
export type Role = Schemas["RoleOut"];
export type Binding = Schemas["RoleBindingOut"];
export type Permission = Schemas["PermissionOut"];
export type ApiKey = Schemas["ApiKeyOut"];
export type Namespace = Schemas["NamespaceOut"];

export const adminKeys = {
  users: (q = "") => ["admin", "users", q] as const,
  groups: ["admin", "groups"] as const,
  roles: ["admin", "roles"] as const,
  permissions: ["admin", "permissions"] as const,
  bindings: (ns?: string) => ["admin", "bindings", ns ?? "*"] as const,
  serviceAccounts: ["admin", "service-accounts"] as const,
  oidc: ["admin", "oidc"] as const,
  apiKeys: (all: boolean) => ["admin", "api-keys", all] as const,
  members: (ns: string) => ["admin", "members", ns] as const,
};

export function useUsers(q = "") {
  return useQuery({
    queryKey: adminKeys.users(q),
    queryFn: async () => {
      const page = await unwrap(client.GET("/v1/users", { params: { query: { q: q || undefined, limit: 500 } } }));
      return { ...page, items: page.items.map((u) => ({ ...u, groups: u.groups ?? [] })) as User[] };
    },
  });
}

export function useGroups(enabled = true) {
  return useQuery({
    queryKey: adminKeys.groups,
    queryFn: async () => {
      const page = await unwrap(client.GET("/v1/groups", { params: { query: { limit: 500 } } }));
      return { ...page, items: page.items.map((g) => ({ ...g, members: g.members ?? [] })) as Group[] };
    },
    enabled,
  });
}

export function useRoles(enabled = true) {
  return useQuery({ queryKey: adminKeys.roles, queryFn: () => unwrap(client.GET("/v1/roles")), enabled });
}

export function usePermissionCatalog() {
  return useQuery({ queryKey: adminKeys.permissions, queryFn: () => unwrap(client.GET("/v1/permissions")), staleTime: Infinity });
}

export function useBindings(namespace?: string) {
  return useQuery({
    queryKey: adminKeys.bindings(namespace),
    queryFn: () => unwrap(client.GET("/v1/role-bindings", { params: { query: { namespace } } })),
  });
}

export function useServiceAccounts(enabled = true) {
  return useQuery({ queryKey: adminKeys.serviceAccounts, queryFn: () => unwrap(client.GET("/v1/service-accounts")), enabled });
}

/** Wrap a mutation with toasts and query invalidation. */
export function useAction<TArgs, TResult = unknown>(
  fn: (args: TArgs) => Promise<TResult>,
  {
    success,
    invalidate = [["admin"]],
    onSuccess,
  }: {
    success?: string | ((r: TResult, a: TArgs) => string);
    invalidate?: readonly (readonly unknown[])[];
    onSuccess?: (r: TResult, a: TArgs) => void;
  } = {},
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (r, a) => {
      for (const key of invalidate) void qc.invalidateQueries({ queryKey: key });
      if (success) toast.success(typeof success === "function" ? success(r, a) : success);
      onSuccess?.(r, a);
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
}

/** Map subject ids (users, groups) to display names. */
export function useSubjectNames() {
  const users = useUsers();
  const groups = useGroups();
  const sas = useServiceAccounts();
  return useMemo(() => {
    const m = new Map<string, { name: string; kind: "user" | "group" | "service" }>();
    for (const u of users.data?.items ?? []) m.set(u.id, { name: u.display_name || u.username, kind: "user" });
    for (const s of sas.data ?? []) m.set(s.id, { name: s.display_name || s.name, kind: "service" });
    for (const g of groups.data?.items ?? []) m.set(g.id, { name: g.display_name, kind: "group" });
    return m;
  }, [users.data, groups.data, sas.data]);
}

export function SourceBadge({ source }: { source: string | null | undefined }) {
  if (!source || source === "local" || source === "manual") return <Badge>Local</Badge>;
  if (source === "scim") return <Badge tone="info">SCIM</Badge>;
  if (source === "oidc") return <Badge tone="accent">OIDC</Badge>;
  return <Badge>{source}</Badge>;
}

export function userSource(u: User): string {
  if (u.external_id) return "scim";
  if (u.oidc_subject) return "oidc";
  return "local";
}

export function Avatar({ name, className, group }: { name: string; className?: string; group?: boolean }) {
  const initials = name
    .split(/[\s@._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join("");
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  return (
    <span
      className={cn("flex size-7 shrink-0 items-center justify-center rounded-full text-[10.5px] font-semibold text-white", className)}
      style={{ background: `linear-gradient(135deg, hsl(${hue} 55% 48%), hsl(${(hue + 40) % 360} 60% 42%))` }}
    >
      {group ? <UsersIcon className="size-3.5" /> : initials || <UserRound className="size-3.5" />}
    </span>
  );
}

export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
}) {
  return (
    <div className={cn("relative", className)}>
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
      <Input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="pl-8" />
    </div>
  );
}

/** Choose a user, service account or group as a role-binding subject. */
export function SubjectPicker({
  value,
  onChange,
}: {
  value: { kind: "user" | "group"; id: string } | null;
  onChange: (v: { kind: "user" | "group"; id: string } | null) => void;
}) {
  const [q, setQ] = useState("");
  const users = useUsers();
  const groups = useGroups();
  const sas = useServiceAccounts();
  const options: { kind: "user" | "group"; id: string; name: string; sub: string; group?: boolean }[] = [
    ...(users.data?.items ?? []).map((u) => ({ kind: "user" as const, id: u.id, name: u.display_name || u.username, sub: u.email ?? u.username })),
    ...(sas.data ?? []).map((s) => ({ kind: "user" as const, id: s.id, name: s.display_name || s.name, sub: `service account · ${s.name}` })),
    ...(groups.data?.items ?? []).map((g) => ({
      kind: "group" as const,
      id: g.id,
      name: g.display_name,
      sub: `group · ${g.members.length} members`,
      group: true,
    })),
  ];
  const filtered = options.filter((o) => !q || `${o.name} ${o.sub}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="rounded-lg border border-border">
      <div className="border-b border-border p-1.5">
        <SearchInput value={q} onChange={setQ} placeholder="Find people, service accounts or groups" />
      </div>
      <div className="max-h-56 overflow-y-auto p-1">
        {filtered.map((o) => {
          const on = value?.id === o.id;
          return (
            <button
              type="button"
              key={o.id}
              onClick={() => onChange(on ? null : { kind: o.kind, id: o.id })}
              className={cn("flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left", on ? "bg-accent-soft" : "hover:bg-subtle")}
            >
              <Avatar name={o.name} group={o.group} className="size-6" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium">{o.name}</span>
                <span className="block truncate text-[11px] text-faint">{o.sub}</span>
              </span>
              {on && <Badge tone="accent">Selected</Badge>}
            </button>
          );
        })}
        {!filtered.length && <div className="px-2 py-4 text-center text-xs text-faint">No matches.</div>}
      </div>
    </div>
  );
}

export function RoleSelect({ value, onChange, scope }: { value: string; onChange: (v: string) => void; scope?: "namespace" | "tenant" }) {
  const roles = useRoles();
  return (
    <NativeSelect value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Choose a role…</option>
      {(roles.data ?? [])
        .filter((r) => !(scope === "namespace" && r.name === "tenant-admin"))
        .map((r) => (
          <option key={r.name} value={r.name}>
            {r.name}
            {r.builtin ? "" : " (custom)"}
          </option>
        ))}
    </NativeSelect>
  );
}

export function KV({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 text-[13px]">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 truncate text-right">{children}</span>
    </div>
  );
}
