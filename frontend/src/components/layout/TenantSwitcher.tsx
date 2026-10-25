/** Top-left of the sidebar: which tenant you're in, and the tenants you can switch to. */
import { Building2, Check, ChevronsUpDown, LogOut, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { Badge, Popover, Spinner } from "@/components/ui";
import { useMyTenants, useSwitchTenant, useTenantSummaries, type MyTenant } from "@/pages/instance/api";
import { TenantMark } from "@/pages/instance/shared";
import { ShoalMark } from "./AppShell";

/**
 * The tenants this person can open. Signed-in people get theirs from `/v1/me/tenants` (instance admins and
 * auditors also see the rest, as "Other tenants"). The break-glass admin token lists every tenant.
 */
function useSwitchable() {
  const { me } = useAuth();
  const viaHeader = !!me?.is_system_admin;
  const mine = useMyTenants(!!me && !viaHeader);
  const all = useTenantSummaries(viaHeader);
  if (!viaHeader) return { tenants: mine.data ?? [], loading: mine.isLoading, viaHeader };
  const tenants: MyTenant[] = (all.data ?? []).map((t) => ({ slug: t.slug, name: t.name, status: t.status ?? "active", member: false, current: t.slug === me?.tenant?.slug }));
  return { tenants, loading: all.isLoading, viaHeader };
}

export function TenantSwitcher({ collapsed }: { collapsed: boolean }) {
  const { me, instance } = useAuth();
  const { tenants, loading, viaHeader } = useSwitchable();
  const switchTenant = useSwitchTenant();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  const current = me?.tenant;
  const label = current?.name ?? (instance.reader ? "Instance" : "No tenant");
  const sub = current ? current.slug : instance.admin ? "Instance admin" : instance.reader ? "Instance auditor" : "";
  // API keys belong to one tenant; with nothing else to pick there's nothing to open.
  const switchable = instance.reader || tenants.some((t) => !t.current);

  const go = async (slug: string | null) => {
    setBusy(slug ?? "");
    try {
      await switchTenant(slug, { viaHeader });
    } catch (e) {
      setBusy(null);
      toast.error(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const header = (
    <>
      {current ? <TenantMark name={current.name} className={collapsed ? "size-7" : ""} /> : <ShoalMark className={cn("shrink-0", collapsed ? "h-5" : "h-6")} />}
      {!collapsed && (
        <div className="min-w-0 flex-1 text-left">
          <div className="truncate text-[13px] leading-tight font-semibold tracking-tight">{label}</div>
          <div className="truncate text-[11px] leading-tight text-faint">{sub || "Shoal"}</div>
        </div>
      )}
    </>
  );

  if (!switchable)
    return <div className={cn("flex min-w-0 flex-1 items-center gap-2", collapsed && "justify-center")}>{header}</div>;

  const members = tenants.filter((t) => t.member);
  const others = tenants.filter((t) => !t.member);
  const row = (t: MyTenant) => {
    const suspended = t.status === "suspended";
    const disabled = t.current || (suspended && !instance.reader) || busy !== null;
    return (
      <button
        key={t.slug}
        disabled={disabled}
        onClick={() => void go(t.slug)}
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px]",
          t.current ? "bg-accent-soft/60" : "hover:bg-subtle",
          disabled && !t.current && "cursor-not-allowed opacity-60",
        )}
      >
        <TenantMark name={t.name} className="size-5 text-[9.5px]" />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium">{t.name}</span>
          <span className="block truncate font-mono text-[11px] text-faint">{t.slug}</span>
        </span>
        {suspended && <Badge tone="warn">Suspended</Badge>}
        {busy === t.slug ? <Spinner className="size-3.5" /> : t.current ? <Check className="size-3.5 text-accent" /> : null}
      </button>
    );
  };

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      className="w-72 p-1.5"
      trigger={
        <button
          aria-label="Switch tenant"
          className={cn("flex min-w-0 flex-1 items-center gap-2 rounded-lg py-1 transition-colors hover:bg-subtle", collapsed ? "justify-center px-0" : "-mx-1 px-1")}
        >
          {header}
          {!collapsed && <ChevronsUpDown className="size-3.5 shrink-0 text-faint" />}
        </button>
      }
    >
      <div className="max-h-80 overflow-y-auto">
        {loading && (
          <div className="flex justify-center py-3">
            <Spinner />
          </div>
        )}
        {members.length > 0 && (
          <>
            <GroupLabel>Your tenants</GroupLabel>
            {members.map(row)}
          </>
        )}
        {others.length > 0 && (
          <>
            <GroupLabel>{viaHeader ? "Tenants" : "Other tenants"}</GroupLabel>
            {others.map(row)}
          </>
        )}
        {!loading && !tenants.length && <div className="px-2 py-3 text-center text-xs text-faint">No tenants yet.</div>}
      </div>
      {instance.reader && (
        <div className="mt-1 space-y-0.5 border-t border-border pt-1">
          <button
            onClick={() => {
              setOpen(false);
              navigate("/instance");
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] text-muted hover:bg-subtle hover:text-fg"
          >
            <ShieldCheck className="size-3.5" /> Instance console
          </button>
          {current && (
            <button
              disabled={busy !== null}
              onClick={() => void go(null)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] text-muted hover:bg-subtle hover:text-fg"
            >
              {viaHeader ? <Building2 className="size-3.5" /> : <LogOut className="size-3.5" />} Leave {current.name}
            </button>
          )}
        </div>
      )}
    </Popover>
  );
}

function GroupLabel({ children }: { children: string }) {
  return <div className="px-2 pt-1.5 pb-1 text-[10.5px] font-semibold tracking-wider text-faint uppercase">{children}</div>;
}
