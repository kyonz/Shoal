import * as RDialog from "@radix-ui/react-dialog";
import { Command } from "cmdk";
import { Activity, ArrowRight, Building2, Moon, Plus, Search, Workflow } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useMyTenants, useSwitchTenant } from "@/pages/instance/api";
import { INSTANCE_NAV } from "@/pages/instance/InstanceLayout";
import { useNamespaces, useResources, useRuns } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { nsPath, useNs, useSwitchNamespace } from "@/lib/ns";
import { relTime } from "@/lib/format";
import { Kbd, StatusIcon } from "@/components/ui";
import { ADMIN_NAV, NAV } from "./nav";

export function useCommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return { open, setOpen };
}

const item =
  "flex h-9 cursor-pointer items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-fg data-[selected=true]:bg-subtle [&_svg]:size-4 [&_svg]:text-muted";
const group = "[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-faint [&_[cmdk-group-heading]]:uppercase";

export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const ns = useNs();
  const navigate = useNavigate();
  const { can, me, instance } = useAuth();
  const switchNs = useSwitchNamespace();
  const workflows = useResources("workflows", ns, { enabled: open && can("workflows:read", ns) });
  const runs = useRuns(ns, {}, undefined, open && can("runs:read", ns));
  const namespaces = useNamespaces();
  // The admin token switches with a header and lists tenants in the switcher; the palette covers people.
  const tenants = useMyTenants(open && !!me && !me.is_system_admin);
  const switchTenant = useSwitchTenant();

  const go = (to: string) => {
    onOpenChange(false);
    navigate(to);
  };

  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 animate-fade-in bg-black/40 backdrop-blur-[2px]" />
        <RDialog.Content className="fixed top-[14vh] left-1/2 z-50 w-[min(640px,calc(100vw-2rem))] -translate-x-1/2 animate-pop-in overflow-hidden rounded-2xl border border-border bg-raised shadow-pop">
          <RDialog.Title className="sr-only">Command palette</RDialog.Title>
          <RDialog.Description className="sr-only">Search pages, workflows and runs</RDialog.Description>
          <Command loop className="flex flex-col">
            <div className="flex items-center gap-2 border-b border-border px-3.5">
              <Search className="size-4 text-faint" />
              <Command.Input autoFocus placeholder="Search workflows, runs, pages…" className="h-12 flex-1 bg-transparent text-[14px] outline-none placeholder:text-faint" />
              <Kbd>Esc</Kbd>
            </div>
            <Command.List className="max-h-[56vh] overflow-y-auto p-1.5">
              <Command.Empty className="px-3 py-8 text-center text-[13px] text-muted">Nothing found.</Command.Empty>
              <Command.Group heading="Actions" className={group}>
                {can("workflows:write", ns) && (
                  <Command.Item className={item} onSelect={() => go(nsPath(ns, "workflows", "new"))} keywords={["create", "add"]}>
                    <Plus /> New workflow
                  </Command.Item>
                )}
                <Command.Item
                  className={item}
                  keywords={["dark", "light", "theme"]}
                  onSelect={() => {
                    const dark = !document.documentElement.classList.contains("dark");
                    document.documentElement.classList.toggle("dark", dark);
                    try {
                      localStorage.setItem("shoal.theme", dark ? "dark" : "light");
                    } catch {
                      /* ignore */
                    }
                    onOpenChange(false);
                  }}
                >
                  <Moon /> Toggle theme
                </Command.Item>
              </Command.Group>
              <Command.Group heading="Workflows" className={group}>
                {(workflows.data ?? []).map((w) => (
                  <Command.Item key={w.metadata.name} className={item} value={`workflow ${w.metadata.name} ${w.metadata.description ?? ""}`} onSelect={() => go(nsPath(ns, "workflows", w.metadata.name))}>
                    <Workflow />
                    <span className="flex-1 truncate">{w.metadata.name}</span>
                    <span className="truncate text-xs text-faint">{w.metadata.description}</span>
                  </Command.Item>
                ))}
              </Command.Group>
              <Command.Group heading="Recent runs" className={group}>
                {(runs.data?.items ?? []).slice(0, 8).map((r) => (
                  <Command.Item key={r.id} className={item} value={`run ${r.id} ${r.workflow_name} ${r.status}`} onSelect={() => go(nsPath(ns, "runs", r.id))}>
                    <StatusIcon status={r.status} />
                    <span className="flex-1 truncate">{r.workflow_name}</span>
                    <span className="font-mono text-[11px] text-faint">{r.id.slice(-8)}</span>
                    <span className="w-24 text-right text-xs text-faint">{relTime(r.created_at)}</span>
                  </Command.Item>
                ))}
              </Command.Group>
              <Command.Group heading="Go to" className={group}>
                {NAV.flatMap((g) => g.items)
                  .filter((i) => !i.permission || can(i.permission, i.namespaced ? ns : undefined))
                  .map((i) => (
                    <Command.Item key={i.label} className={item} keywords={i.keywords?.split(" ")} onSelect={() => go(i.namespaced ? nsPath(ns, i.to) : i.to)}>
                      <i.icon />
                      <span className="flex-1">{i.label}</span>
                      <ArrowRight className="opacity-40" />
                    </Command.Item>
                  ))}
              </Command.Group>
              <Command.Group heading="Admin console" className={group}>
                {ADMIN_NAV.filter((i) => i.to !== "/admin" && !NAV.some((g) => g.items.some((n) => n.to === i.to)))
                  .filter((i) => !i.permission || can(i.permission))
                  .map((i) => (
                    <Command.Item key={i.to} className={item} keywords={["admin", ...(i.keywords?.split(" ") ?? [])]} onSelect={() => go(i.to)}>
                      <i.icon />
                      <span className="flex-1">{i.label}</span>
                      <ArrowRight className="opacity-40" />
                    </Command.Item>
                  ))}
              </Command.Group>
              <Command.Group heading="Switch namespace" className={group}>
                {(namespaces.data ?? []).filter((n) => n.name !== ns).map((n) => (
                  <Command.Item
                    key={n.name}
                    className={item}
                    value={`namespace ${n.name} ${n.display_name}`}
                    onSelect={() => {
                      onOpenChange(false);
                      switchNs(n.name);
                    }}
                  >
                    <Activity /> {n.display_name} <span className="text-xs text-faint">{n.name}</span>
                  </Command.Item>
                ))}
              </Command.Group>
              {instance.reader && (
                <Command.Group heading="Instance console" className={group}>
                  {INSTANCE_NAV.map((i) => (
                    <Command.Item key={i.to} className={item} value={`instance ${i.label}`} keywords={i.keywords.split(" ")} onSelect={() => go(i.to)}>
                      <i.icon />
                      <span className="flex-1">{i.label}</span>
                      <ArrowRight className="opacity-40" />
                    </Command.Item>
                  ))}
                </Command.Group>
              )}
              <Command.Group heading="Switch tenant" className={group}>
                {(tenants.data ?? [])
                  .filter((t) => !t.current && (t.status !== "suspended" || instance.reader))
                  .map((t) => (
                    <Command.Item
                      key={t.slug}
                      className={item}
                      value={`tenant ${t.slug} ${t.name}`}
                      onSelect={() => {
                        onOpenChange(false);
                        void switchTenant(t.slug).catch((e) => toast.error(e instanceof ApiError ? e.detail : String(e)));
                      }}
                    >
                      <Building2 /> {t.name} <span className="text-xs text-faint">{t.slug}</span>
                    </Command.Item>
                  ))}
              </Command.Group>
            </Command.List>
          </Command>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
