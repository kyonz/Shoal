import { LogOut, Network, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { ApiError, client, unwrap } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorNotice,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Table,
  Td,
  Th,
  Tooltip,
  Tr,
  useConfirm,
} from "@/components/ui";
import { MODE_LABEL, MODE_OPTIONS, type FederationMode } from "@/lib/federation";
import { adminKeys, KV, RoleSelect, subjectKindLabel, useAction, useBindings, useMembership, type Membership } from "./shared";

type Mode = FederationMode;
type Visibility = "full" | "metadata";

/** Admin console › Federation: what a hub may see and do in this organisation. Hub links are in the instance console. */
export default function Federation() {
  const { me, can, instance } = useAuth();
  const member = !!me?.tenant && can("federation:manage");

  return (
    <Page>
      <PageHeader
        title="Federation"
        description="Let a Shoal hub reach this organisation while its data stays in this deployment. You decide what the hub may see and do."
      />
      {member ? (
        <MembershipPanel />
      ) : (
        <EmptyState
          icon={<Network className="size-5" />}
          title="No access"
          description="Organisation admins decide whether a hub can reach this organisation."
          action={
            instance.reader ? (
              <Link to="/instance/federation">
                <Button variant="secondary">Hub links in the instance console</Button>
              </Link>
            ) : undefined
          }
        />
      )}
    </Page>
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
                  description:
                    "The hub can no longer reach this organisation, and is told so. Roles given to hub people and groups are removed. This deployment keeps running on its own with all of its data.",
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
