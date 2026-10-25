/** /instance/tenants/:slug: one tenant's details, its tenant-wide people, and suspend or delete. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Building2, Globe, LogIn, Pause, Play, Plus, Trash2, UsersRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Skeleton,
  Table,
  Td,
  Textarea,
  Th,
  Tr,
} from "@/components/ui";
import { Avatar, KV } from "@/pages/admin/shared";
import { TENANT_ROLES, instanceKeys, useSwitchTenant, useTenantMembers, useTenantSummaries } from "./api";
import { TenantMark, TenantStatus, useTenantActions } from "./shared";

const detail = (e: unknown) => (e instanceof ApiError ? e.detail : String(e));

export default function TenantDetail() {
  const { slug = "" } = useParams();
  const { me, instance } = useAuth();
  const tenants = useTenantSummaries();
  const tenant = tenants.data?.find((t) => t.slug === slug);
  const actions = useTenantActions();
  const switchTenant = useSwitchTenant();
  const navigate = useNavigate();

  if (tenants.isLoading)
    return (
      <Page>
        <Skeleton className="mb-4 h-10 w-72" />
        <Skeleton className="h-64" />
      </Page>
    );
  if (!tenant)
    return (
      <Page>
        <EmptyState
          icon={<Building2 className="size-5" />}
          title="Tenant not found"
          description={`There's no tenant called ${slug} on this deployment.`}
          action={<Button onClick={() => navigate("/instance")}>All tenants</Button>}
        />
      </Page>
    );

  const current = tenant.slug === me?.tenant?.slug;
  const suspended = tenant.status === "suspended";
  return (
    <Page>
      <Link to="/instance" className="mb-3 inline-flex items-center gap-1 text-xs text-muted hover:text-fg">
        <ArrowLeft className="size-3.5" /> Tenants
      </Link>
      <PageHeader
        icon={<TenantMark name={tenant.name} className="size-8 text-sm" />}
        title={tenant.name}
        description={tenant.description || undefined}
        actions={
          <>
            {!current && (
              <Button
                icon={<LogIn className="size-4" />}
                onClick={() => void switchTenant(tenant.slug, { viaHeader: !!me?.is_system_admin }).catch((e) => toast.error(detail(e)))}
              >
                Open tenant
              </Button>
            )}
            {instance.admin &&
              (suspended ? (
                <Button variant="primary" icon={<Play className="size-4" />} onClick={() => void actions.setStatus(tenant.slug, tenant.name, "active")}>
                  Resume
                </Button>
              ) : (
                <Button icon={<Pause className="size-4" />} onClick={() => void actions.setStatus(tenant.slug, tenant.name, "suspended")}>
                  Suspend
                </Button>
              ))}
          </>
        }
      />
      {suspended && (
        <div role="status" className="mb-4 flex items-center gap-2 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2 text-[13px] text-warn">
          <Pause className="size-4 shrink-0" />
          <span>
            <b className="font-semibold">Suspended.</b> Its members can't sign in or call the API, and no new runs start. Its data is kept.
          </span>
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
        <div className="min-w-0 space-y-4">
          <Members slug={tenant.slug} canManage={instance.admin} />
          {instance.admin && <Settings slug={tenant.slug} name={tenant.name} description={tenant.description ?? ""} />}
          {instance.admin && (
            <Card className="border-bad/30">
              <CardHeader title="Delete tenant" description="Permanently deletes the tenant and everything in it. People keep their accounts and their other tenants." />
              <div className="flex justify-end px-4 pb-4">
                <Button
                  variant="danger"
                  icon={<Trash2 className="size-4" />}
                  onClick={async () => {
                    if (await actions.remove(tenant.slug, tenant.name)) navigate("/instance", { replace: true });
                  }}
                >
                  Delete {tenant.name}…
                </Button>
              </div>
            </Card>
          )}
        </div>
        <Card className="h-fit p-4">
          <div className="divide-y divide-border">
            <KV label="Status">
              <TenantStatus status={tenant.status} />
            </KV>
            <KV label="Slug">
              <span className="font-mono">{tenant.slug}</span>
            </KV>
            <KV label="People">{tenant.users}</KV>
            <KV label="Namespaces">{tenant.namespaces}</KV>
            <KV label="Federation">
              {tenant.federated ? (
                <Badge tone="info">
                  <Globe className="size-3" /> Managed by a member deployment
                </Badge>
              ) : (
                <span className="text-muted">Not federated</span>
              )}
            </KV>
            <KV label="Created">{dateTime(tenant.created_at)}</KV>
          </div>
        </Card>
      </div>
    </Page>
  );
}

function Settings({ slug, name, description }: { slug: string; name: string; description: string }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState({ name, description });
  useEffect(() => setDraft({ name, description }), [name, description]);
  const dirty = draft.name !== name || draft.description !== description;
  const save = useMutation({
    mutationFn: () =>
      unwrap(client.PATCH("/v1/tenants/{slug}", { params: { path: { slug } }, body: { name: draft.name.trim() || name, description: draft.description.trim() || null } })),
    onSuccess: () => {
      toast.success("Tenant updated");
      void qc.invalidateQueries({ queryKey: ["instance"] });
    },
    onError: (e) => toast.error(detail(e)),
  });
  return (
    <Card>
      <CardHeader title="Details" description="Shown in the tenant switcher and on sign-in." />
      <div className="space-y-4 px-4 pb-4">
        <Field label="Name" required>
          <Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </Field>
        <Field label="Description" hint="optional">
          <Textarea rows={2} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
        </Field>
        <div className="flex justify-end gap-2">
          {dirty && (
            <Button variant="ghost" onClick={() => setDraft({ name, description })}>
              Discard
            </Button>
          )}
          <Button variant="primary" disabled={!dirty || !draft.name.trim()} loading={save.isPending} onClick={() => save.mutate()}>
            Save
          </Button>
        </div>
      </div>
    </Card>
  );
}

function Members({ slug, canManage }: { slug: string; canManage: boolean }) {
  const members = useTenantMembers(slug);
  const [adding, setAdding] = useState(false);
  const list = members.data ?? [];
  return (
    <Card>
      <CardHeader
        title="People with tenant-wide roles"
        description="Namespace roles and groups are managed inside the tenant's own admin console."
        actions={
          canManage && (
            <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => setAdding(true)}>
              Add person
            </Button>
          )
        }
      />
      {members.isLoading ? (
        <div className="space-y-2 p-4">
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </div>
      ) : members.error ? (
        <p className="px-4 pb-4 text-xs text-bad">{detail(members.error)}</p>
      ) : !list.length ? (
        <EmptyState icon={<UsersRound className="size-5" />} title="No people yet" description="Add someone to give them a role here." />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Person</Th>
              <Th>Tenant-wide roles</Th>
              <Th>Account</Th>
            </tr>
          </thead>
          <tbody>
            {list.map((m) => (
              <Tr key={m.user_id}>
                <Td>
                  <div className="flex items-center gap-2.5">
                    <Avatar name={m.display_name || m.username} />
                    <div className="min-w-0">
                      <div className="truncate font-medium">{m.display_name || m.username}</div>
                      <div className="truncate text-xs text-faint">{m.email ?? m.username}</div>
                    </div>
                  </div>
                </Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    {m.roles.length ? (
                      m.roles.map((r) => (
                        <Badge key={r} tone={r === "tenant-admin" ? "accent" : "neutral"} className="font-mono">
                          {r}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-xs text-faint">Namespace roles only</span>
                    )}
                  </div>
                </Td>
                <Td>
                  {m.linked ? (
                    <Badge tone="ok" title="Linked to an instance account: this tenant is in their switcher">
                      Linked
                    </Badge>
                  ) : (
                    <Badge title="Linked when they first sign in with this email">Invited</Badge>
                  )}
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      {adding && <AddMemberDialog slug={slug} onClose={() => setAdding(false)} />}
    </Card>
  );
}

function AddMemberDialog({ slug, onClose }: { slug: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("tenant-admin");
  const add = useMutation({
    mutationFn: () => unwrap(client.POST("/v1/tenants/{slug}/members", { params: { path: { slug } }, body: { email: email.trim(), role } })),
    onSuccess: (m) => {
      toast.success(m.linked ? `${m.display_name || m.email} now has ${role}` : `Invited ${m.email ?? email}`);
      void qc.invalidateQueries({ queryKey: instanceKeys.members(slug) });
      void qc.invalidateQueries({ queryKey: instanceKeys.tenants });
      onClose();
    },
  });
  const valid = /^[^@\s]+@[^@\s]+$/.test(email.trim());
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Add a person"
      description="Grants a role across the whole tenant. If they haven't signed in yet, they're invited and linked when they first sign in with this email."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid} loading={add.isPending} onClick={() => add.mutate()}>
            Add person
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) add.mutate();
        }}
      >
        <Field label="Email" required>
          <Input autoFocus type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="dana@acme.com" />
        </Field>
        <Field label="Role">
          <NativeSelect value={role} onChange={(e) => setRole(e.target.value)}>
            {TENANT_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </NativeSelect>
        </Field>
        {add.error && <p className="text-xs text-bad">{detail(add.error)}</p>}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
