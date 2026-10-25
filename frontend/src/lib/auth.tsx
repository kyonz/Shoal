import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { ApiError, credentials } from "@/api/client";
import { useMe, usePermissions } from "@/api/hooks";
import { can, type PermissionSummary } from "./perms";
import type { Schemas } from "@/api/client";

interface AuthState {
  me: Schemas["Me"] | undefined;
  perms: PermissionSummary | undefined;
  loading: boolean;
  signedIn: boolean;
  can: (permission: string, namespace?: string) => boolean;
  /** Instance roles: admins manage tenants and accounts, auditors read them. The admin token counts as an admin. */
  instance: { admin: boolean; reader: boolean };
  signInWithToken: (token: string, tenant?: string | null) => Promise<void>;
  signOut: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useMe();
  const hasTenant = !!me.data?.tenant;
  const perms = usePermissions(hasTenant);

  const signInWithToken = useCallback(
    async (token: string, tenant?: string | null) => {
      credentials.token = token.trim();
      credentials.tenant = tenant?.trim() || null;
      qc.clear();
      const result = await me.refetch();
      if (result.error) {
        credentials.token = null;
        credentials.tenant = null;
        throw result.error;
      }
    },
    [qc, me],
  );

  const signOut = useCallback(async () => {
    try {
      await fetch("/v1/auth/logout", { method: "POST", credentials: "include" });
    } catch {
      /* offline */
    }
    credentials.token = null;
    credentials.tenant = null;
    qc.clear();
    window.location.assign("/login");
  }, [qc]);

  const value = useMemo<AuthState>(() => {
    const unauthorized = me.error instanceof ApiError && me.error.status === 401;
    // The system admin token has every permission; tenant principals use their summary.
    const summary: PermissionSummary | undefined = me.data?.is_system_admin
      ? { tenant: ["*"], namespaces: {} }
      : (perms.data as PermissionSummary | undefined);
    return {
      me: me.data,
      perms: summary,
      loading: me.isLoading || (hasTenant && perms.isLoading && !me.data?.is_system_admin),
      signedIn: !!me.data && !unauthorized,
      can: (p, ns) => can(summary, p, ns),
      instance: instanceAccess(me.data),
      signInWithToken,
      signOut,
    };
  }, [me.data, me.error, me.isLoading, perms.data, perms.isLoading, hasTenant, signInWithToken, signOut]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function instanceAccess(me: Schemas["Me"] | undefined): { admin: boolean; reader: boolean } {
  const admin = !!me && (me.is_system_admin || me.instance_role === "instance-admin");
  return { admin, reader: admin || me?.instance_role === "instance-auditor" };
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

/** Shorthand: does the caller hold `permission` (in `namespace`)? */
export function useCan(permission: string, namespace?: string): boolean {
  return useAuth().can(permission, namespace);
}
