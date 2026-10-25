import { Check, Copy, Lock, Minus, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { client, unwrap } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import { relTime } from "@/lib/format";
import { matches } from "@/lib/perms";
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
  Switch,
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
import {
  Avatar,
  isGroupKind,
  subjectKindLabel,
  type Subject,
  RoleSelect,
  SubjectPicker,
  useAction,
  useBindings,
  usePermissionCatalog,
  useRoles,
  useSubjectNames,
  type Permission,
  type Role,
} from "./shared";

const NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const VERB_ORDER = [
  "read",
  "use",
  "create",
  "write",
  "run",
  "fire",
  "launch",
  "install",
  "approve",
  "cancel",
  "manage",
  "publish",
  "provision",
  "delete",
];

export default function Roles() {
  const roles = useRoles();
  const bindings = useBindings();
  return (
    <Page wide>
      <PageHeader
        title="Roles & access"
        description="A role is a named set of permissions. Bind roles to users, service accounts or groups, tenant-wide or in one namespace."
      />
      <Tabs defaultValue="roles">
        <TabsList className="mb-4">
          <TabsTrigger value="roles" count={roles.data?.length}>
            Roles
          </TabsTrigger>
          <TabsTrigger value="bindings" count={bindings.data?.length}>
            Role bindings
          </TabsTrigger>
        </TabsList>
        <TabsContent value="roles">
          <RolesTab />
        </TabsContent>
        <TabsContent value="bindings">
          <BindingsTab />
        </TabsContent>
      </Tabs>
    </Page>
  );
}

// --------------------------------------------------------------------------- roles + matrix

function RolesTab() {
  const roles = useRoles();
  const catalog = usePermissionCatalog();
  const bindings = useBindings();
  const { can } = useAuth();
  const manage = can("roles:manage");
  const confirm = useConfirm();
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ name: string; description: string; permissions: string[]; isNew: boolean } | null>(null);

  const current = roles.data?.find((r) => r.name === selected);
  useEffect(() => {
    if (!selected && roles.data?.length) setSelected(roles.data[0].name);
  }, [selected, roles.data]);

  const save = useAction(
    (d: { name: string; description: string; permissions: string[] }) =>
      unwrap(
        client.PUT("/v1/roles/{name}", {
          params: { path: { name: d.name } },
          body: { description: d.description || null, permissions: d.permissions },
        }),
      ),
    {
      success: (_r, d) => `Role ${d.name} saved`,
      onSuccess: (_r, d) => {
        setDraft(null);
        setSelected(d.name);
      },
    },
  );
  const remove = useAction((name: string) => unwrap(client.DELETE("/v1/roles/{name}", { params: { path: { name } } })), {
    success: "Role deleted",
    onSuccess: () => setSelected(null),
  });

  const editing =
    draft ??
    (current && !current.builtin ? { name: current.name, description: current.description, permissions: current.permissions, isNew: false } : null);
  const usage = (role: string) => (bindings.data ?? []).filter((b) => b.role === role).length;

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
      <Card className="h-fit">
        <CardHeader
          title="Roles"
          actions={
            manage && (
              <Button
                size="xs"
                variant="secondary"
                icon={<Plus className="size-3" />}
                onClick={() => setDraft({ name: "", description: "", permissions: ["workflows:read", "runs:read"], isNew: true })}
              >
                New
              </Button>
            )
          }
        />
        <div className="p-1.5">
          {roles.isLoading && <Skeleton className="m-1 h-40" />}
          {(roles.data ?? []).map((r) => {
            const active = !draft && current?.name === r.name;
            return (
              <button
                key={r.name}
                onClick={() => {
                  setDraft(null);
                  setSelected(r.name);
                }}
                className={cn("flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left", active ? "bg-accent-soft" : "hover:bg-subtle")}
              >
                {r.builtin ? (
                  <Lock className="mt-0.5 size-3.5 shrink-0 text-faint" />
                ) : (
                  <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-accent" />
                )}
                <span className="min-w-0 flex-1">
                  <span className={cn("block text-[13px] font-medium", active && "text-accent")}>{r.name}</span>
                  <span className="line-clamp-2 block text-[11px] text-faint">{r.description}</span>
                </span>
                <span className="text-[11px] text-faint tabular-nums" title="Bindings">
                  {usage(r.name)}
                </span>
              </button>
            );
          })}
          {draft?.isNew && (
            <div className="mt-1 flex items-center gap-2 rounded-lg bg-accent-soft px-2.5 py-2 text-[13px] font-medium text-accent">
              <Plus className="size-3.5" /> {draft.name || "new role"}
            </div>
          )}
        </div>
      </Card>

      <Card className="min-w-0">
        {editing ? (
          <>
            <div className="grid gap-3 border-b border-border p-4 sm:grid-cols-[220px_1fr_auto] sm:items-end">
              <Field label="Name" required error={editing.name && !NAME.test(editing.name) ? "lowercase, numbers, dashes" : undefined}>
                <Input
                  mono
                  value={editing.name}
                  disabled={!editing.isNew}
                  onChange={(e) => setDraft({ ...editing, name: e.target.value })}
                  autoFocus={editing.isNew}
                />
              </Field>
              <Field label="Description">
                <Input
                  value={editing.description}
                  disabled={!manage}
                  onChange={(e) => setDraft({ ...editing, description: e.target.value })}
                  placeholder="What is this role for?"
                />
              </Field>
              {manage && (
                <div className="flex gap-2">
                  {!editing.isNew && (
                    <Button
                      variant="ghost"
                      icon={<Trash2 className="size-3.5" />}
                      onClick={async () => {
                        const n = usage(editing.name);
                        if (
                          await confirm({
                            title: `Delete role ${editing.name}?`,
                            description: n ? `${n} binding(s) use this role and will be removed.` : undefined,
                            danger: true,
                            confirmLabel: "Delete",
                          })
                        )
                          remove.mutate(editing.name);
                      }}
                    />
                  )}
                  {draft && (
                    <Button variant="ghost" onClick={() => setDraft(null)}>
                      Discard
                    </Button>
                  )}
                  <Button
                    variant="primary"
                    loading={save.isPending}
                    disabled={!draft || !NAME.test(editing.name) || !editing.permissions.length}
                    onClick={() => save.mutate(editing)}
                  >
                    Save role
                  </Button>
                </div>
              )}
            </div>
            <PermissionMatrix
              catalog={catalog.data ?? []}
              value={editing.permissions}
              onChange={manage ? (permissions) => setDraft({ ...editing, permissions }) : undefined}
            />
          </>
        ) : current ? (
          <>
            <div className="flex items-start justify-between gap-3 border-b border-border p-4">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-semibold">{current.name}</h3>
                  <Badge>
                    <Lock className="size-3" /> Built-in
                  </Badge>
                </div>
                <p className="mt-0.5 text-[13px] text-muted">{current.description}</p>
              </div>
              {manage && (
                <Button
                  size="sm"
                  icon={<Copy className="size-3.5" />}
                  onClick={() =>
                    setDraft({ name: `${current.name}-custom`, description: current.description, permissions: [...current.permissions], isNew: true })
                  }
                >
                  Duplicate as custom
                </Button>
              )}
            </div>
            <PermissionMatrix catalog={catalog.data ?? []} value={current.permissions} />
          </>
        ) : (
          <EmptyState title="Select a role" />
        )}
      </Card>
    </div>
  );
}

