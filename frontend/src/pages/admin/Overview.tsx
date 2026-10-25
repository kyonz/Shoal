import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Boxes, KeyRound, LogIn, ScrollText, ShieldCheck, Users, UsersRound } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { client, unwrap } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { Badge, Card, CardHeader, Page, PageHeader, Skeleton } from "@/components/ui";
import { adminKeys, useGroups, useRoles, useUsers } from "./shared";
import { Actor } from "@/lib/actor";

export default function AdminOverview() {
  const { can, me } = useAuth();
  const users = useUsers();
  const groups = useGroups(can("users:read"));
  const roles = useRoles(can("roles:manage"));
  const namespaces = useNamespaces();
  const oidc = useQuery({ queryKey: adminKeys.oidc, queryFn: () => unwrap(client.GET("/v1/oidc-providers")), enabled: can("auth:manage") });
  const keys = useQuery({
    queryKey: adminKeys.apiKeys(can("api-keys:manage")),
    queryFn: () => unwrap(client.GET("/v1/api-keys", { params: { query: { all: can("api-keys:manage") } } })),
  });
  const audit = useQuery({
    queryKey: ["admin", "audit", "recent"],
    queryFn: () => unwrap(client.GET("/v1/audit", { params: { query: { limit: 8 } as never } })),
    enabled: can("audit:read"),
  });
  const recent = (Array.isArray(audit.data) ? audit.data : ((audit.data as { items?: unknown[] } | undefined)?.items ?? [])) as {
    id: number;
    actor: string | null;
    action: string;
    target: string | null;
    created_at: string;
  }[];

  const activeUsers = users.data?.items.filter((u) => u.active).length;
  const activeKeys = keys.data?.filter((k) => !k.revoked_at).length;
  const customRoles = roles.data?.filter((r) => !r.builtin).length;
  const oidcEnabled = oidc.data?.filter((p) => p.enabled).length;

  return (
    <Page>
      <PageHeader
        title="Admin console"
        description={`Manage who can do what in ${me?.tenant?.name ?? "this tenant"}.`}
        icon={<ShieldCheck className="size-4" />}
      />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {can("users:read") && (
          <Tile
            to="/admin/users"
            icon={<Users className="size-4" />}
            label="Users"
            value={users.data?.total}
            sub={activeUsers != null ? `${activeUsers} active` : undefined}
          />
        )}
        {can("users:read") && (
          <Tile to="/admin/groups" icon={<UsersRound className="size-4" />} label="Groups" value={groups.data?.total ?? groups.data?.items.length} />
        )}
        <Tile to="/admin/namespaces" icon={<Boxes className="size-4" />} label="Namespaces" value={namespaces.data?.length} />
        {can("roles:manage") && (
          <Tile
            to="/admin/roles"
            icon={<ShieldCheck className="size-4" />}
            label="Roles"
            value={roles.data?.length}
            sub={customRoles != null ? `${customRoles} custom` : undefined}
          />
        )}
        <Tile to="/admin/api-keys" icon={<KeyRound className="size-4" />} label="API keys" value={activeKeys} sub="active" />
        {can("auth:manage") && (
          <Tile
            to="/admin/auth"
            icon={<LogIn className="size-4" />}
            label="Single sign-on"
            value={oidc.data ? (oidcEnabled ? `${oidcEnabled} provider${oidcEnabled > 1 ? "s" : ""}` : "Not set up") : undefined}
            sub={oidc.data && !oidcEnabled ? "Add an OIDC provider" : "OIDC + SCIM"}
            warn={!!oidc.data && !oidcEnabled}
          />
        )}
      </div>
      {can("audit:read") && (
        <Card className="mt-5">
          <CardHeader
            title="Recent activity"
            actions={
              <Link to="/admin/audit" className="flex items-center gap-1 text-xs font-medium text-accent hover:underline">
                Audit log <ArrowRight className="size-3" />
              </Link>
            }
          />
          {audit.isLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-6" />
              <Skeleton className="h-6" />
            </div>
          ) : (
            <div className="divide-y divide-border">
              {recent.map((e) => (
                <div key={e.id} className="flex items-center gap-3 px-4 py-2 text-[13px]">
                  <ScrollText className="size-3.5 shrink-0 text-faint" />
                  <Actor actor={e.actor} className="max-w-48" />
                  <Badge className="font-mono">{e.action}</Badge>
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted">{e.target}</span>
                  <span className="text-xs text-faint">{relTime(e.created_at)}</span>
                </div>
              ))}
              {!recent.length && <div className="px-4 py-6 text-center text-xs text-faint">No activity yet.</div>}
            </div>
          )}
        </Card>
      )}
    </Page>
  );
}

function Tile({
  to,
  icon,
  label,
  value,
  sub,
  warn,
}: {
  to: string;
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  warn?: boolean;
}) {
  return (
    <Link to={to} className="group block h-full">
      <Card className="h-full p-4 transition-colors group-hover:border-border-strong">
        <div className="flex items-center justify-between text-xs text-muted">
          <span className="flex items-center gap-2">
            <span className="text-accent">{icon}</span>
            {label}
          </span>
          <ArrowRight className="size-3.5 text-faint opacity-0 transition-opacity group-hover:opacity-100" />
        </div>
        <div className={`mt-1.5 text-2xl font-semibold tracking-tight tabular-nums ${warn ? "text-warn" : ""}`}>
          {value ?? <Skeleton className="h-8 w-16" />}
        </div>
        <div className="mt-0.5 text-xs text-faint">{sub ?? "\u00a0"}</div>
      </Card>
    </Link>
  );
}
