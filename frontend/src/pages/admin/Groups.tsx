import { Plus, Trash2, Users as UsersIcon, X } from "lucide-react";
import { useEffect, useState } from "react";
import { client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { Badge, Button, Card, EmptyState, Field, Input, Page, PageHeader, Sheet, Skeleton, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { Avatar, SearchInput, SourceBadge, useAction, useBindings, useGroups, useUsers, type Group } from "./shared";

export default function Groups() {
  const { can } = useAuth();
  const manage = can("users:manage");
  const groups = useGroups();
  const users = useUsers();
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Group | "new" | null>(null);
  const userName = (id: string) => {
    const u = users.data?.items.find((x) => x.id === id);
    return u ? u.display_name || u.username : id;
  };
  const rows = (groups.data?.items ?? []).filter((g) => !q || g.display_name.toLowerCase().includes(q.toLowerCase()));

  return (
    <Page>
      <PageHeader
        title="Groups"
        description="Groups collect users so roles can be granted once. Groups from SCIM or an OIDC groups claim stay in sync with your identity provider."
        actions={
          manage && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
              New group
            </Button>
          )
        }
      />
      <Card>
        <div className="flex items-center gap-2 border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search groups" className="w-72" />
        </div>
        {groups.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<UsersIcon className="size-5" />}
            title={q ? "No groups match" : "No groups yet"}
            description="Create a group, or let SCIM / OIDC sync them from your identity provider."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Group</Th>
                <Th>Source</Th>
                <Th>Members</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((g) => (
                <Tr key={g.id} onClick={() => setEditing(g)}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <Avatar name={g.display_name} group />
                      <span className="font-medium">{g.display_name}</span>
                    </div>
                  </Td>
                  <Td>
                    <SourceBadge source={g.source} />
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <div className="flex -space-x-1.5">
                        {g.members.slice(0, 5).map((m) => (
                          <Avatar key={m} name={userName(m)} className="size-6 ring-2 ring-panel" />
                        ))}
                      </div>
                      <span className="text-xs text-muted">{g.members.length}</span>
                    </div>
                  </Td>
                  <Td className="text-muted">{relTime(g.created_at)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <GroupSheet group={editing} onClose={() => setEditing(null)} canManage={manage} />
    </Page>
  );
}

function GroupSheet({ group, onClose, canManage }: { group: Group | "new" | null; onClose: () => void; canManage: boolean }) {
  const isNew = group === "new";
  const g = isNew ? null : group;
  const users = useUsers();
  const bindings = useBindings();
  const confirm = useConfirm();
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const external = !!g && g.source !== "local";
  const editable = canManage && !external;

  useEffect(() => {
    setName(g?.display_name ?? "");
    setMembers(g?.members ?? []);
    setQ("");
  }, [group, g]);

  const save = useAction(
    async () => {
      if (isNew) return unwrap(client.POST("/v1/groups", { body: { display_name: name.trim(), members } }));
      if (!g) return;
      if (name.trim() !== g.display_name)
        await unwrap(client.PATCH("/v1/groups/{group_id}", { params: { path: { group_id: g.id } }, body: { display_name: name.trim() } }));
      await unwrap(client.PUT("/v1/groups/{group_id}/members", { params: { path: { group_id: g.id } }, body: { members } }));
    },
    { success: isNew ? "Group created" : "Group saved", onSuccess: onClose },
  );
  const remove = useAction((id: string) => unwrap(client.DELETE("/v1/groups/{group_id}", { params: { path: { group_id: id } } })), {
    success: "Group deleted",
    onSuccess: onClose,
  });

  const all = users.data?.items ?? [];
  const inGroup = all.filter((u) => members.includes(u.id));
  const candidates = all.filter(
    (u) => !members.includes(u.id) && (!q || `${u.display_name} ${u.username} ${u.email}`.toLowerCase().includes(q.toLowerCase())),
  );
  const roles = (bindings.data ?? []).filter((b) => g && b.subject_kind === "group" && b.subject_id === g.id);

  return (
    <Sheet
      open={!!group}
      onOpenChange={(o) => !o && onClose()}
      title={isNew ? "New group" : g?.display_name}
      description={external ? `Synced from ${g?.source.toUpperCase()}; membership is managed by your identity provider.` : undefined}
      footer={
        editable && (
          <>
            {g && (
              <Button
                variant="ghost"
                className="mr-auto text-bad"
                icon={<Trash2 className="size-3.5" />}
                onClick={async () => {
                  if (
                    await confirm({
                      title: `Delete ${g.display_name}?`,
                      description: "Role bindings granted to this group are removed too.",
                      danger: true,
                      confirmLabel: "Delete",
                    })
                  )
                    remove.mutate(g.id);
                }}
              >
                Delete
              </Button>
            )}
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" loading={save.isPending} disabled={!name.trim()} onClick={() => save.mutate(undefined)}>
              {isNew ? "Create group" : "Save"}
            </Button>
          </>
        )
      }
    >
      <div className="space-y-5">
        <Field label="Name" required>
          <Input
            value={name}
            disabled={!editable && !isNew}
            onChange={(e) => setName(e.target.value)}
            autoFocus={isNew}
            placeholder="e.g. platform-team"
          />
        </Field>
        <div>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-medium">Members</span>
            <span className="text-xs text-faint">{members.length}</span>
          </div>
          <div className="rounded-lg border border-border">
            {inGroup.length ? (
              <div className="divide-y divide-border">
                {inGroup.map((u) => (
                  <div key={u.id} className="flex items-center gap-2.5 px-3 py-1.5">
                    <Avatar name={u.display_name || u.username} className="size-6" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-medium">{u.display_name || u.username}</div>
                      <div className="truncate text-[11px] text-faint">{u.email ?? u.username}</div>
                    </div>
                    {editable && (
                      <Button variant="ghost" size="icon-sm" aria-label="Remove" onClick={() => setMembers(members.filter((m) => m !== u.id))}>
                        <X className="size-3.5" />
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="px-3 py-4 text-center text-xs text-faint">No members yet.</p>
            )}
          </div>
          {editable && (
            <div className="mt-3">
              <SearchInput value={q} onChange={setQ} placeholder="Add people…" />
              {q && (
                <div className="mt-1 max-h-48 overflow-y-auto rounded-lg border border-border p-1">
                  {candidates.slice(0, 20).map((u) => (
                    <button
                      key={u.id}
                      onClick={() => {
                        setMembers([...members, u.id]);
                        setQ("");
                      }}
                      className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-subtle"
                    >
                      <Avatar name={u.display_name || u.username} className="size-6" />
                      <span className="flex-1 truncate text-[13px]">{u.display_name || u.username}</span>
                      <span className="truncate text-[11px] text-faint">{u.email}</span>
                      <Plus className="size-3.5 text-muted" />
                    </button>
                  ))}
                  {!candidates.length && <div className="px-2 py-3 text-center text-xs text-faint">No matching users.</div>}
                </div>
              )}
            </div>
          )}
        </div>
        {g && (
          <div>
            <div className="mb-2 text-xs font-medium">Roles granted to this group</div>
            {roles.length ? (
              <div className="flex flex-wrap gap-1.5">
                {roles.map((b) => (
                  <Badge key={b.id} tone={b.namespace ? "accent" : "info"}>
                    {b.role} · {b.namespace ?? "tenant-wide"}
                  </Badge>
                ))}
              </div>
            ) : (
              <p className="text-xs text-faint">None yet.</p>
            )}
          </div>
        )}
      </div>
    </Sheet>
  );
}
