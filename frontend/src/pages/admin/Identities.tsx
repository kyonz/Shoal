import { GitBranch, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ApiError, type Resource } from "@/api/client";
import { useDeleteResource, usePutResource, useResources, useSchemas } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { useNs } from "@/lib/ns";
import { SchemaForm } from "@/components/forms/SchemaForm";
import { Badge, Button, Card, EmptyState, Field, Input, Page, PageHeader, Sheet, Skeleton, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { Avatar, SearchInput } from "./shared";

const NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

export default function Identities() {
  const ns = useNs();
  const { can } = useAuth();
  const write = can("identities:write");
  const list = useResources("identities", ns);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Resource | "new" | null>(null);
  const rows = (list.data ?? []).filter((r) => !q || JSON.stringify([r.metadata.name, r.spec]).toLowerCase().includes(q.toLowerCase()));

  return (
    <Page wide>
      <PageHeader
        title="Identities"
        description="Map each person across systems (email, GitHub, Jira, Slack) so runs started from a webhook are attributed to the human who asked, and governance can check their permissions."
        actions={
          write && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
              Add identity
            </Button>
          )
        }
      />
      <Card>
        <div className="border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search by name, email or login" className="w-80" />
        </div>
        {list.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
          </div>
        ) : !rows.length ? (
          <EmptyState
            icon={<GitBranch className="size-5" />}
            title={q ? "No identities match" : "No identities yet"}
            description="Identities are also created automatically when people link their GitHub account."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Person</Th>
                <Th>GitHub</Th>
                <Th>Jira</Th>
                <Th>Slack</Th>
                <Th>Actors</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const s = r.spec as Record<string, unknown>;
                const display = (s.display_name as string) || r.metadata.name;
                return (
                  <Tr key={r.metadata.name} onClick={() => setEditing(r)}>
                    <Td>
                      <div className="flex items-center gap-2.5">
                        <Avatar name={display} />
                        <div>
                          <div className="font-medium">{display}</div>
                          <div className="text-xs text-faint">{(s.email as string) ?? r.metadata.name}</div>
                        </div>
                      </div>
                    </Td>
                    <Td className="font-mono text-xs">{s.github ? `@${s.github}` : <span className="text-faint">—</span>}</Td>
                    <Td className="font-mono text-xs">{(s.jira_account_id as string) ?? <span className="text-faint">—</span>}</Td>
                    <Td className="font-mono text-xs">{(s.slack_id as string) ?? <span className="text-faint">—</span>}</Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {((s.actors as string[]) ?? []).map((a) => (
                          <Badge key={a} className="font-mono">
                            {a}
                          </Badge>
                        ))}
                        {s.github_token_secret ? <Badge tone="ok">GitHub linked</Badge> : null}
                      </div>
                    </Td>
                    <Td className="text-muted">{relTime(r.metadata.updated_at)}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      <IdentitySheet ns={ns} value={editing} onClose={() => setEditing(null)} canWrite={write} />
    </Page>
  );
}

function IdentitySheet({ ns, value, onClose, canWrite }: { ns: string; value: Resource | "new" | null; onClose: () => void; canWrite: boolean }) {
  const isNew = value === "new";
  const r = isNew ? null : value;
  const schemas = useSchemas();
  const put = usePutResource("identities", ns);
  const del = useDeleteResource("identities", ns);
  const confirm = useConfirm();
  const [name, setName] = useState("");
  const [spec, setSpec] = useState<Record<string, unknown>>({});
  const [description, setDescription] = useState("");

  useEffect(() => {
    setName(r?.metadata.name ?? "");
    setSpec((r?.spec as Record<string, unknown>) ?? {});
    setDescription(r?.metadata.description ?? "");
  }, [value, r]);

  const schema = schemas.data?.identities?.spec_schema;
  const save = () =>
    put.mutate(
      { name, body: { metadata: { description: description || null, labels: r?.metadata.labels ?? {} }, spec } },
      {
        onSuccess: () => {
          toast.success("Identity saved");
          onClose();
        },
        onError: (e) => toast.error(e instanceof ApiError ? e.detail : String(e)),
      },
    );

  return (
    <Sheet
      open={!!value}
      onOpenChange={(o) => !o && onClose()}
      title={isNew ? "Add identity" : ((r?.spec as { display_name?: string })?.display_name ?? r?.metadata.name)}
      footer={
        canWrite && (
          <>
            {r && (
              <Button
                variant="ghost"
                className="mr-auto text-bad"
                icon={<Trash2 className="size-3.5" />}
                onClick={async () => {
                  if (await confirm({ title: `Delete identity ${r.metadata.name}?`, danger: true, confirmLabel: "Delete" }))
                    del.mutate(r.metadata.name, { onSuccess: () => (toast.success("Identity deleted"), onClose()) });
                }}
              >
                Delete
              </Button>
            )}
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" loading={put.isPending} disabled={!NAME.test(name)} onClick={save}>
              Save
            </Button>
          </>
        )
      }
    >
      <div className="space-y-4">
        <Field
          label="Name"
          required
          description="A stable id, e.g. dana-smith."
          error={name && !NAME.test(name) ? "Use lowercase letters, numbers and dashes." : undefined}
        >
          <Input mono value={name} disabled={!isNew} onChange={(e) => setName(e.target.value)} autoFocus={isNew} />
        </Field>
        <Field label="Description">
          <Input value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {schema ? (
          <SchemaForm
            schema={schema}
            value={spec}
            onChange={setSpec}
            order={["display_name", "email", "github", "jira_account_id", "slack_id", "actors"]}
          />
        ) : (
          <Skeleton className="h-48" />
        )}
      </div>
    </Sheet>
  );
}
