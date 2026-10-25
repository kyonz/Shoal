import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Boxes, ChevronLeft, Plus, Trash2, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { nsPath } from "@/lib/ns";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  Input,
  NativeSelect,
  Page,
  PageHeader,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
  Tr,
  useConfirm,
} from "@/components/ui";
import { adminKeys, Avatar, isGroupKind, KV, RoleSelect, type Subject, type SubjectKind, subjectKindLabel, SubjectPicker, useAction, useRoles } from "./shared";

export default function NamespaceDetail() {
  const { name = "" } = useParams();
  const { can } = useAuth();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const manageMembers = can("members:manage", name);
  const ns = useQuery({
    queryKey: ["admin", "namespace", name],
    queryFn: () => unwrap(client.GET("/v1/namespaces/{name}", { params: { path: { name } } })),
  });
  const members = useQuery({
    queryKey: adminKeys.members(name),
    queryFn: () => unwrap(client.GET("/v1/namespaces/{name}/members", { params: { path: { name } } })),
    enabled: can("members:read", name),
  });
  const roles = useRoles();
  const [adding, setAdding] = useState(false);
  const [settings, setSettings] = useState({ display_name: "", description: "" });

  useEffect(() => {
    if (ns.data) setSettings({ display_name: ns.data.display_name, description: ns.data.description ?? "" });
  }, [ns.data]);

  const removeBinding = useAction((id: string) => unwrap(client.DELETE("/v1/role-bindings/{binding_id}", { params: { path: { binding_id: id } } })), {
    success: "Member removed",
  });
  const changeRole = useAction(
    async ({ id, kind, subject, role }: { id: string; kind: SubjectKind; subject: string; role: string }) => {
      await unwrap(client.POST("/v1/role-bindings", { body: { role, subject_kind: kind, subject_id: subject, namespace: name } }));
      await unwrap(client.DELETE("/v1/role-bindings/{binding_id}", { params: { path: { binding_id: id } } }));
    },
    { success: "Role changed" },
  );
  const saveSettings = useAction(
    () =>
      unwrap(
        client.PUT("/v1/namespaces/{name}", {
          params: { path: { name } },
          body: { display_name: settings.display_name || null, description: settings.description || null },
        }),
      ),
    { success: "Namespace saved", invalidate: [["admin"], ["namespaces"]] },
  );
  const remove = useAction(() => unwrap(client.DELETE("/v1/namespaces/{name}", { params: { path: { name } } })), {
    success: `Namespace ${name} deleted`,
    invalidate: [["admin"], ["namespaces"], ["me"]],
    onSuccess: () => navigate("/admin/namespaces"),
  });

  if (ns.error)
    return (
      <Page>
        <ErrorNotice error={ns.error} />
      </Page>
    );

  const local = (members.data ?? []).filter((m) => m.namespace === name);
  const inherited = (members.data ?? []).filter((m) => !m.namespace);
  const protectedNs = name === "default" || name === "shared";

  return (
    <Page>
      <PageHeader
        breadcrumb={
          <Link to="/admin/namespaces" className="flex items-center gap-1 hover:text-fg">
            <ChevronLeft className="size-3.5" /> Namespaces
          </Link>
        }
        icon={<Boxes className="size-4" />}
        title={ns.data?.display_name ?? name}
        description={ns.data?.description ?? <span className="font-mono text-xs">{name}</span>}
        actions={
          <Button variant="secondary" onClick={() => navigate(nsPath(name, "overview"))} icon={<ArrowRight className="size-4" />}>
            Open namespace
          </Button>
        }
      />
      <Tabs defaultValue="members">
        <TabsList className="mb-4">
          <TabsTrigger value="members" count={local.length}>
            Members
          </TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>
        <TabsContent value="members" className="space-y-4">
          <Card>
            <CardHeader
              title="Members"
              description="People, service accounts and groups with a role in this namespace."
              actions={
                manageMembers && (
                  <Button variant="primary" size="sm" icon={<UserPlus className="size-3.5" />} onClick={() => setAdding(true)}>
                    Add member
                  </Button>
                )
              }
            />
            {members.isLoading ? (
              <div className="space-y-2 p-4">
                <Skeleton className="h-9" />
                <Skeleton className="h-9" />
              </div>
            ) : !local.length ? (
              <EmptyState
                icon={<UserPlus className="size-5" />}
                title="No members yet"
                description="Add people or groups and choose what they can do here."
              />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Member</Th>
                    <Th>Role</Th>
                    <Th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {local.map((m) => (
                    <Tr key={m.binding_id}>
                      <Td>
                        <div className="flex items-center gap-2.5">
                          <Avatar name={m.subject_name} group={isGroupKind(m.subject_kind)} />
                          <div>
                            <div className="font-medium">{m.subject_name}</div>
                            <div className="text-xs text-faint">{subjectKindLabel(m.subject_kind)}</div>
                          </div>
                        </div>
                      </Td>
                      <Td>
                        {manageMembers ? (
                          <div className="w-48">
                            <NativeSelect
                              value={m.role}
                              onChange={(e) =>
                                changeRole.mutate({
                                  id: m.binding_id,
                                  kind: m.subject_kind as SubjectKind,
                                  subject: m.subject_id,
                                  role: e.target.value,
                                })
                              }
                            >
                              {(roles.data ?? [])
                                .filter((r) => r.name !== "tenant-admin")
                                .map((r) => (
                                  <option key={r.name} value={r.name}>
                                    {r.name}
                                  </option>
                                ))}
                            </NativeSelect>
                          </div>
                        ) : (
                          <Badge tone="accent">{m.role}</Badge>
                        )}
                      </Td>
                      <Td>
                        {manageMembers && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="Remove member"
                            onClick={async () => {
                              if (
                                await confirm({
                                  title: `Remove ${m.subject_name}?`,
                                  description: `They lose the ${m.role} role in ${name}.`,
                                  danger: true,
                                  confirmLabel: "Remove",
                                })
                              )
                                removeBinding.mutate(m.binding_id);
                            }}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>
          {inherited.length > 0 && (
            <Card>
              <CardHeader title="Inherited access" description="Tenant-wide role bindings that also apply here. Manage them under Roles." />
              <div className="flex flex-wrap gap-2 p-4">
                {inherited.map((m) => (
                  <span key={m.binding_id} className="flex items-center gap-2 rounded-lg border border-border px-2 py-1 text-[13px]">
                    <Avatar name={m.subject_name} group={isGroupKind(m.subject_kind)} className="size-5" />
                    {m.subject_name}
                    <Badge tone="info">{m.role}</Badge>
                  </span>
                ))}
              </div>
            </Card>
          )}
        </TabsContent>
        <TabsContent value="settings" className="space-y-4">
          <Card>
            <CardHeader title="General" />
            <div className="grid max-w-xl gap-3.5 p-4">
              <Field label="Name" description="The namespace's name cannot be changed.">
                <Input mono value={name} disabled />
              </Field>
              <Field label="Display name">
                <Input
                  value={settings.display_name}
                  disabled={!can("namespaces:create")}
                  onChange={(e) => setSettings({ ...settings, display_name: e.target.value })}
                />
              </Field>
              <Field label="Description">
                <Input
                  value={settings.description}
                  disabled={!can("namespaces:create")}
                  onChange={(e) => setSettings({ ...settings, description: e.target.value })}
                />
              </Field>
              {ns.data && (
                <div className="rounded-lg border border-border px-3 py-1">
                  <KV label="Created">{dateTime(ns.data.created_at)}</KV>
                  <KV label="Created by">{ns.data.created_by ?? "—"}</KV>
                </div>
              )}
              {can("namespaces:create") && (
                <div>
                  <Button variant="primary" loading={saveSettings.isPending} onClick={() => saveSettings.mutate(undefined)}>
                    Save
                  </Button>
                </div>
              )}
            </div>
          </Card>
          {can("namespaces:delete") && (
            <Card className="border-bad/30">
              <CardHeader title={<span className="text-bad">Danger zone</span>} />
              <div className="flex items-center justify-between gap-4 p-4">
                <div className="text-[13px]">
                  <div className="font-medium">Delete this namespace</div>
                  <div className="text-muted">
                    {protectedNs
                      ? "The default and shared namespaces cannot be deleted."
                      : "Removes everything in it: workflows, agents, connections, secrets and run history."}
                  </div>
                </div>
                <Button
                  variant="danger"
                  disabled={protectedNs}
                  loading={remove.isPending}
                  onClick={async () => {
                    if (
                      await confirm({
                        title: `Delete namespace ${name}?`,
                        description: "This cannot be undone.",
                        danger: true,
                        confirmLabel: "Delete namespace",
                        typeToConfirm: name,
                      })
                    )
                      remove.mutate(undefined);
                  }}
                >
                  Delete namespace
                </Button>
              </div>
            </Card>
          )}
        </TabsContent>
      </Tabs>
      <AddMemberDialog ns={name} open={adding} onOpenChange={setAdding} />
    </Page>
  );
}

function AddMemberDialog({ ns, open, onOpenChange }: { ns: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [subject, setSubject] = useState<Subject | null>(null);
  const [role, setRole] = useState("editor");
  const roles = useRoles(open);
  const add = useAction(
    () => unwrap(client.POST("/v1/role-bindings", { body: { role, subject_kind: subject!.kind, subject_id: subject!.id, namespace: ns } })),
    {
      success: "Member added",
      onSuccess: () => {
        onOpenChange(false);
        setSubject(null);
      },
    },
  );
  const selected = roles.data?.find((r) => r.name === role);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Add member to ${ns}`}
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon={<Plus className="size-3.5" />}
            loading={add.isPending}
            disabled={!subject || !role}
            onClick={() => add.mutate(undefined)}
          >
            Add member
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <SubjectPicker value={subject} onChange={setSubject} />
        <Field label="Role" description={selected?.description}>
          <RoleSelect value={role} onChange={setRole} scope="namespace" />
        </Field>
      </div>
    </Dialog>
  );
}
