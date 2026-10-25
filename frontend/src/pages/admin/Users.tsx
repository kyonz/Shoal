import { MoreHorizontal, Plus, UserCheck, UserX, Trash2, Pencil, Users as UsersIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, client, unwrap } from "@/api/client";
import { credentialKeys } from "@/pages/connect/PersonalCredentials";
import { useAuth } from "@/lib/auth";
import { dateTime, relTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
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
  Sheet,
  Skeleton,
  Switch,
  Table,
  Td,
  Th,
  Tr,
  useConfirm,
} from "@/components/ui";
import { Avatar, KV, SearchInput, SourceBadge, useAction, useBindings, useGroups, useUsers, userSource, type User } from "./shared";

function useDebounced<T>(value: T, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function Users() {
  const { can, me } = useAuth();
  const manage = can("users:manage");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<"all" | "active" | "inactive">("all");
  const debounced = useDebounced(q);
  const users = useUsers(debounced);
  const groups = useGroups();
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const confirm = useConfirm();

  const groupName = (id: string) => groups.data?.items.find((g) => g.id === id)?.display_name ?? id;
  const setActive = useAction(
    ({ id, active }: { id: string; active: boolean }) =>
      unwrap(client.PATCH("/v1/users/{user_id}", { params: { path: { user_id: id } }, body: { active } })),
    { success: (_r, a) => (a.active ? "User reactivated" : "User deactivated; their sessions and API keys stop working") },
  );
  const remove = useAction((id: string) => unwrap(client.DELETE("/v1/users/{user_id}", { params: { path: { user_id: id } } })), {
    success: "User deleted",
  });

  const rows = (users.data?.items ?? []).filter((u) => status === "all" || (status === "active" ? u.active : !u.active));

  return (
    <Page>
      <PageHeader
        title="Users"
        description="People who can sign in. Users arrive through OIDC sign-in or SCIM provisioning, or you can add them here."
        actions={
          manage && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
              Add user
            </Button>
          )
        }
      />
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search by name, username or email" className="w-80" />
          <div className="flex rounded-lg border border-border bg-subtle p-0.5 text-xs">
            {(["all", "active", "inactive"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setStatus(s)}
                className={`h-7 rounded-md px-2.5 font-medium capitalize ${status === s ? "bg-panel text-fg shadow-card" : "text-muted hover:text-fg"}`}
              >
                {s}
              </button>
            ))}
          </div>
          <div className="flex-1" />
          {users.data && <span className="text-xs text-faint">{users.data.total ?? rows.length} users</span>}
        </div>
        {users.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<UsersIcon className="size-5" />}
            title={q ? "No users match" : "No users yet"}
            description="Connect an OIDC provider or SCIM to bring people in automatically."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>User</Th>
                <Th>Source</Th>
                <Th>Groups</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
                <Th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((u) => (
                <Tr key={u.id} onClick={() => setEditing(u)}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <Avatar name={u.display_name || u.username} />
                      <div className="min-w-0">
                        <div className="truncate font-medium">
                          {u.display_name || u.username}
                          {u.id === me?.user_id && <span className="ml-1.5 text-xs font-normal text-faint">(you)</span>}
                        </div>
                        <div className="truncate text-xs text-faint">{u.email ?? u.username}</div>
                      </div>
                    </div>
                  </Td>
                  <Td>
                    <SourceBadge source={userSource(u)} />
                  </Td>
                  <Td>
                    <div className="flex max-w-64 flex-wrap gap-1">
                      {u.groups.slice(0, 3).map((g) => (
                        <Badge key={g}>{groupName(g)}</Badge>
                      ))}
                      {u.groups.length > 3 && <Badge>+{u.groups.length - 3}</Badge>}
                      {!u.groups.length && <span className="text-faint">—</span>}
                    </div>
                  </Td>
                  <Td>
                    {u.active ? (
                      <Badge tone="ok" dot>
                        Active
                      </Badge>
                    ) : (
                      <Badge tone="bad" dot>
                        Deactivated
                      </Badge>
                    )}
                  </Td>
                  <Td className="text-muted" title={dateTime(u.last_login_at)}>
                    {u.last_login_at ? relTime(u.last_login_at) : "Never"}
                  </Td>
                  <Td onClick={(e) => e.stopPropagation()}>
                    {manage && (
                      <Menu>
                        <MenuTrigger asChild>
                          <Button variant="ghost" size="icon-sm" aria-label="Actions">
                            <MoreHorizontal className="size-4" />
                          </Button>
                        </MenuTrigger>
                        <MenuContent>
                          <MenuItem icon={<Pencil />} onSelect={() => setEditing(u)}>
                            Edit
                          </MenuItem>
                          {u.active ? (
                            <MenuItem
                              icon={<UserX />}
                              disabled={u.id === me?.user_id}
                              onSelect={async () => {
                                if (
                                  await confirm({
                                    title: `Deactivate ${u.display_name || u.username}?`,
                                    description: "Their sessions end and their API keys stop working immediately.",
                                    confirmLabel: "Deactivate",
                                    danger: true,
                                  })
                                )
                                  setActive.mutate({ id: u.id, active: false });
                              }}
                            >
                              Deactivate
                            </MenuItem>
                          ) : (
                            <MenuItem icon={<UserCheck />} onSelect={() => setActive.mutate({ id: u.id, active: true })}>
                              Reactivate
                            </MenuItem>
                          )}
                          <MenuSeparator />
                          <MenuItem
                            icon={<Trash2 />}
                            danger
                            disabled={u.id === me?.user_id}
                            onSelect={async () => {
                              if (
                                await confirm({
                                  title: "Delete user?",
                                  description: "This removes the user, their role bindings, API keys and connected accounts. Use deactivate to keep history.",
                                  confirmLabel: "Delete",
                                  danger: true,
                                  typeToConfirm: u.username,
                                })
                              )
                                remove.mutate(u.id);
                            }}
                          >
                            Delete
                          </MenuItem>
                        </MenuContent>
                      </Menu>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <UserSheet user={editing} onClose={() => setEditing(null)} canManage={manage} />
    </Page>
  );
}

function UserSheet({ user, onClose, canManage }: { user: User | "new" | null; onClose: () => void; canManage: boolean }) {
  const isNew = user === "new";
  const u = isNew ? null : user;
  const groups = useGroups();
  const bindings = useBindings();
  const [form, setForm] = useState({ username: "", email: "", display_name: "", active: true });
  const [memberOf, setMemberOf] = useState<string[]>([]);

  useEffect(() => {
    if (user === "new") setForm({ username: "", email: "", display_name: "", active: true });
    else if (user) setForm({ username: user.username, email: user.email ?? "", display_name: user.display_name, active: user.active });
    setMemberOf(user && user !== "new" ? user.groups : []);
  }, [user]);

  const save = useAction(
    async () => {
      let id = u?.id;
      if (isNew) {
        const created = await unwrap(
          client.POST("/v1/users", {
            body: { username: form.username.trim(), email: form.email || null, display_name: form.display_name || null, active: form.active },
          }),
        );
        id = created.id;
      } else if (u) {
        await unwrap(
          client.PATCH("/v1/users/{user_id}", {
            params: { path: { user_id: u.id } },
            body: { email: form.email || null, display_name: form.display_name || null, active: form.active },
          }),
        );
      }
      // Sync group membership (only groups we manage locally).
      const before = new Set(u?.groups ?? []);
      for (const g of groups.data?.items ?? []) {
        const want = memberOf.includes(g.id);
        if (want === before.has(g.id)) continue;
        const members = want ? [...g.members, id!] : g.members.filter((m) => m !== id);
        await unwrap(client.PUT("/v1/groups/{group_id}/members", { params: { path: { group_id: g.id } }, body: { members } }));
      }
    },
    { success: isNew ? "User added" : "User updated", onSuccess: onClose },
  );

  const myBindings = (bindings.data ?? []).filter(
    (b) => u && (b.subject_id === u.id || (b.subject_kind === "group" && u.groups.includes(b.subject_id))),
  );
  const managedExternally = !!u?.external_id;

  return (
    <Sheet
      open={!!user}
      onOpenChange={(o) => !o && onClose()}
      title={isNew ? "Add user" : u?.display_name || u?.username}
      description={isNew ? "They can sign in through OIDC once their username or email matches." : (u?.email ?? u?.username)}
      footer={
        canManage && (
          <>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" loading={save.isPending} disabled={isNew && !form.username.trim()} onClick={() => save.mutate(undefined)}>
              {isNew ? "Add user" : "Save changes"}
            </Button>
          </>
        )
      }
    >
      <div className="space-y-5">
        {managedExternally && (
          <div className="rounded-lg border border-info/25 bg-info-soft px-3 py-2 text-[13px] text-info">
            Provisioned by SCIM. Changes made here may be overwritten by your identity provider.
          </div>
        )}
        <div className="grid gap-3.5">
          <Field label="Username" required description={isNew ? "Usually the email address used to sign in." : undefined}>
            <Input value={form.username} disabled={!isNew} onChange={(e) => setForm({ ...form, username: e.target.value })} autoFocus={isNew} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Display name">
              <Input value={form.display_name} disabled={!canManage} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
            </Field>
            <Field label="Email">
              <Input type="email" value={form.email} disabled={!canManage} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </Field>
          </div>
          <div className="flex items-center justify-between rounded-lg border border-border bg-subtle/40 px-3 py-2">
            <div>
              <div className="text-xs font-medium">Active</div>
              <div className="text-xs text-faint">Deactivated users cannot sign in and their API keys stop working.</div>
            </div>
            <Switch checked={form.active} disabled={!canManage} onCheckedChange={(active) => setForm({ ...form, active })} />
          </div>
        </div>

        <div>
          <div className="mb-2 text-xs font-medium">Groups</div>
          {!groups.data?.items.length ? (
            <p className="text-xs text-faint">No groups yet.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {groups.data.items.map((g) => {
                const on = memberOf.includes(g.id);
                const external = g.source !== "local";
                return (
                  <button
                    key={g.id}
                    type="button"
                    disabled={!canManage || external}
                    title={external ? `Managed by ${g.source.toUpperCase()}` : undefined}
                    onClick={() => setMemberOf(on ? memberOf.filter((x) => x !== g.id) : [...memberOf, g.id])}
                    className={`h-7 rounded-md border px-2.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                      on ? "border-accent/40 bg-accent-soft text-accent" : "border-border text-muted hover:text-fg"
                    }`}
                  >
                    {g.display_name}
                    {external && <span className="ml-1 text-[10px] uppercase opacity-70">{g.source}</span>}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {u && (
          <>
            <div>
              <div className="mb-2 text-xs font-medium">Roles</div>
              {!myBindings.length ? (
                <p className="text-xs text-faint">No role bindings. Grant roles under Roles or a namespace's Members tab.</p>
              ) : (
                <div className="divide-y divide-border rounded-lg border border-border">
                  {myBindings.map((b) => (
                    <div key={b.id} className="flex items-center justify-between px-3 py-2 text-[13px]">
                      <span className="font-medium">{b.role}</span>
                      <span className="flex items-center gap-1.5 text-xs text-muted">
                        {b.subject_kind === "group" && <Badge>via group</Badge>}
                        {b.namespace ? <Badge tone="accent">{b.namespace}</Badge> : <Badge tone="info">tenant-wide</Badge>}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            {canManage && <UserCredentials username={u.username} />}
            <div className="rounded-lg border border-border px-3 py-1">
              <KV label="ID">
                <span className="font-mono text-xs">{u.id}</span>
              </KV>
              <KV label="Source">
                <SourceBadge source={userSource(u)} />
              </KV>
              {u.oidc_subject && (
                <KV label="OIDC subject">
                  <span className="font-mono text-xs">{u.oidc_subject}</span>
                </KV>
              )}
              {u.external_id && (
                <KV label="SCIM external id">
                  <span className="font-mono text-xs">{u.external_id}</span>
                </KV>
              )}
              <KV label="Created">{dateTime(u.created_at)}</KV>
              <KV label="Last sign-in">{u.last_login_at ? dateTime(u.last_login_at) : "Never"}</KV>
            </div>
          </>
        )}
      </div>
    </Sheet>
  );
}

/** Which connections this person has connected their own account to (metadata only), with revoke. */
function UserCredentials({ username }: { username: string }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const list = useQuery({
    queryKey: credentialKeys.user(username),
    queryFn: () => unwrap(client.GET("/v1/users/{username}/credentials", { params: { path: { username } } })),
  });
  const revoke = useMutation({
    mutationFn: ({ ns, connection }: { ns: string; connection: string }) =>
      unwrap(client.DELETE("/v1/users/{username}/credentials/{namespace}/{connection}", { params: { path: { username, namespace: ns, connection } } })),
    onSuccess: (_r, v) => {
      toast.success(`Revoked ${username}'s credential for ${v.connection}`);
      void qc.invalidateQueries({ queryKey: credentialKeys.user(username) });
    },
    onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
  });
  const rows = list.data ?? [];
  return (
    <div>
      <div className="mb-2 text-xs font-medium">Connected accounts</div>
      {list.isLoading ? (
        <Skeleton className="h-10" />
      ) : !rows.length ? (
        <p className="text-xs text-faint">No personal credentials. Connections that act as each person ask them to connect when a run needs it.</p>
      ) : (
        <div className="divide-y divide-border rounded-lg border border-border">
          {rows.map((c) => (
            <div key={`${c.namespace}/${c.connection}`} className="flex items-center gap-2 px-3 py-2 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">
                  {c.connection} <span className="font-normal text-faint">in {c.namespace}</span>
                </div>
                <div className="text-[11px] text-faint">
                  {c.kind === "oauth" ? "OAuth" : "Token"} · {c.fields.join(", ")} · updated {relTime(c.updated_at)}
                  {c.expires_at ? ` · expires ${relTime(c.expires_at)}` : ""}
                </div>
              </div>
              <Button
                variant="ghost"
                size="sm"
                loading={revoke.isPending && revoke.variables?.connection === c.connection && revoke.variables?.ns === c.namespace}
                onClick={async () => {
                  const ok = await confirm({
                    title: `Revoke ${username}'s credential for ${c.connection}?`,
                    description: "Runs acting for them through this connection will wait until they connect again. The value is deleted, not shown.",
                    confirmLabel: "Revoke",
                    danger: true,
                  });
                  if (ok) revoke.mutate({ ns: c.namespace, connection: c.connection });
                }}
              >
                Revoke
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
