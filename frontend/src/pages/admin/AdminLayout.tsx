import { ShieldCheck } from "lucide-react";
import { NavLink, Outlet } from "react-router";
import { ADMIN_NAV } from "@/components/layout/nav";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";

export function AdminLayout() {
  const { can, me } = useAuth();
  const items = ADMIN_NAV.filter((i) => !i.permission || can(i.permission, i.permissionNamespace));
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-[calc(100vh-3rem)] w-52 shrink-0 flex-col border-r border-border bg-panel/50 px-2 py-4 md:flex">
        <div className="mb-3 flex items-center gap-2 px-2">
          <ShieldCheck className="size-4 text-accent" />
          <div className="min-w-0">
            <div className="text-[13px] font-semibold">Admin console</div>
            <div className="truncate text-[11px] text-faint">{me?.tenant?.name ?? "System"}</div>
          </div>
        </div>
        <nav className="space-y-0.5">
          {items.map((i) => (
            <NavLink
              key={i.to}
              to={i.to}
              end={i.to === "/admin"}
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
