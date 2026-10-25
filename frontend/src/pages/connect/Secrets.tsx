import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Lock, MoreHorizontal, Plus, RotateCw, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { rawJson } from "@/api/client";
import { useResources } from "@/api/hooks";
import { useAuth } from "@/lib/auth";
import { relTime } from "@/lib/format";
import { useNs } from "@/lib/ns";
import { Badge, Button, Card, EmptyState, ErrorNotice, Input, Menu, MenuContent, MenuItem, MenuTrigger, Page, PageHeader, Table, Td, Th, Tr, useConfirm } from "@/components/ui";
import { RowSkeleton, SecretDialog, useSecrets } from "./shared";

export default function Secrets() {
  const ns = useNs();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const { can } = useAuth();
  const canWrite = can("secrets:write", ns);
  const { data, isLoading, error } = useSecrets(ns);
  const connections = useResources("connections", ns, { enabled: can("connections:read", ns) });
  const [dialog, setDialog] = useState<{ name?: string; lock?: boolean } | null>(null);
  const [q, setQ] = useState("");

  const usedBy = new Map<string, string[]>();
  for (const c of connections.data ?? []) {
    for (const ref of Object.values((c.spec.secrets as Record<string, string>) ?? {})) {
      usedBy.set(ref, [...(usedBy.get(ref) ?? []), c.metadata.name]);
    }
  }
  const missing = [...usedBy.keys()].filter((r) => data && !data.some((s) => s.name === r));

  const del = useMutation({
    mutationFn: (name: string) => rawJson<void>(`/v1/namespaces/${ns}/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["secrets", ns] }),
  });

  const remove = async (name: string) => {
    const users = usedBy.get(name) ?? [];
    const ok = await confirm({
      title: `Delete secret ${name}?`,
      description: users.length ? `Used by ${users.join(", ")}. Those connections will stop working.` : "This cannot be undone.",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await del.mutateAsync(name);
    toast.success(`Deleted ${name}`);
  };

  const rows = (data ?? []).filter((s) => !q || s.name.includes(q.toLowerCase()));

  return (
    <Page>
      <PageHeader
        icon={<KeyRound className="size-4" />}
        title="Secrets"
        description="Encrypted values that connections, agents and workflows reference by name. Values are write-only: they can be replaced but never read back."
        actions={
          canWrite && (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setDialog({})}>
              New secret
            </Button>
          )
        }
      />
      <ErrorNotice error={error} className="mb-4" />
      {missing.length > 0 && (
        <Card className="mb-4 border-warn/30 bg-warn-soft p-3 text-[13px]">
          <div className="font-medium text-warn">Referenced but not set</div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {missing.map((m) => (
              <button key={m} disabled={!canWrite} onClick={() => setDialog({ name: m, lock: true })} className="inline-flex h-6 items-center gap-1 rounded-md border border-warn/30 bg-panel px-2 font-mono text-xs hover:border-warn">
                <Plus className="size-3" /> {m}
              </button>
            ))}
          </div>
        </Card>
      )}
      <Card>
        <div className="border-b border-border px-4 py-2.5">
          <div className="relative w-64">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-faint" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter secrets" className="pl-8" />
          </div>
        </div>
        {isLoading ? (
          <RowSkeleton />
        ) : rows.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Value</Th>
                <Th>Used by</Th>
                <Th>Updated</Th>
                <Th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <Tr key={s.name}>
                  <Td className="font-mono text-[12.5px] font-medium">{s.name}</Td>
                  <Td>
                    <span className="inline-flex items-center gap-1.5 text-xs text-faint">
                      <Lock className="size-3" /> ••••••••
                    </span>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {(usedBy.get(s.name) ?? []).map((c) => (
                        <Badge key={c}>{c}</Badge>
                      ))}
                      {!usedBy.get(s.name) && <span className="text-xs text-faint">—</span>}
                    </div>
                  </Td>
                  <Td className="text-xs text-muted">{relTime(s.updated_at)}</Td>
                  <Td>
                    {canWrite && (
                      <Menu>
                        <MenuTrigger asChild>
                          <Button variant="ghost" size="icon-sm" aria-label="Actions">
                            <MoreHorizontal className="size-4" />
                          </Button>
                        </MenuTrigger>
                        <MenuContent>
                          <MenuItem icon={<RotateCw />} onSelect={() => setDialog({ name: s.name, lock: true })}>
                            Replace value
                          </MenuItem>
                          <MenuItem icon={<Trash2 />} danger onSelect={() => void remove(s.name)}>
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
        ) : (
          <EmptyState
            icon={<Lock className="size-5" />}
            title={data?.length ? "No secrets match" : "No secrets yet"}
            description="Store API tokens, private keys and webhook secrets here, then reference them from connections."
            action={canWrite && <Button variant="primary" onClick={() => setDialog({})}>Add a secret</Button>}
          />
        )}
      </Card>
      <SecretDialog
        ns={ns}
        open={!!dialog}
        onOpenChange={(o) => !o && setDialog(null)}
        initialName={dialog?.name}
        lockName={dialog?.lock}
        onSaved={(n) => toast.success(`Saved secret ${n}`)}
      />
    </Page>
  );
}
