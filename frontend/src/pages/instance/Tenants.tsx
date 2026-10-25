/** /instance: every tenant on this deployment. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, Globe, LogIn, MoreHorizontal, Pause, Play, Plus, Trash2, UsersRound } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  Page,
  PageHeader,
  Skeleton,
  Stat,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from "@/components/ui";
import { SearchInput } from "@/pages/admin/shared";
import { TokenDialog } from "@/pages/admin/TokenDialog";
import { instanceKeys, useSwitchTenant, useTenantSummaries, type TenantSummary } from "./api";
import { TenantMark, TenantStatus, useTenantActions } from "./shared";

export default function Tenants() {
  const { me, instance } = useAuth();
  const tenants = useTenantSummaries();
  const actions = useTenantActions();
  const switchTenant = useSwitchTenant();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [bootstrap, setBootstrap] = useState<{ slug: string; key: string } | null>(null);

  const list = tenants.data ?? [];
  const rows = list.filter((t) => !q || `${t.name} ${t.slug} ${t.description ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const people = list.reduce((n, t) => n + t.users, 0);
  const suspended = list.filter((t) => t.status === "suspended").length;

  const open = async (t: TenantSummary) => {
    try {
      await switchTenant(t.slug, { viaHeader: !!me?.is_system_admin });
    } catch (e) {
      toast.error(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <Page wide>
      <PageHeader
        icon={<Building2 className="size-4" />}
        title="Tenants"
        description="Each tenant is a separate organisation with its own people, namespaces, workflows and sign-in. People with an account can belong to several and switch between them."
        actions={
          instance.admin && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
              New tenant
            </Button>
          )
        }
      />
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <Stat label="Tenants" icon={<Building2 className="size-4" />} value={tenants.isLoading ? "…" : list.length} />
        <Stat label="People" icon={<UsersRound className="size-4" />} value={tenants.isLoading ? "…" : people} sub="active, across every tenant" />
        <Stat label="Suspended" icon={<Pause className="size-4" />} value={tenants.isLoading ? "…" : suspended} tone={suspended ? "warn" : undefined} />
      </div>
      <Card>
        <div className="flex items-center gap-2 border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search tenants" className="w-72" />
        </div>
        {tenants.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<Building2 className="size-5" />}
            title={q ? "No tenants match" : "No tenants yet"}
            description={q ? undefined : "Create one for each organisation that uses this deployment."}
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Tenant</Th>
                <Th>Status</Th>
                <Th className="text-right">People</Th>
                <Th className="text-right">Namespaces</Th>
                <Th>Created</Th>
                <Th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const current = t.slug === me?.tenant?.slug;
                return (
                  <Tr key={t.id} onClick={() => navigate(`/instance/tenants/${encodeURIComponent(t.slug)}`)}>
                    <Td>
                      <div className="flex items-center gap-2.5">
                        <TenantMark name={t.name} className="size-7" />
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5 font-medium">
                            {t.name}
                            {current && <span className="text-xs font-normal text-faint">(current)</span>}
                            {t.federated && (
                              <Badge tone="info" title="Managed by a member deployment through federation">
                                <Globe className="size-3" /> Federated
                              </Badge>
                            )}
                          </div>
                          <div className="truncate text-xs text-faint">
                            <span className="font-mono">{t.slug}</span>
                            {t.description ? ` · ${t.description}` : ""}
                          </div>
                        </div>
                      </div>
                    </Td>
                    <Td>
                      <TenantStatus status={t.status} />
                    </Td>
                    <Td className="text-right tabular-nums">{t.users}</Td>
                    <Td className="text-right tabular-nums">{t.namespaces}</Td>
                    <Td className="text-muted">{relTime(t.created_at)}</Td>
                    <Td onClick={(e) => e.stopPropagation()}>
                      <Menu>
                        <MenuTrigger asChild>
                          <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${t.name}`}>
                            <MoreHorizontal className="size-4" />
                          </Button>
                        </MenuTrigger>
                        <MenuContent>
                          <MenuItem icon={<LogIn />} disabled={current} onSelect={() => void open(t)}>
                            Open tenant
                          </MenuItem>
                          {instance.admin && (
                            <>
                              {t.status === "suspended" ? (
                                <MenuItem icon={<Play />} onSelect={() => void actions.setStatus(t.slug, t.name, "active")}>
                                  Resume
                                </MenuItem>
                              ) : (
                                <MenuItem icon={<Pause />} onSelect={() => void actions.setStatus(t.slug, t.name, "suspended")}>
                                  Suspend
                                </MenuItem>
                              )}
                              <MenuSeparator />
                              <MenuItem icon={<Trash2 />} danger onSelect={() => void actions.remove(t.slug, t.name)}>
                                Delete…
                              </MenuItem>
                            </>
                          )}
                        </MenuContent>
                      </Menu>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      {creating && (
        <CreateTenantDialog
          onClose={() => setCreating(false)}
          onCreated={(slug, key) => {
            setCreating(false);
            setBootstrap({ slug, key });
          }}
        />
      )}
      <TokenDialog
        token={bootstrap?.key ?? null}
        onClose={() => setBootstrap(null)}
        title={`${bootstrap?.slug ?? "Tenant"} is ready`}
        description="This API key belongs to the tenant's bootstrap admin service account and has every permission there. Use it for automation or to set up sign-in, then revoke it once people can sign in."
      />
    </Page>
  );
}

const SLUG = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

function slugify(name: string) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

function CreateTenantDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (slug: string, key: string) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [email, setEmail] = useState("");
  const effectiveSlug = slugTouched ? slug : slugify(name);
  const slugError = effectiveSlug && !SLUG.test(effectiveSlug) ? "Lowercase letters, digits and dashes; starts and ends with a letter or digit." : undefined;
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        client.POST("/v1/tenants", {
          body: { slug: effectiveSlug, name: name.trim() || effectiveSlug, description: description.trim() || null, admin_email: email.trim() || null },
        }),
      ),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["instance"] });
      void qc.invalidateQueries({ queryKey: instanceKeys.myTenants });
      toast.success(`${r.tenant.name} created`);
      onCreated(r.tenant.slug, r.api_key);
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New tenant"
      description="A separate organisation on this deployment, with its own people, namespaces, sign-in and audit log."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!effectiveSlug || !!slugError} onClick={() => create.mutate()}>
            Create tenant
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (effectiveSlug && !slugError) create.mutate();
        }}
      >
        <Field label="Name" required>
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Corp" />
        </Field>
        <Field label="Slug" required error={slugError} description="Used in URLs, sign-in and the CLI. It can't be changed later.">
          <Input
            mono
            value={effectiveSlug}
            onChange={(e) => {
              setSlugTouched(true);
              setSlug(e.target.value);
            }}
            placeholder="acme"
          />
        </Field>
        <Field label="Description" hint="optional">
          <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field
          label="First admin"
          hint="optional"
          description="This person becomes tenant admin. If they already have an account, the tenant shows up in their switcher; otherwise they're invited and linked when they first sign in."
        >
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="admin@acme.com" />
        </Field>
        {create.error && <p className="text-xs text-bad">{create.error instanceof ApiError ? create.error.detail : String(create.error)}</p>}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
