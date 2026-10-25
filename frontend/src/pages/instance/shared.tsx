/** Widgets and actions shared by the instance console and the tenant switcher. */
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { cn } from "@/lib/cn";
import { Badge, useConfirm } from "@/components/ui";
import { instanceKeys } from "./api";

export function TenantMark({ name, className }: { name: string; className?: string }) {
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 11);
  return (
    <span
      aria-hidden
      className={cn("flex size-6 shrink-0 items-center justify-center rounded-md text-[10.5px] font-bold text-white uppercase", className)}
      style={{ background: `linear-gradient(135deg, hsl(${hue} 55% 46%), hsl(${(hue + 35) % 360} 60% 40%))` }}
    >
      {name.slice(0, 1)}
    </span>
  );
}

export function TenantStatus({ status }: { status?: string | null }) {
  return status === "suspended" ? (
    <Badge tone="warn" dot>
      Suspended
    </Badge>
  ) : (
    <Badge tone="ok" dot>
      Active
    </Badge>
  );
}

const errorText = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));

/** Suspend, resume and delete a tenant, each behind a confirmation. */
export function useTenantActions() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const refresh = () => void qc.invalidateQueries({ queryKey: ["instance"] });

  const setStatus = async (slug: string, name: string, status: "active" | "suspended") => {
    if (
      status === "suspended" &&
      !(await confirm({
        title: `Suspend ${name}?`,
        description: "Its members are locked out and no new runs start. Nothing is deleted, and you can resume it at any time. Instance admins can still open it.",
        confirmLabel: "Suspend tenant",
        danger: true,
      }))
    )
      return false;
    try {
      await unwrap(client.PATCH("/v1/tenants/{slug}", { params: { path: { slug } }, body: { status } }));
      toast.success(status === "suspended" ? `${name} is suspended` : `${name} is active again`);
      refresh();
      return true;
    } catch (e) {
      toast.error(errorText(e));
      return false;
    }
  };

  const remove = async (slug: string, name: string) => {
    if (
      !(await confirm({
        title: `Delete ${name}?`,
        description: "This permanently deletes the tenant and everything in it: namespaces, workflows, runs, secrets, people, API keys and its audit log. It cannot be undone.",
        confirmLabel: "Delete tenant",
        danger: true,
        typeToConfirm: slug,
      }))
    )
      return false;
    try {
      await unwrap(client.DELETE("/v1/tenants/{slug}", { params: { path: { slug } } }));
      toast.success(`${name} deleted`);
      qc.removeQueries({ queryKey: instanceKeys.members(slug) });
      refresh();
      return true;
    } catch (e) {
      toast.error(errorText(e));
      return false;
    }
  };

  return { setStatus, remove };
}
