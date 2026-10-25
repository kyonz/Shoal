import { useQuery } from "@tanstack/react-query";
import { CircleDot, KeyRound, LogOut, MoreHorizontal, Network, Plus, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { ApiError, client, unwrap, type Schemas } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { dateTime, relTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  CopyField,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  Input,
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
  NativeSelect,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
  Tooltip,
  Tr,
  useConfirm,
} from "@/components/ui";
import { adminKeys, KV, RoleSelect, subjectKindLabel, useAction, useBindings, useMembership, type FederationLink, type Membership } from "./shared";

type Mode = "read" | "read_write";
type Visibility = "full" | "metadata";
type LinkCreated = Schemas["LinkCreated"];

const MODE_LABEL: Record<Mode, string> = { read: "Read only", read_write: "Read and write" };
const MODE_OPTIONS = [
  { value: "read" as Mode, label: "Read only" },
  { value: "read_write" as Mode, label: "Read and write" },
];

/** Admin console › Federation: hub links for operators, hub membership for tenant admins. */
export default function Federation() {
  const { me, can } = useAuth();
  const operator = !!me?.is_system_admin;
  const member = !!me?.tenant && can("federation:manage");
  const [tab, setTab] = useState(operator ? "links" : "membership");

  return (
    <Page>
      <PageHeader
        title="Federation"
        description="Run an organisation in its own deployment, keep its data there, and still reach it from one hub. The member decides what the hub may see and do."
      />
      {operator && member ? (
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList className="mb-5">
            <TabsTrigger value="links">Hub links</TabsTrigger>
            <TabsTrigger value="membership">This organisation</TabsTrigger>
          </TabsList>
          <TabsContent value="links" className="outline-none">
            <HubLinks />
          </TabsContent>
          <TabsContent value="membership" className="outline-none">
            <MembershipPanel />
          </TabsContent>
        </Tabs>
      ) : operator ? (
        <HubLinks />
      ) : member ? (
        <MembershipPanel />
      ) : (
        <EmptyState icon={<Network className="size-5" />} title="No access" description="Federation is managed by operators and organisation admins." />
      )}
    </Page>
  );
}

// --------------------------------------------------------------------------- hub (operator)

function HubLinks() {
  const confirm = useConfirm();
  const links = useQuery({ queryKey: adminKeys.links, queryFn: () => unwrap(client.GET("/v1/federation/links")), refetchInterval: 10_000 });
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<LinkCreated | null>(null);

  const setMode = useAction(
    ({ tenant, mode }: { tenant: string; mode: Mode }) => unwrap(client.PATCH("/v1/federation/links/{tenant}", { params: { path: { tenant } }, body: { mode } })),
    { success: (_, a) => `${a.tenant} now asks for ${MODE_LABEL[a.mode].toLowerCase()} access`, invalidate: [adminKeys.links] },
  );
  const reissue = useAction(
    (tenant: string) => unwrap(client.POST("/v1/federation/links/{tenant}/enrollment", { params: { path: { tenant } } })),
    { invalidate: [adminKeys.links], onSuccess: (r) => setIssued(r) },
  );
  const revoke = useAction(
    (tenant: string) => unwrap(client.DELETE("/v1/federation/links/{tenant}", { params: { path: { tenant } } })),
    { success: (_, t) => `Revoked the link to ${t}`, invalidate: [adminKeys.links] },
  );

  const rows = links.data ?? [];
  return (
    <>
      <Card>
        <CardHeader
          title="Federated organisations"
          description="Calls for these tenants are forwarded to their member deployments over an outbound connection the member opens."
          actions={
            <Button variant="primary" size="sm" icon={<Plus className="size-3.5" />} onClick={() => setCreating(true)}>
              Federate a tenant
            </Button>
          }
        />
        {links.error ? (
          <ErrorNotice error={links.error} className="m-4" />
        ) : links.isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<Network className="size-5" />}
            title="No federated tenants"
            description="Federate an empty tenant, then give its enrollment token to the admin of the deployment that will hold its data."
            action={
              <Button variant="primary" icon={<Plus className="size-3.5" />} onClick={() => setCreating(true)}>
                Federate a tenant
              </Button>
            }
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Organisation</Th>
                <Th>Connection</Th>
                <Th>Access</Th>
                <Th>Exposes</Th>
                <Th>Member</Th>
                <Th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((l) => (
                <Tr key={l.id}>
                  <Td>
                    <div className="font-mono text-[12.5px] font-medium">{l.tenant}</div>
                    <div className="text-[11px] text-faint">linked {relTime(l.created_at)}</div>
                  </Td>
                  <Td>
                    <ConnectionCell link={l} />
                  </Td>
                  <Td>
                    <Tooltip
                      content={`The hub asks for ${MODE_LABEL[l.requested_mode].toLowerCase()}${l.member_mode ? `; the member allows up to ${MODE_LABEL[l.member_mode].toLowerCase()}` : ""}.`}
                    >
                      <span>
                        <Badge tone={l.effective_mode === "read_write" ? "accent" : "info"}>{MODE_LABEL[l.effective_mode]}</Badge>
                      </span>
                    </Tooltip>
                  </Td>
                  <Td className="text-xs text-muted">
                    <div>{l.namespaces.length ? l.namespaces.join(", ") : "All namespaces"}</div>
                    <div className="text-faint">{l.visibility === "metadata" ? "Metadata only" : "Full run data"}</div>
                  </Td>
                  <Td className="text-xs text-muted">{memberVersion(l) ?? <span className="text-faint">—</span>}</Td>
                  <Td>
                    <Menu>
                      <MenuTrigger asChild>
                        <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${l.tenant}`} icon={<MoreHorizontal className="size-4" />} />
                      </MenuTrigger>
                      <MenuContent align="end">
                        {(["read", "read_write"] as Mode[])
                          .filter((m) => m !== l.requested_mode)
                          .map((m) => (
                            <MenuItem key={m} icon={<CircleDot />} disabled={l.status === "revoked"} onSelect={() => setMode.mutate({ tenant: l.tenant, mode: m })}>
                              Ask for {MODE_LABEL[m].toLowerCase()}
                            </MenuItem>
                          ))}
                        <MenuItem icon={<KeyRound />} disabled={l.status === "revoked"} onSelect={() => reissue.mutate(l.tenant)}>
                          New enrollment token
                        </MenuItem>
                        <MenuSeparator />
                        <MenuItem
                          danger
                          icon={<Trash2 />}
                          disabled={l.status === "revoked"}
                          onSelect={async () => {
                            const ok = await confirm({
                              title: `Revoke the link to ${l.tenant}?`,
                              description: "The hub stops forwarding calls for this organisation. Its deployment keeps running on its own with all of its data.",
                              confirmLabel: "Revoke link",
                              danger: true,
                              typeToConfirm: l.tenant,
                            });
                            if (ok) revoke.mutate(l.tenant);
                          }}
                        >
                          Revoke link
                        </MenuItem>
                      </MenuContent>
                    </Menu>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <CreateLinkDialog open={creating} onOpenChange={setCreating} onCreated={setIssued} />
      <EnrollmentDialog created={issued} onClose={() => setIssued(null)} />
    </>
  );
}

function memberVersion(l: FederationLink): string | null {
  const v = (l.member as { version?: string }).version;
  return v ? `v${v}` : null;
}

function ConnectionCell({ link }: { link: FederationLink }) {
  if (link.status === "revoked") return <Badge>Revoked</Badge>;
  if (link.status === "pending")
    return (
      <div>
        <Badge tone="warn" dot>
          Waiting to enroll
        </Badge>
      </div>
    );
  return (
    <div>
      <Badge tone={link.connected ? "ok" : "bad"} dot>
        {link.connected ? "Connected" : "Offline"}
      </Badge>
      {link.last_seen_at && (
        <div className="mt-0.5 text-[11px] text-faint" title={dateTime(link.last_seen_at)}>
          seen {relTime(link.last_seen_at)}
        </div>
      )}
    </div>
  );
}

function CreateLinkDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (r: LinkCreated) => void }) {
  const [tenant, setTenant] = useState("");
  const [mode, setMode] = useState<Mode>("read_write");
  const tenants = useQuery({ queryKey: ["admin", "tenants"], queryFn: () => unwrap(client.GET("/v1/tenants")), enabled: open });
  const links = useQuery({ queryKey: adminKeys.links, queryFn: () => unwrap(client.GET("/v1/federation/links")), enabled: open });
  const linked = new Set((links.data ?? []).filter((l) => l.status !== "revoked").map((l) => l.tenant));
  const create = useAction(() => unwrap(client.POST("/v1/federation/links", { body: { tenant, mode } })), {
    invalidate: [adminKeys.links],
    onSuccess: (r) => {
      onOpenChange(false);
      setTenant("");
      onCreated(r);
    },
  });
  const list = ((tenants.data ?? []) as { slug: string; name: string }[]).filter((t) => !linked.has(t.slug));
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Federate a tenant"
      description="Its data will live in a member deployment. Pick an empty tenant: anything already in it on the hub is no longer reachable once it's federated."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!tenant} onClick={() => create.mutate(undefined)}>
            Federate
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Tenant" required>
          {list.length ? (
            <NativeSelect value={tenant} onChange={(e) => setTenant(e.target.value)}>
              <option value="">Choose a tenant…</option>
              {list.map((t) => (
                <option key={t.slug} value={t.slug}>
                  {t.name} ({t.slug})
                </option>
              ))}
            </NativeSelect>
          ) : (
            <Input value={tenant} onChange={(e) => setTenant(e.target.value.trim())} placeholder="tenant-slug" className="font-mono" />
          )}
        </Field>
        <Field label="Access the hub asks for" description="The member sets a ceiling when it joins and can lower this at any time.">
          <Segmented value={mode} onChange={setMode} options={MODE_OPTIONS} />
        </Field>
      </div>
    </Dialog>
  );
}

function EnrollmentDialog({ created, onClose }: { created: LinkCreated | null; onClose: () => void }) {
  const hub = window.location.origin;
  return (
    <Dialog
      open={!!created}
      onOpenChange={(o) => !o && onClose()}
      size="lg"
      title={created ? `Enroll ${created.link.tenant}` : ""}
      description="Give this single-use token to the admin of the member deployment. It can't be shown again."
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {created && (
        <div className="space-y-4">
          <Field label="Enrollment token" description={`Expires ${relTime(created.enrollment_expires_at)} (${dateTime(created.enrollment_expires_at)}).`}>
            <CopyField value={created.enrollment_token} />
          </Field>
          <Field label="Hub URL">
            <CopyField value={hub} />
          </Field>
          <div className="rounded-lg border border-border bg-subtle/50 p-3 text-[13px] leading-relaxed text-muted">
            On the member, open <b className="text-fg">Admin console › Federation</b> and choose <b className="text-fg">Join a hub</b>, or run:
            <CopyField className="mt-2" value={`shoal federation join ${hub} --token ${created.enrollment_token}`} />
          </div>
        </div>
      )}
    </Dialog>
  );
}

// --------------------------------------------------------------------------- member (tenant admin)

function MembershipPanel() {
  const { me } = useAuth();
  if (me?.federated) return <FederatedFromHere />;
  return <MembershipOfHub />;
}

/** On the hub, a federated tenant's data and hub access are managed in its member deployment. */
function FederatedFromHere() {
  return (
    <EmptyState
      icon={<Network className="size-5" />}
      title="Managed in its own deployment"
      description="This organisation is federated: its data lives in a member deployment that this hub forwards to. Its admins decide what the hub may see and do from the Federation page there."
    />
  );
}

function MembershipOfHub() {
  const membership = useMembership();
  if (membership.isLoading)
    return (
      <div className="space-y-3">
        <Skeleton className="h-40 rounded-xl" />
        <Skeleton className="h-56 rounded-xl" />
      </div>
    );
  if (membership.error) return <ErrorNotice error={membership.error} />;
  return membership.data ? <MembershipDetail m={membership.data} /> : <JoinHub />;
}

interface Grant {
  role: string;
  subject_kind: "hub_user" | "hub_group";
  subject_id: string;
  namespace: string;
}

function JoinHub() {
  const [form, setForm] = useState({ hub_url: "", token: "", max_mode: "read" as Mode, namespaces: [] as string[], visibility: "full" as Visibility });
  const [grants, setGrants] = useState<Grant[]>([]);
  const join = useAction(
    () =>
      unwrap(
        client.PUT("/v1/federation/membership", {
          body: {
            ...form,
            hub_url: form.hub_url.trim(),
            token: form.token.trim(),
            grants: grants.filter((g) => g.role && g.subject_id.trim()).map((g) => ({ ...g, subject_id: g.subject_id.trim(), namespace: g.namespace || null })),
          },
        }),
      ),
    { success: "Joined the hub", invalidate: [adminKeys.membership, ["admin", "bindings"]] },
  );
  const fieldErrors = join.error instanceof ApiError ? join.error.fieldErrors : [];
  const err = (f: string) => fieldErrors.find((e) => e.loc.includes(f))?.msg;

  return (
    <Card>
      <CardHeader title="Join a hub" description="Let a Shoal hub reach this organisation. This deployment keeps its data and checks every forwarded call against its own roles." />
      <div className="space-y-5 p-5">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Hub URL" required error={err("hub_url")}>
            <Input value={form.hub_url} onChange={(e) => setForm({ ...form, hub_url: e.target.value })} placeholder="https://shoal.example.com" />
          </Field>
          <Field label="Enrollment token" required error={err("token")} description="From the hub operator; single use.">
            <Input value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value })} className="font-mono" />
          </Field>
        </div>
        <AccessFields value={form} onChange={(v) => setForm({ ...form, ...v })} />
        <div>
          <div className="mb-1 text-[13px] font-medium">Give hub people access</div>
          <p className="mb-2 text-xs text-muted">
            Role bindings for people and groups in the hub's directory, made when you join. You can add more later under{" "}
            <Link to="/admin/roles" className="text-accent hover:underline">
              Roles
            </Link>
            .
          </p>
          <GrantRows value={grants} onChange={setGrants} />
        </div>
        {join.error && !fieldErrors.length && <ErrorNotice error={join.error} />}
        <div className="flex justify-end">
          <Button variant="primary" loading={join.isPending} disabled={!form.hub_url.trim() || !form.token.trim()} icon={<Network className="size-3.5" />} onClick={() => join.mutate(undefined)}>
            Join hub
          </Button>
        </div>
      </div>
    </Card>
  );
}

function AccessFields({
  value,
  onChange,
}: {
  value: { max_mode: Mode; namespaces: string[]; visibility: Visibility };
  onChange: (v: Partial<{ max_mode: Mode; namespaces: string[]; visibility: Visibility }>) => void;
}) {
  const namespaces = useNamespaces();
  const all = !value.namespaces.length;
  const toggle = (ns: string) =>
    onChange({ namespaces: value.namespaces.includes(ns) ? value.namespaces.filter((n) => n !== ns) : [...value.namespaces, ns] });
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Field label="Most the hub may do" description="The hub's own request can only lower this.">
        <Segmented value={value.max_mode} onChange={(v) => onChange({ max_mode: v })} options={MODE_OPTIONS} />
      </Field>
      <Field label="Run data" description={value.visibility === "metadata" ? "The hub sees statuses, timings and costs, but not inputs, outputs or step data." : "The hub sees runs in full."}>
        <Segmented
          value={value.visibility}
          onChange={(v) => onChange({ visibility: v })}
          options={[
            { value: "full" as Visibility, label: "Full" },
            { value: "metadata" as Visibility, label: "Metadata only" },
          ]}
        />
      </Field>
      <Field label="Namespaces the hub can see" className="md:col-span-2" description={all ? "All namespaces, including ones created later." : `Only ${value.namespaces.length} selected.`}>
        <div className="flex flex-wrap gap-1.5">
          <Chip on={all} onClick={() => onChange({ namespaces: [] })}>
            All
          </Chip>
          {(namespaces.data ?? []).map((n) => (
            <Chip key={n.name} on={value.namespaces.includes(n.name)} onClick={() => toggle(n.name)}>
              {n.display_name}
            </Chip>
          ))}
        </div>
      </Field>
    </div>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={cn(
        "h-7 rounded-md border px-2.5 text-xs font-medium transition-colors",
        on ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:bg-subtle hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

function GrantRows({ value, onChange }: { value: Grant[]; onChange: (v: Grant[]) => void }) {
  const namespaces = useNamespaces();
  const set = (i: number, patch: Partial<Grant>) => onChange(value.map((g, j) => (j === i ? { ...g, ...patch } : g)));
  return (
    <div className="space-y-2">
      {value.map((g, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2 md:flex-nowrap">
          <NativeSelect value={g.subject_kind} onChange={(e) => set(i, { subject_kind: e.target.value as Grant["subject_kind"] })} className="w-32 shrink-0">
            <option value="hub_user">Person</option>
            <option value="hub_group">Group</option>
          </NativeSelect>
          <Input
            value={g.subject_id}
            onChange={(e) => set(i, { subject_id: e.target.value })}
            placeholder={g.subject_kind === "hub_user" ? "email@company.com or sa:name" : "Hub group name"}
            className="min-w-40 flex-1"
          />
          <div className="w-40 shrink-0">
            <RoleSelect value={g.role} onChange={(role) => set(i, { role })} scope={g.namespace ? "namespace" : "tenant"} />
          </div>
          <NativeSelect value={g.namespace} onChange={(e) => set(i, { namespace: e.target.value })} className="w-40 shrink-0">
            <option value="">Tenant-wide</option>
            {(namespaces.data ?? []).map((n) => (
              <option key={n.name} value={n.name}>
                {n.display_name}
              </option>
            ))}
          </NativeSelect>
          <Button variant="ghost" size="icon-sm" aria-label="Remove" icon={<X className="size-3.5" />} onClick={() => onChange(value.filter((_, j) => j !== i))} />
        </div>
      ))}
      <Button size="sm" variant="secondary" icon={<Plus className="size-3.5" />} onClick={() => onChange([...value, { role: "viewer", subject_kind: "hub_user", subject_id: "", namespace: "" }])}>
        Add access
      </Button>
    </div>
  );
}

function MembershipDetail({ m }: { m: Membership }) {
  const confirm = useConfirm();
  const bindings = useBindings();
  const [form, setForm] = useState({ max_mode: m.max_mode as Mode, namespaces: m.namespaces, visibility: m.visibility as Visibility });
  useEffect(() => setForm({ max_mode: m.max_mode as Mode, namespaces: m.namespaces, visibility: m.visibility as Visibility }), [m]);
  const dirty = form.max_mode !== m.max_mode || form.visibility !== m.visibility || [...form.namespaces].sort().join() !== [...m.namespaces].sort().join();

  const save = useAction(() => unwrap(client.PATCH("/v1/federation/membership", { body: form })), {
    success: "Hub access updated",
    invalidate: [adminKeys.membership],
  });
  const leave = useAction(() => unwrap(client.DELETE("/v1/federation/membership")), { success: "Left the hub", invalidate: [adminKeys.membership] });
  const hubBindings = (bindings.data ?? []).filter((b) => b.subject_kind === "hub_user" || b.subject_kind === "hub_group");
  const recent = m.last_connected_at && Date.now() - new Date(m.last_connected_at).getTime() < 90_000;

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          title="Hub membership"
          description="A hub can reach this organisation. Every forwarded call is checked against this deployment's roles and recorded in its audit log."
          actions={
            <Button
              variant="danger"
              size="sm"
              icon={<LogOut className="size-3.5" />}
              onClick={async () => {
                const ok = await confirm({
                  title: "Leave the hub?",
                  description: "The hub can no longer reach this organisation. This deployment keeps running on its own with all of its data.",
                  confirmLabel: "Leave hub",
                  danger: true,
                });
                if (ok) leave.mutate(undefined);
              }}
            >
              Leave hub
            </Button>
          }
        />
        <div className="grid gap-x-8 px-5 py-3 md:grid-cols-2">
          <KV label="Hub">
            <span className="font-mono text-xs">{m.hub_url}</span>
          </KV>
          <KV label="Connection">
            {m.connector_error ? (
              <Tooltip content={m.connector_error}>
                <span>
                  <Badge tone="bad" dot>
                    {m.last_connected_at ? `Can't reach the hub · last connected ${relTime(m.last_connected_at)}` : "Can't reach the hub"}
                  </Badge>
                </span>
              </Tooltip>
            ) : (
              <Badge tone={recent ? "ok" : "warn"} dot>
                {m.last_connected_at ? `${recent ? "Connected" : "Last connected"} ${relTime(m.last_connected_at)}` : "Not connected yet"}
              </Badge>
            )}
          </KV>
          <KV label="Effective access">
            <Tooltip content={`The hub asks for ${m.hub_mode ? MODE_LABEL[m.hub_mode as Mode].toLowerCase() : "—"}; you allow up to ${MODE_LABEL[m.max_mode as Mode].toLowerCase()}.`}>
              <span>
                <Badge tone={m.effective_mode === "read_write" ? "accent" : "info"}>{MODE_LABEL[m.effective_mode as Mode]}</Badge>
              </span>
            </Tooltip>
          </KV>
          <KV label="Joined">{relTime(m.created_at)}</KV>
        </div>
        {m.connector_error && (
          <div className="mx-5 mb-4 rounded-lg border border-bad/25 bg-bad-soft px-3 py-2 text-[13px] text-bad first-letter:uppercase">
            {m.connector_error}. If the hub operator revoked the link, leave the hub here and join again with a new enrollment token.
          </div>
        )}
      </Card>

      <Card>
        <CardHeader title="What the hub may do" />
        <div className="space-y-4 p-5">
          <AccessFields value={form} onChange={(v) => setForm({ ...form, ...v })} />
          <div className="flex justify-end gap-2">
            {dirty && (
              <Button variant="ghost" onClick={() => setForm({ max_mode: m.max_mode as Mode, namespaces: m.namespaces, visibility: m.visibility as Visibility })}>
                Discard
              </Button>
            )}
            <Button variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(undefined)}>
              Save changes
            </Button>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Hub people with access"
          description="Role bindings for people and groups from the hub's directory."
          actions={
            <Link to="/admin/roles">
              <Button size="sm" variant="secondary" icon={<Plus className="size-3.5" />}>
                Grant in Roles
              </Button>
            </Link>
          }
        />
        {!hubBindings.length ? (
          <div className="px-5 py-6 text-center text-[13px] text-muted">No one from the hub has a role here yet, so forwarded calls are refused.</div>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Subject</Th>
                <Th>Role</Th>
                <Th>Scope</Th>
              </tr>
            </thead>
            <tbody>
              {hubBindings.map((b) => (
                <Tr key={b.id}>
                  <Td>
                    <span className="font-medium">{b.subject_id}</span>
                    <span className="ml-2 text-xs text-faint">{subjectKindLabel(b.subject_kind)}</span>
                  </Td>
                  <Td>
                    <Badge tone="accent">{b.role}</Badge>
                  </Td>
                  <Td>{b.namespace ? <span className="font-mono text-xs">{b.namespace}</span> : <Badge tone="info">Tenant-wide</Badge>}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