/** Rows = resources, columns = verbs, split by tenant and namespace scope. */
function PermissionMatrix({ catalog, value, onChange }: { catalog: Permission[]; value: string[]; onChange?: (v: string[]) => void }) {
  const has = (p: string) => value.some((pattern) => matches(pattern, p));
  const explicit = new Set(value);
  const full = value.includes("*");

  const sections = useMemo(() => {
    return (["namespace", "tenant"] as const).map((scope) => {
      const perms = catalog.filter((p) => p.scope === scope);
      const resources = [...new Set(perms.map((p) => p.name.split(":")[0]))].sort();
      const verbs = [...new Set(perms.map((p) => p.name.split(":")[1]))].sort(
        (a, b) => ((VERB_ORDER.indexOf(a) + 100) % 100) - ((VERB_ORDER.indexOf(b) + 100) % 100) || a.localeCompare(b),
      );
      const byName = new Map(perms.map((p) => [p.name, p]));
      return { scope, resources, verbs, byName };
    });
  }, [catalog]);

  const toggle = (names: string[], on: boolean) => {
    if (!onChange) return;
    // Expand wildcards into explicit permissions before editing.
    let next = new Set(value.includes("*") ? catalog.map((p) => p.name) : [...explicit].filter((p) => !p.includes("*")));
    for (const pattern of value.filter((p) => p.includes("*") && p !== "*")) for (const p of catalog) if (matches(pattern, p.name)) next.add(p.name);
    for (const n of names) on ? next.add(n) : next.delete(n);
    next = new Set([...next].sort());
    onChange([...next]);
  };

  return (
    <div className="space-y-6 p-4">
      <div className="flex items-center justify-between rounded-lg border border-border bg-subtle/40 px-3 py-2">
        <div className="text-[13px]">
          <span className="font-medium">Full access</span>
          <span className="ml-2 text-xs text-muted">Grants every permission, including ones added by future versions (*).</span>
        </div>
        <Switch checked={full} disabled={!onChange} onCheckedChange={(c) => onChange?.(c ? ["*"] : [])} />
      </div>
      {sections.map(({ scope, resources, verbs, byName }) => (
        <div key={scope}>
          <div className="mb-2 flex items-baseline gap-2">
            <h4 className="text-[13px] font-semibold">{scope === "namespace" ? "Namespace permissions" : "Tenant permissions"}</h4>
            <span className="text-xs text-faint">
              {scope === "namespace"
                ? "Apply in the namespace the role is bound to (or all, when tenant-wide)."
                : "Only take effect in tenant-wide bindings."}
            </span>
          </div>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full border-collapse text-[12.5px]">
              <thead>
                <tr className="bg-subtle/60">
                  <th className="sticky left-0 bg-subtle px-3 py-2 text-left text-[11px] font-semibold tracking-wide text-faint uppercase">
                    Resource
                  </th>
                  {verbs.map((v) => {
                    const col = resources.map((r) => `${r}:${v}`).filter((n) => byName.has(n));
                    const all = col.every(has);
                    return (
                      <th key={v} className="px-1 py-2 text-center text-[11px] font-semibold text-faint">
                        <button
                          disabled={!onChange}
                          onClick={() => toggle(col, !all)}
                          className={cn("rounded px-1.5 py-0.5 capitalize", onChange && "hover:bg-border/60 hover:text-fg")}
                        >
                          {v}
                        </button>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {resources.map((r) => {
                  const row = verbs.map((v) => `${r}:${v}`).filter((n) => byName.has(n));
                  const count = row.filter(has).length;
                  return (
                    <tr key={r} className="border-t border-border hover:bg-subtle/40">
                      <td className="sticky left-0 bg-panel px-3 py-1.5">
                        <button
                          disabled={!onChange}
                          onClick={() => toggle(row, count < row.length)}
                          className="flex items-center gap-2 text-left font-medium whitespace-nowrap capitalize disabled:cursor-default"
                        >
                          <span
                            className={cn(
                              "flex size-3.5 items-center justify-center rounded-sm border",
                              count === row.length
                                ? "border-accent bg-accent text-accent-fg"
                                : count
                                  ? "border-accent text-accent"
                                  : "border-border-strong",
                            )}
                          >
                            {count === row.length ? <Check className="size-2.5" /> : count ? <Minus className="size-2.5" /> : null}
                          </span>
                          {r.replace(/-/g, " ")}
                        </button>
                      </td>
                      {verbs.map((v) => {
                        const n = `${r}:${v}`;
                        const p = byName.get(n);
                        if (!p)
                          return (
                            <td key={v} className="text-center text-faint/50">
                              ·
                            </td>
                          );
                        const on = has(n);
                        const viaWildcard = on && !explicit.has(n);
                        return (
                          <td key={v} className="px-1 py-1 text-center">
                            <Tooltip content={`${n} — ${p.description}${viaWildcard ? " (via wildcard)" : ""}`}>
                              <button
                                disabled={!onChange}
                                onClick={() => toggle([n], !on)}
                                aria-label={n}
                                aria-pressed={on}
                                className={cn(
                                  "inline-flex size-5 items-center justify-center rounded-md border transition-colors",
                                  on
                                    ? viaWildcard
                                      ? "border-accent/50 bg-accent-soft text-accent"
                                      : "border-accent bg-accent text-accent-fg"
                                    : "border-border-strong",
                                  onChange && !on && "hover:border-accent",
                                  !onChange && "cursor-default",
                                )}
                              >
                                {on && <Check className="size-3" />}
                              </button>
                            </Tooltip>
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- bindings

function BindingsTab() {
  const { can } = useAuth();
  const manage = can("roles:manage");
  const [scopeFilter, setScopeFilter] = useState("");
  const bindings = useBindings();
  const names = useSubjectNames();
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const remove = useAction((id: string) => unwrap(client.DELETE("/v1/role-bindings/{binding_id}", { params: { path: { binding_id: id } } })), {
    success: "Binding removed",
  });
  const namespaces = useNamespaces();
  const rows = (bindings.data ?? []).filter((b) => !scopeFilter || (scopeFilter === "_tenant" ? !b.namespace : b.namespace === scopeFilter));

  return (
    <Card>
      <div className="flex items-center gap-2 border-b border-border p-3">
        <div className="w-56">
          <NativeSelect value={scopeFilter} onChange={(e) => setScopeFilter(e.target.value)}>
            <option value="">All scopes</option>
            <option value="_tenant">Tenant-wide</option>
            {(namespaces.data ?? []).map((n) => (
              <option key={n.name} value={n.name}>
                {n.display_name}
              </option>
            ))}
          </NativeSelect>
        </div>
        <div className="flex-1" />
        {manage && (
          <Button variant="primary" size="sm" icon={<Plus className="size-3.5" />} onClick={() => setOpen(true)}>
            Grant role
          </Button>
        )}
      </div>
      {bindings.isLoading ? (
        <div className="p-4">
          <Skeleton className="h-24" />
        </div>
      ) : !rows.length ? (
        <EmptyState title="No role bindings" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Subject</Th>
              <Th>Role</Th>
              <Th>Scope</Th>
              <Th>Granted</Th>
              <Th className="w-10" />
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => {
              const s = names.get(b.subject_id);
              const label = s?.name ?? b.subject_id;
              return (
                <Tr key={b.id}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <Avatar name={label} group={isGroupKind(b.subject_kind)} className="size-6" />
                      <span className="font-medium">{label}</span>
                      <span className="text-xs text-faint">{subjectKindLabel(b.subject_kind, s?.kind === "service")}</span>
                    </div>
                  </Td>
                  <Td>
                    <Badge tone="accent">{b.role}</Badge>
                  </Td>
                  <Td>{b.namespace ? <span className="font-mono text-xs">{b.namespace}</span> : <Badge tone="info">Tenant-wide</Badge>}</Td>
                  <Td className="text-muted">
                    {relTime(b.created_at)}
                    {b.created_by && <span className="ml-1 text-faint">by {b.created_by}</span>}
                  </Td>
                  <Td>
                    {manage && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Remove binding"
                        onClick={async () => {
                          if (
                            await confirm({
                              title: "Remove role binding?",
                              description: `${label} loses ${b.role}${b.namespace ? ` in ${b.namespace}` : " tenant-wide"}.`,
                              danger: true,
                              confirmLabel: "Remove",
                            })
                          )
                            remove.mutate(b.id);
                        }}
                      >
                        <Trash2 className="size-3.5" />
                      </Button>
                    )}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      )}
      <GrantDialog open={open} onOpenChange={setOpen} />
    </Card>
  );
}

function GrantDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const namespaces = useNamespaces();
  const roles = useRoles(open);
  const [subject, setSubject] = useState<Subject | null>(null);
  const [role, setRole] = useState("");
  const [scope, setScope] = useState("");
  const grant = useAction(
    () =>
      unwrap(client.POST("/v1/role-bindings", { body: { role, subject_kind: subject!.kind, subject_id: subject!.id, namespace: scope || null } })),
    {
      success: "Role granted",
      onSuccess: () => {
        onOpenChange(false);
        setSubject(null);
        setRole("");
      },
    },
  );
  const selected: Role | undefined = roles.data?.find((r) => r.name === role);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Grant a role"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={grant.isPending} disabled={!subject || !role} onClick={() => grant.mutate(undefined)}>
            Grant
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <SubjectPicker value={subject} onChange={setSubject} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Role" required description={selected?.description}>
            <RoleSelect value={role} onChange={setRole} />
          </Field>
          <Field label="Scope" description={scope ? `Only in ${scope}.` : "Everywhere in the tenant."}>
            <NativeSelect value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="">Tenant-wide</option>
              {(namespaces.data ?? []).map((n) => (
                <option key={n.name} value={n.name}>
                  {n.display_name}
                </option>
              ))}
            </NativeSelect>
          </Field>
        </div>
      </div>
    </Dialog>
  );
}
