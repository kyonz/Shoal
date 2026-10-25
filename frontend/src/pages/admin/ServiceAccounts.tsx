import { Bot, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { client, unwrap } from "@/api/client";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { Badge, Button, Card, Dialog, EmptyState, Field, Input, Page, PageHeader, Skeleton, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { useQuery } from "@tanstack/react-query";
import { adminKeys, Avatar, useAction, useBindings, useServiceAccounts } from "./shared";

const NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

export default function ServiceAccounts() {
  const { can } = useAuth();
  const manage = can("users:manage");
  const sas = useServiceAccounts();
  const bindings = useBindings();
  const keys = useQuery({
    queryKey: adminKeys.apiKeys(true),
    queryFn: () => unwrap(client.GET("/v1/api-keys", { params: { query: { all: true } } })),
    enabled: can("api-keys:manage"),
  });
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", display_name: "", description: "" });
  const confirm = useConfirm();
  const navigate = useNavigate();

  const create = useAction(
    () =>
      unwrap(
        client.PUT("/v1/service-accounts/{name}", {
          params: { path: { name: form.name } },
          body: { display_name: form.display_name || null, description: form.description || null },
        }),
      ),
    {
      success: "Service account created",
      onSuccess: () => {
        setOpen(false);
        setForm({ name: "", display_name: "", description: "" });
      },
    },
  );
  const remove = useAction((name: string) => unwrap(client.DELETE("/v1/service-accounts/{name}", { params: { path: { name } } })), {
    success: "Service account deleted",
  });

  return (
    <Page>
      <PageHeader
        title="Service accounts"
        description="Non-human principals for CI, Terraform, SCIM and other automation. They own API keys and receive roles like users."
        actions={
          manage && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>
              New service account
            </Button>
          )
        }
      />
      <Card>
        {sas.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : !sas.data?.length ? (
          <EmptyState
            icon={<Bot className="size-5" />}
            title="No service accounts"
            description="Create one for each automation so its access can be scoped and revoked on its own."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Service account</Th>
                <Th>Roles</Th>
                <Th>API keys</Th>
                <Th>Created</Th>
                <Th className="w-24" />
              </tr>
            </thead>
            <tbody>
              {sas.data.map((s) => {
                const roles = (bindings.data ?? []).filter((b) => b.subject_id === s.id);
                const owned = (keys.data ?? []).filter((k) => k.owner_id === s.id && !k.revoked_at);
                return (
                  <Tr key={s.id}>
                    <Td>
                      <div className="flex items-center gap-2.5">
                        <Avatar name={s.name} className="rounded-lg" />
                        <div className="min-w-0">
                          <div className="font-medium">{s.display_name || s.name}</div>
                          <div className="truncate font-mono text-[11px] text-faint">
                            sa:{s.name}
                            {s.description && <span className="ml-2 font-sans">{s.description}</span>}
                          </div>
                        </div>
                      </div>
                    </Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {roles.map((b) => (
                          <Badge key={b.id} tone={b.namespace ? "accent" : "info"}>
                            {b.role}
                            {b.namespace ? ` · ${b.namespace}` : ""}
                          </Badge>
                        ))}
                        {!roles.length && <span className="text-faint">—</span>}
                      </div>
                    </Td>
                    <Td className="text-muted">{keys.data ? owned.length : "—"}</Td>
                    <Td className="text-muted">{relTime(s.created_at)}</Td>
                    <Td>
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          title="Create API key"
                          aria-label="Create API key"
                          onClick={() => navigate(`/admin/api-keys?new=1&sa=${encodeURIComponent(s.name)}`)}
                        >
                          <KeyRound className="size-3.5" />
                        </Button>
                        {manage && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label="Delete"
                            onClick={async () => {
                              if (
                                await confirm({
                                  title: `Delete sa:${s.name}?`,
                                  description: "Its API keys stop working immediately.",
                                  danger: true,
                                  confirmLabel: "Delete",
                                  typeToConfirm: s.name,
                                })
                              )
                                remove.mutate(s.name);
                            }}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="New service account"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" loading={create.isPending} disabled={!NAME.test(form.name)} onClick={() => create.mutate(undefined)}>
              Create
            </Button>
          </>
        }
      >
        <div className="space-y-3.5">
          <Field
            label="Name"
            required
            description="Lowercase letters, numbers and dashes, e.g. terraform or ci-deploy."
            error={form.name && !NAME.test(form.name) ? "Use lowercase letters, numbers and dashes." : undefined}
          >
            <Input mono autoFocus value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Display name">
            <Input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
          </Field>
          <Field label="Description">
            <Input
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="What uses this account?"
            />
          </Field>
        </div>
      </Dialog>
    </Page>
  );
}
