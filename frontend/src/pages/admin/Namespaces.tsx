import { Boxes, ChevronRight, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { client, unwrap } from "@/api/client";
import { useNamespaces } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { Badge, Button, Card, Dialog, EmptyState, Field, Input, Page, PageHeader, Skeleton, Table, Td, Th, Tr } from "@/components/ui";
import { SearchInput, useAction, useBindings } from "./shared";

const NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

export default function Namespaces() {
  const { can } = useAuth();
  const namespaces = useNamespaces();
  const bindings = useBindings();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(params.get("new") === "1");
  const [form, setForm] = useState({ name: "", display_name: "", description: "" });

  useEffect(() => {
    if (params.get("new") === "1") setOpen(true);
  }, [params]);

  const close = () => {
    setOpen(false);
    if (params.has("new")) {
      params.delete("new");
      setParams(params, { replace: true });
    }
  };

  const create = useAction(
    () =>
      unwrap(
        client.PUT("/v1/namespaces/{name}", {
          params: { path: { name: form.name } },
          body: { display_name: form.display_name || null, description: form.description || null },
        }),
      ),
    {
      success: (_r) => `Namespace ${form.name} created`,
      invalidate: [["admin"], ["namespaces"], ["me"]],
      onSuccess: () => {
        const name = form.name;
        close();
        setForm({ name: "", display_name: "", description: "" });
        navigate(`/admin/namespaces/${name}`);
      },
    },
  );

  const rows = (namespaces.data ?? []).filter(
    (n) => !q || `${n.name} ${n.display_name} ${n.description ?? ""}`.toLowerCase().includes(q.toLowerCase()),
  );
  const memberCount = (ns: string) => (bindings.data ?? []).filter((b) => b.namespace === ns).length;

  return (
    <Page>
      <PageHeader
        title="Namespaces"
        description="A namespace is a team's space: agents, workflows, connections, secrets and runs live in exactly one. Add people and groups as members with a role."
        actions={
          can("namespaces:create") && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setOpen(true)}>
              New namespace
            </Button>
          )
        }
      />
      <Card>
        <div className="border-b border-border p-3">
          <SearchInput value={q} onChange={setQ} placeholder="Search namespaces" className="w-72" />
        </div>
        {namespaces.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : !rows.length ? (
          <EmptyState icon={<Boxes className="size-5" />} title="No namespaces" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Namespace</Th>
                <Th>Members</Th>
                <Th>Created</Th>
                <Th className="w-8" />
              </tr>
            </thead>
            <tbody>
              {rows.map((n) => (
                <Tr key={n.name} onClick={() => navigate(`/admin/namespaces/${n.name}`)}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <span className="flex size-7 items-center justify-center rounded-lg bg-accent-soft text-[11px] font-bold text-accent uppercase">
                        {n.name.slice(0, 2)}
                      </span>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 font-medium">
                          {n.display_name}
                          <span className="font-mono text-[11px] font-normal text-faint">{n.name}</span>
                          {n.name === "shared" && <Badge tone="info">tenant-wide fallback</Badge>}
                          {n.name === "default" && <Badge>default</Badge>}
                        </div>
                        {n.description && <div className="truncate text-xs text-muted">{n.description}</div>}
                      </div>
                    </div>
                  </Td>
                  <Td className="text-muted">{bindings.data ? memberCount(n.name) : "—"}</Td>
                  <Td className="text-muted">
                    {relTime(n.created_at)}
                    {n.created_by && <span className="ml-1 text-faint">by {n.created_by}</span>}
                  </Td>
                  <Td>
                    <ChevronRight className="size-4 text-faint" />
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Dialog
        open={open}
        onOpenChange={(o) => (o ? setOpen(true) : close())}
        title="New namespace"
        description="You become its admin automatically if you created it with your own account."
        footer={
          <>
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" loading={create.isPending} disabled={!NAME.test(form.name)} onClick={() => create.mutate(undefined)}>
              Create namespace
            </Button>
          </>
        }
      >
        <div className="space-y-3.5">
          <Field label="Display name">
            <Input
              autoFocus
              value={form.display_name}
              placeholder="Payments team"
              onChange={(e) => {
                const display_name = e.target.value;
                const slug = display_name
                  .toLowerCase()
                  .replace(/[^a-z0-9]+/g, "-")
                  .replace(/^-+|-+$/g, "")
                  .slice(0, 63);
                setForm((f) => ({ ...f, display_name, name: !f.name || f.name === autoSlug(f.display_name) ? slug : f.name }));
              }}
            />
          </Field>
          <Field
            label="Name"
            required
            description="Used in URLs and the API. Cannot be changed later."
            error={form.name && !NAME.test(form.name) ? "Use lowercase letters, numbers and dashes." : undefined}
          >
            <Input mono value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="payments" />
          </Field>
          <Field label="Description">
            <Input
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="What does this team automate?"
            />
          </Field>
        </div>
      </Dialog>
    </Page>
  );
}

function autoSlug(s: string) {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}
