import { Activity, Building2, MessageSquareText, ShieldCheck, UsersRound, Workflow } from "lucide-react";
import { NavLink, Outlet } from "react-router";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { EmptyState } from "@/components/ui";

export const INSTANCE_NAV = [
  { label: "Tenants", to: "/instance", icon: Building2, keywords: "organisations workspaces suspend" },
  { label: "Accounts", to: "/instance/accounts", icon: UsersRound, keywords: "people instance admins auditors" },
  { label: "Orchestrator", to: "/instance/orchestrator", icon: Workflow, keywords: "temporal scheduler task queue workers" },
  { label: "Assistant", to: "/instance/assistant", icon: MessageSquareText, keywords: "chat assistant model gateway claude code" },
  { label: "Instance audit", to: "/instance/audit", icon: Activity, keywords: "log events" },
];

/** Instance console: /instance/*, for instance admins and (read-only) auditors. */
export function InstanceLayout() {
  const { instance } = useAuth();
  if (!instance.reader)
    return (
      <EmptyState
        className="h-full"
        icon={<ShieldCheck className="size-5" />}
        title="Instance admins only"
        description="The instance console manages every tenant on this Shoal deployment. Ask an instance admin for the instance-admin or instance-auditor role."
      />
    );
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-[calc(100vh-3rem)] w-52 shrink-0 flex-col border-r border-border bg-panel/50 px-2 py-4 md:flex">
        <div className="mb-3 flex items-center gap-2 px-2">
          <ShieldCheck className="size-4 text-accent" />
          <div className="min-w-0">
            <div className="text-[13px] font-semibold">Instance console</div>
            <div className="truncate text-[11px] text-faint">{instance.admin ? "All tenants" : "Read-only (auditor)"}</div>
          </div>
        </div>
        <nav className="space-y-0.5">
          {INSTANCE_NAV.map((i) => (
            <NavLink
              key={i.to}
              to={i.to}
              end={i.to === "/instance"}
              className={({ isActive }) =>
                cn(
                  "flex h-8 items-center gap-2.5 rounded-lg px-2 text-[13px] font-medium transition-colors",
                  isActive ? "bg-accent-soft text-accent" : "text-muted hover:bg-subtle hover:text-fg",
                )
              }
            >
              <i.icon className="size-4 shrink-0" />
              <span className="truncate">{i.label}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
