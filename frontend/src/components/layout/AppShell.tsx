import { Check, ChevronsUpDown, CloudOff, Lock, RefreshCw, ShieldQuestion, X, LogOut, Monitor, Moon, PanelLeftClose, PanelLeftOpen, Plus, Search, Sun, UserRound } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { nsPath, rememberNamespace, useNs, useSwitchNamespace } from "@/lib/ns";
import { useTheme } from "@/lib/theme";
import { Button, Kbd, Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger, Popover, Tooltip } from "@/components/ui";
import { CommandPalette, useCommandPalette } from "./CommandPalette";
import { NAV } from "./nav";
import { FEDERATION_DISCONNECTED, FEDERATION_READ_ONLY, federationMessage, useFederatedLink, useFederationState } from "@/lib/federation";
import shoalIcon from "@/assets/brand/shoal-icon.svg";
import shoalLogo from "@/assets/brand/shoal-logo.svg";
import shoalLogoDark from "@/assets/brand/shoal-logo-dark.svg";

export function ShoalMark({ className }: { className?: string }) {
  return <img src={shoalIcon} alt="" aria-hidden className={cn("w-auto", className)} draggable={false} />;
}

/** The full wordmark, swapped for the light-on-dark variant in dark mode. */
export function ShoalLogo({ className }: { className?: string }) {
  return (
    <>
      <img src={shoalLogo} alt="Shoal" className={cn("w-auto dark:hidden", className)} draggable={false} />
      <img src={shoalLogoDark} alt="Shoal" className={cn("hidden w-auto dark:block", className)} draggable={false} />
    </>
  );
}

function useCollapsed(): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => {
    try {
      return localStorage.getItem("shoal.sidebar") === "collapsed";
    } catch {
      return false;
    }
  });
  return [
    v,
    (next) => {
      setV(next);
      try {
        localStorage.setItem("shoal.sidebar", next ? "collapsed" : "open");
      } catch {
        /* ignore */
      }
    },
  ];
}

export function AppShell() {
  const ns = useNs();
  const location = useLocation();
  const { can } = useAuth();
  const [savedCollapsed, setCollapsed] = useCollapsed();
  const palette = useCommandPalette();

  useEffect(() => {
    if (location.pathname.startsWith("/ns/")) rememberNamespace(ns);
  }, [ns, location.pathname]);

  // Full-bleed pages (builder, run viewer) manage their own scrolling.
  const fullBleed = /\/workflows\/[^/]+$|\/runs\/[^/]+$/.test(location.pathname);
  // Canvas pages need the room: the sidebar starts collapsed there.
  const [expandedHere, setExpandedHere] = useState(false);
  useEffect(() => setExpandedHere(false), [fullBleed]);
  const collapsed = fullBleed ? !expandedHere : savedCollapsed;

  return (
    <div className="flex h-full">
      <aside
        className={cn(
          "flex shrink-0 flex-col border-r border-border bg-panel transition-[width] duration-150",
          collapsed ? "w-14" : "w-60",
        )}
      >
        <div className={cn("flex h-12 items-center gap-2 border-b border-border", collapsed ? "justify-center px-0" : "px-3")}>
          <ShoalMark className={cn("shrink-0", collapsed ? "h-5" : "h-6")} />
          {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="text-[13px] leading-tight font-semibold tracking-tight">Shoal</div>
              <TenantName />
            </div>
          )}
        </div>
        <nav className="flex-1 overflow-y-auto px-2 py-2">
          {NAV.map((group, gi) => {
            const items = group.items.filter((i) => !i.permission || can(i.permission, i.namespaced ? ns : undefined));
            if (!items.length) return null;
            return (
              <div key={gi} className={cn(gi > 0 && "mt-3")}>
                {group.label && !collapsed && (
                  <div className="px-2 pb-1 text-[10.5px] font-semibold tracking-wider text-faint uppercase">{group.label}</div>
                )}
                {group.label && collapsed && <div className="mx-2 my-2 h-px bg-border" />}
                {items.map((item) => {
                  const to = item.namespaced ? nsPath(ns, item.to) : item.to;
                  const link = (
                    <NavLink
                      key={item.label}
                      to={to}
                      end={item.to === "/admin"}
                      className={({ isActive }) =>
                        cn(
                          "group flex h-8 items-center gap-2.5 rounded-lg text-[13px] font-medium transition-colors",
                          collapsed ? "justify-center" : "px-2",
                          isActive ? "bg-accent-soft text-accent" : "text-muted hover:bg-subtle hover:text-fg",
                        )
                      }
                    >
                      <item.icon className="size-4 shrink-0" />
                      {!collapsed && <span className="truncate">{item.label}</span>}
                    </NavLink>
                  );
                  return collapsed ? (
                    <Tooltip key={item.label} content={item.label} side="right">
                      {link}
                    </Tooltip>
                  ) : (
                    link
                  );
                })}
              </div>
            );
          })}
        </nav>
        <div className={cn("border-t border-border p-2", collapsed && "flex justify-center")}>
          <Button
            variant="ghost"
            size={collapsed ? "icon-sm" : "sm"}
            className={cn(!collapsed && "w-full justify-start")}
            onClick={() => (fullBleed ? setExpandedHere(collapsed) : setCollapsed(!collapsed))}
            icon={collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          >
            {!collapsed && "Collapse"}
          </Button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-panel/80 px-4 backdrop-blur">
          <NamespaceSwitcher />
          <div className="flex-1" />
          <button
            onClick={() => palette.setOpen(true)}
            className="flex h-8 w-72 items-center gap-2 rounded-lg border border-border bg-subtle/60 px-2.5 text-[13px] text-faint transition-colors hover:border-border-strong hover:text-muted"
          >
            <Search className="size-3.5" />
            <span className="flex-1 text-left">Search or jump to…</span>
            <Kbd>⌘K</Kbd>
          </button>
          <ThemeToggle />
          <UserMenu />
        </header>
        <FederationBanner />
        <main className={cn("min-h-0 flex-1", fullBleed ? "overflow-hidden" : "overflow-y-auto")}>
          <Outlet />
        </main>
      </div>
      <CommandPalette open={palette.open} onOpenChange={palette.setOpen} />
    </div>
  );
}

