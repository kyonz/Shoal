/** The instance: tenants you can switch to, and tenant and account management for instance admins. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { client, credentials, rawJson, unwrap, type Schemas } from "@/api/client";
import { rememberNamespace } from "@/lib/ns";

export type MyTenant = Schemas["MyTenant"];
export type TenantSummary = Schemas["TenantSummary"];
export type TenantMember = Schemas["TenantMemberOut"];
export type Account = Schemas["AccountOut"];
export type AccountDetail = Schemas["AccountDetail"];
export type AuditEntry = Schemas["AuditOut"];
export type OrchestratorStatus = Schemas["OrchestratorStatus"];
export type OrchestratorSetting = Schemas["OrchestratorSetting"];
export type TemporalSettings = Schemas["TemporalSettings"];
export type TemporalCheck = Schemas["TemporalCheck"];

export const instanceKeys = {
  myTenants: ["instance", "my-tenants"] as const,
  tenants: ["instance", "tenants"] as const,
  members: (slug: string) => ["instance", "members", slug] as const,
  accounts: (q: string) => ["instance", "accounts", q] as const,
  account: (id: string) => ["instance", "account", id] as const,
  audit: (action: string) => ["instance", "audit", action] as const,
  orchestrator: ["instance", "orchestrator"] as const,
};

export const INSTANCE_ROLE_LABEL: Record<string, string> = { "instance-admin": "Instance admin", "instance-auditor": "Instance auditor" };

/** Tenant roles offered when adding someone (built in to every tenant; custom roles work too). */
export const TENANT_ROLES = ["tenant-admin", "editor", "operator", "viewer"];

export function useMyTenants(enabled = true) {
  return useQuery({ queryKey: instanceKeys.myTenants, queryFn: () => unwrap(client.GET("/v1/me/tenants")), enabled, staleTime: 60_000 });
}

export function useTenantSummaries(enabled = true) {
  return useQuery({ queryKey: instanceKeys.tenants, queryFn: () => unwrap(client.GET("/v1/instance/tenants")), enabled });
}

export function useTenantMembers(slug: string) {
  return useQuery({
    queryKey: instanceKeys.members(slug),
    queryFn: () => unwrap(client.GET("/v1/tenants/{slug}/members", { params: { path: { slug } } })),
  });
}

export function useAccounts(q: string) {
  return useQuery({
    queryKey: instanceKeys.accounts(q),
    queryFn: () => unwrap(client.GET("/v1/instance/accounts", { params: { query: { q: q || undefined } } })),
  });
}

export function useOrchestrator() {
  return useQuery({ queryKey: instanceKeys.orchestrator, queryFn: () => unwrap(client.GET("/v1/instance/orchestrator")) });
}

export function useAccount(id: string | null) {
  return useQuery({
    queryKey: instanceKeys.account(id ?? ""),
    queryFn: () => unwrap(client.GET("/v1/instance/accounts/{account_id}", { params: { path: { account_id: id! } } })),
    enabled: !!id,
  });
}

/**
 * Point this browser at another tenant (or, for instance admins, at none) and start again from the top.
 *
 * People switch through `POST /v1/me/tenant`, which reissues their session (cookie, and the returned
 * token when the session was pasted in). The break-glass admin token has no session: it picks a tenant
 * with the X-Shoal-Tenant header instead.
 */
export function useSwitchTenant() {
  const qc = useQueryClient();
  return async (slug: string | null, { viaHeader = false, to = "/" }: { viaHeader?: boolean; to?: string } = {}) => {
    if (viaHeader) credentials.tenant = slug;
    else {
      const out = await rawJson<Schemas["SessionOut"]>("/v1/me/tenant", { method: "POST", body: JSON.stringify({ tenant: slug }) });
      if (credentials.token?.startsWith("shoals_")) credentials.token = out.session;
      credentials.tenant = null;
    }
    // Namespaces belong to a tenant: start from the new tenant's default one.
    rememberNamespace("default");
    qc.clear();
    window.location.assign(slug ? to : "/instance");
  };
}