function TenantName() {
  const { me } = useAuth();
  return <div className="truncate text-[11px] text-faint">{me?.tenant?.name ?? (me?.is_system_admin ? "System admin" : "")}</div>;
}

function NamespaceSwitcher() {
  const ns = useNs();
  const { data } = useNamespaces();
  const { me, can } = useAuth();
  const switchTo = useSwitchNamespace();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const visible = new Set(me?.namespaces ?? []);
  const list = (data ?? []).filter((n) => me?.is_system_admin || visible.has(n.name) || can("namespaces:create"));
  const filtered = list.filter((n) => !q || n.name.includes(q.toLowerCase()) || n.display_name.toLowerCase().includes(q.toLowerCase()));
  const current = list.find((n) => n.name === ns);
  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setQ("");
      }}
      className="w-72 p-1.5"
      trigger={
        <button className="flex h-8 items-center gap-2 rounded-lg border border-border bg-panel px-2 text-[13px] shadow-card hover:border-border-strong">
          <span className="flex size-5 items-center justify-center rounded bg-accent-soft text-[10px] font-bold text-accent uppercase">
            {ns.slice(0, 2)}
          </span>
          <span className="font-medium">{current?.display_name ?? ns}</span>
          <ChevronsUpDown className="size-3.5 text-faint" />
        </button>
      }
    >
      <input
        autoFocus
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Find a namespace…"
        className="mb-1 h-8 w-full rounded-md border border-border bg-subtle/60 px-2 text-[13px] outline-none focus:border-ring"
      />
      <div className="max-h-72 overflow-y-auto">
        {filtered.map((n) => (
          <button
            key={n.name}
            onClick={() => {
              switchTo(n.name);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-subtle"
          >
            <span className="flex size-5 items-center justify-center rounded bg-subtle text-[10px] font-bold text-muted uppercase">{n.name.slice(0, 2)}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium">{n.display_name}</span>
              {n.description && <span className="block truncate text-[11px] text-faint">{n.description}</span>}
            </span>
            {n.name === ns && <Check className="size-3.5 text-accent" />}
          </button>
        ))}
        {!filtered.length && <div className="px-2 py-3 text-center text-xs text-faint">No namespaces match.</div>}
      </div>
      {can("namespaces:create") && (
        <button
          onClick={() => {
            setOpen(false);
            navigate("/admin/namespaces?new=1");
          }}
          className="mt-1 flex w-full items-center gap-2 rounded-md border-t border-border px-2 pt-2 pb-1.5 text-[13px] text-muted hover:text-fg"
        >
          <Plus className="size-3.5" /> New namespace
        </button>
      )}
    </Popover>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useTheme();
  const icon: Record<string, ReactNode> = { light: <Sun className="size-4" />, dark: <Moon className="size-4" />, system: <Monitor className="size-4" /> };
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Theme">
          {icon[theme]}
        </Button>
      </MenuTrigger>
      <MenuContent className="min-w-36">
        {(["light", "dark", "system"] as const).map((t) => (
          <MenuItem key={t} icon={icon[t]} onSelect={() => setTheme(t)} shortcut={theme === t ? <Check className="size-3.5" /> : undefined}>
            {t[0].toUpperCase() + t.slice(1)}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function UserMenu() {
  const { me, signOut } = useAuth();
  const navigate = useNavigate();
  const name = me?.display_name || me?.email || me?.actor || "?";
  const initials = name
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase())
    .join("");
  return (
    <Menu>
      <MenuTrigger asChild>
        <button className="flex size-8 items-center justify-center rounded-full bg-gradient-to-br from-accent to-info text-[11px] font-semibold text-white shadow-card" aria-label="Account">
          {initials || <UserRound className="size-4" />}
        </button>
      </MenuTrigger>
      <MenuContent className="w-60">
        <div className="px-2 py-1.5">
          <div className="truncate text-[13px] font-medium">{name}</div>
          <div className="truncate text-xs text-faint">{me?.actor}</div>
        </div>
        <MenuSeparator />
        <MenuLabel>Account</MenuLabel>
        <MenuItem onSelect={() => navigate("/admin/api-keys")}>My API keys</MenuItem>
        <MenuItem onSelect={() => navigate("/docs")}>Documentation</MenuItem>
        <MenuSeparator />
        <MenuItem icon={<LogOut />} onSelect={() => void signOut()}>
          Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

/** Tenant-wide state of a federated organisation: member offline, or a read-only link. */
function FederationBanner() {
  const qc = useQueryClient();
  const { me } = useAuth();
  const { disconnected, forbidden, readOnly, dismissedReadOnly, dismissReadOnly } = useFederationState();
  const link = useFederatedLink(!!me?.is_system_admin, me?.tenant?.slug);
  if (disconnected)
    return (
      <div role="status" className="flex items-center gap-2.5 border-b border-warn/25 bg-warn-soft px-4 py-2 text-[13px] text-warn">
        <CloudOff className="size-4 shrink-0" />
        <span className="min-w-0 flex-1">
          <b className="font-semibold">Deployment offline.</b> {federationMessage(FEDERATION_DISCONNECTED)}
        </span>
        <Button size="sm" variant="ghost" icon={<RefreshCw className="size-3.5" />} onClick={() => void qc.refetchQueries({ type: "active" })}>
          Retry
        </Button>
      </div>
    );
  if (link && forbidden)
    return (
      <div role="status" className="flex items-center gap-2.5 border-b border-info/25 bg-info-soft px-4 py-2 text-[13px] text-info">
        <ShieldQuestion className="size-4 shrink-0" />
        <span className="min-w-0 flex-1">
          <b className="font-semibold">{me?.tenant?.name} decides who gets in.</b> Its deployment doesn't give the hub operator any access by default. Ask its admin to grant a role to the hub person{" "}
          <code className="rounded bg-panel/60 px-1 font-mono text-[12px]">{me?.actor}</code>, for example{" "}
          <code className="rounded bg-panel/60 px-1 font-mono text-[12px]">--grant viewer=hub_user:{me?.actor}</code> when joining, or a role binding under Admin console › Roles.
        </span>
      </div>
    );
  if (readOnly && !dismissedReadOnly)
    return (
      <div role="status" className="flex items-center gap-2.5 border-b border-info/25 bg-info-soft px-4 py-2 text-[13px] text-info">
        <Lock className="size-4 shrink-0" />
        <span className="min-w-0 flex-1">
          <b className="font-semibold">Read-only.</b> {federationMessage(FEDERATION_READ_ONLY)}
        </span>
        <Button size="icon-sm" variant="ghost" aria-label="Dismiss" icon={<X className="size-3.5" />} onClick={dismissReadOnly} />
      </div>
    );
  return null;
}
